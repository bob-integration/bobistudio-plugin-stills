# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Stills plugin — affiche une IMAGE FIXE (avec transparence éventuelle) et PRODUIT
# deux flux vidéo planar via le SDK MXL (bobimxl) :
#   Fill = {hostname}        (l'image cadrée, RGB)
#   Key  = {hostname}_key    (l'alpha en luma : blanc=opaque, noir=transparent)
# MIGRÉ MXL (Phase 1, sans double-écriture legacy) : le writer commit un grain planar par
# trame ; le ring/horodatage sont gérés par libmxl. Genlock → index de grain = grille PTP.
#
# Ce fichier est un TEMPLATE str.format() : SEULS {config} / {hostname} /
# {plugin_version} sont des placeholders. TOUTE autre accolade littérale ({{ }})
# doit être doublée, sinon str.format() échoue.
# ─────────────────────────────────────────────────────────────────────────────
import os, io, time, json, threading, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
from PIL import Image
import bobimxl


def _mxl_lib_state():
    """Variante libmxl réellement chargée (baseline / x86-64-v3) — diagnostic seul, ne doit
    JAMAIS faire échouer /state."""
    try:
        return bobimxl.lib_info()
    except Exception:
        return None

CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True



MEDIA_ROOT = CONFIG.get("media_root") or "/mnt/media"
WIDTH  = int(CONFIG.get("width") or 1280);  WIDTH  -= WIDTH % 2
HEIGHT = int(CONFIG.get("height") or 720);  HEIGHT -= HEIGHT % 2
def _rate_nd(v):
    """Cadence → (num, den) EXACT (fractionnaire NTSC = N*1000/1001 ; accepte \"30000/1001\")."""
    try:
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/"); return int(a), int(b)
        f = float(v or 25)
    except Exception:
        return 25, 1
    n = round(f)
    if abs(f - n) < 0.01: return (n or 25), 1
    nominal = round(f * 1001.0 / 1000.0); return nominal * 1000, 1001
FPS_NUM, FPS_DEN = _rate_nd(CONFIG.get("fps") or 25)
FPS    = max(1, round(FPS_NUM / FPS_DEN))
# Genlock broadcast : générateur calé en PHASE sur la grille PTP (CLOCK_REALTIME, disciplinée
# phc2sys) → write_ts = instant de grille, fill & key sur la grille epoch. off → cadence libre.
_gl = CONFIG.get("genlock", True)
GENLOCK = _gl if isinstance(_gl, bool) else str(_gl).strip().lower() in ("1", "true", "yes", "on")

# ─── format shm (identique au pipeline MXL : cf. plugins/player) ─────────────
CHROMA = str(CONFIG.get("chroma") or "422")
# Profondeur shm 8/10/12 bits. Quantification paramétrée (_SCALE/_MAXV/_NP_DT) : en 8 bits
# _SCALE=1, dtype uint8 → sortie byte-identique.
BIT_DEPTH = int(CONFIG.get("bit_depth") or 8)
_DEEP  = BIT_DEPTH >= 10
_BPS   = 2 if _DEEP else 1
_NP_DT = np.uint16 if _DEEP else np.uint8
_SCALE = 1 << (BIT_DEPTH - 8)
_MAXV  = (1 << BIT_DEPTH) - 1
_NEUTRAL = 1 << (BIT_DEPTH - 1)
# ENTRELACÉ NATIF (modèle SDK MXL) : un GRAIN = 1 CHAMP. Le générateur rend une TRAME pleine
# (HEIGHT) puis, si le balayage de sortie est entrelacé, l'éclate en 2 champs (top=lignes paires,
# bottom=impaires) émis à la cadence/index CHAMP. La sortie est DÉCLARÉE entrelacée (Writer) → le
# reste de la chaîne reste champ-natif (jamais désentrelacé). Progressif inchangé.
SCAN = str(CONFIG.get("scan") or "progressive")
INTERLACED  = SCAN.startswith("interlaced")
FIELD_ORDER = "bff" if SCAN == "interlaced_bff" else "tff"
_CW = {{"420": 2, "422": 2, "444": 1}}.get(CHROMA, 2)   # diviseur largeur chroma
_CH = {{"420": 2, "422": 1, "444": 1}}.get(CHROMA, 1)   # diviseur hauteur chroma
PIX_FMT = ({{"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"}}.get(CHROMA, "yuv422p")
           + (("12le" if BIT_DEPTH >= 12 else "10le") if _DEEP else ""))
UV_W = WIDTH // _CW
UV_H = HEIGHT // _CH
# MXL gère le ring + l'horodatage en interne → on ne garde que la taille d'UNE frame planar
# (= taille de grain attendue ; bobimxl la recalcule comme somme des plans Y+Cb+Cr).
Y_SIZE        = WIDTH * HEIGHT * _BPS
UV_SIZE       = UV_W * UV_H * _BPS
V_FRAME_SIZE  = Y_SIZE + 2 * UV_SIZE
# plans (taille, octets/ligne) pour l'éclatement TRAME → 2 CHAMPS quand la sortie est entrelacée
_PLANES = [(Y_SIZE, WIDTH * _BPS), (UV_SIZE, UV_W * _BPS), (UV_SIZE, UV_W * _BPS)]

# ── MODE TRANCHE (chantier latence sous-trame, cf. patch mxl-planar-slices) ─────────────────
# slice_mode=true → les trames Fill/Key (déjà complètes : buffers rendus une fois par image)
# sont émises PAR BANDES avec commit progressif (validSlices=1..N, pattern delay
# _emit_frame_sliced) → l'aval slice-aware démarre sur la 1ʳᵉ bande. Éligibilité : sortie
# PROGRESSIVE + hauteur divisible (algo _out_slice_h) ; entrelacé → repli whole-frame.
# slice_mode absent/False → comportement STRICTEMENT octet-identique à l'historique.
def _as_bool_cfg(v, default=False):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)
SLICE_MODE  = _as_bool_cfg(CONFIG.get("slice_mode"), default=False)
SLICE_LINES = max(1, int(CONFIG.get("slice_lines") or 36))
_SLICE_TARGET = max(1, 1080 // SLICE_LINES)

def _out_slice_h(oh, ch=1):
    """MODE TRANCHE — slice_height de la sortie (même algo que delay/pyramide) : le plus petit
    diviseur sh de oh avec sh ≥ max(1, oh // _SLICE_TARGET), MULTIPLE de ch (lignes chroma
    entières par bande). Aucun diviseur raisonnable (sh > oh//2) → 0 = inéligible."""
    lo = max(1, oh // _SLICE_TARGET)
    for sh in range(lo, oh // 2 + 1):
        if oh % sh == 0 and sh % ch == 0:
            return sh
    return 0

V_SLICE_H = _out_slice_h(HEIGHT, _CH) if (SLICE_MODE and not INTERLACED) else 0
_V_TOTAL_SLICES = (HEIGHT // V_SLICE_H) if V_SLICE_H else 1
_Y_ROW  = WIDTH * _BPS
_UV_ROW = UV_W * _BPS

def _copy_band(dst, src, a, b):
    """MODE TRANCHE — copie IDENTITÉ des lignes image [a, b) sur les 3 plans (Y|Cb|Cr) du
    payload planar, en OCTETS plats. `b` est toujours un multiple de V_SLICE_H (lui-même
    multiple de _CH) → lignes chroma entières. Renvoie b."""
    if b <= a:
        return a
    dst[a * _Y_ROW:b * _Y_ROW] = src[a * _Y_ROW:b * _Y_ROW]
    ca = a // _CH; cb = b // _CH
    if cb > ca:
        o1 = Y_SIZE; o2 = Y_SIZE + UV_SIZE
        dst[o1 + ca * _UV_ROW:o1 + cb * _UV_ROW] = src[o1 + ca * _UV_ROW:o1 + cb * _UV_ROW]
        dst[o2 + ca * _UV_ROW:o2 + cb * _UV_ROW] = src[o2 + ca * _UV_ROW:o2 + cb * _UV_ROW]
    return b

def _emit_grain_sliced(w, gi, vw, data):
    """MODE TRANCHE — émet une trame déjà COMPLÈTE dans un grain OUVERT, par bandes + commit
    progressif (validSlices=1..N, quasi gratuit). Commit FINAL garanti (un grain laissé
    partiel ne serait jamais lisible)."""
    try:
        src = np.frombuffer(data, dtype=np.uint8)
        if src.size < V_FRAME_SIZE:
            n = min(src.size, len(vw))
            vw[:n] = src[:n]           # frame courte (défensif) : copie pleine, pas de bandes
            return
        a = 0
        for k in range(1, _V_TOTAL_SLICES + 1):
            a = _copy_band(vw, src, a, min(HEIGHT, k * V_SLICE_H))
            if k < _V_TOTAL_SLICES:
                w.commit(gi, valid_slices=k)   # réveille l'aval à chaque bande
    finally:
        w.commit(gi, valid_slices=None)

def _split_fields(full_b):
    """TRAME planar pleine → (champ_1er, champ_2nd). tff → 1er champ = lignes paires ; bff = impaires.
    Renvoie les 2 grains-champs prêts à écrire (octets planar demi-hauteur)."""
    bff = FIELD_ORDER == "bff"
    a = bytearray(); b = bytearray(); off = 0
    for (psz, rb) in _PLANES:
        pl = np.frombuffer(full_b, dtype=np.uint8, count=psz, offset=off).reshape(-1, rb)
        ev = pl[0::2]; od = pl[1::2]                 # lignes paires / impaires
        if bff:                                      # bottom (impaires) émis en 1er
            a += od.tobytes(); b += ev.tobytes()
        else:                                        # top (paires) émis en 1er
            a += ev.tobytes(); b += od.tobytes()
        off += psz
    return bytes(a), bytes(b)

FILL_SHM = HOSTNAME
KEY_SHM  = HOSTNAME + "_key"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff")

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE = Image.LANCZOS

# ─── état + buffers courants ─────────────────────────────────────────────────
state_lock = threading.Lock()
state = {{
    "folder": CONFIG.get("folder") or "",
    "file":   CONFIG.get("file") or "",
    "fit":    CONFIG.get("fit") or "native",   # native | fit | stretch
    "x":      float(CONFIG.get("x") or 0),      # décalage % de la largeur (centré)
    "y":      float(CONFIG.get("y") or 0),      # décalage % de la hauteur (centré)
    "size":   float(CONFIG.get("size") or 100), # échelle %
}}

_BLACK_B = (16 << (BIT_DEPTH - 8)).to_bytes(_BPS, "little")   # Y noir studio (16/64/256)
_KEY0_B  = (0).to_bytes(_BPS, "little")                        # Y matte transparent (0)
_NEUT_B  = _NEUTRAL.to_bytes(_BPS, "little")                   # U/V neutre (128/512/2048)
def _black_fill():
    return (_BLACK_B * (WIDTH * HEIGHT)) + (_NEUT_B * (2 * UV_W * UV_H))
def _black_key():
    return (_KEY0_B * (WIDTH * HEIGHT)) + (_NEUT_B * (2 * UV_W * UV_H))

frames_lock = threading.Lock()
frames = {{"fill": _black_fill(), "key": _black_key()}}

metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "file": "",
            "slice_mode": bool(V_SLICE_H), "plugin_version": PLUGIN_VERSION}}


# ─── stockage : dossiers + images (relatifs à MEDIA_ROOT) ────────────────────
def _safe_join(rel):
    rel = (rel or "").strip().lstrip("/")
    full = os.path.normpath(os.path.join(MEDIA_ROOT, rel))
    if full != MEDIA_ROOT and not full.startswith(MEDIA_ROOT + os.sep):
        return None
    return full

def _list_folders():
    out = set()
    try:
        for root, _dirs, files in os.walk(MEDIA_ROOT):
            if any(fn.lower().endswith(IMAGE_EXTS) for fn in files):
                rel = os.path.relpath(root, MEDIA_ROOT)
                out.add("" if rel == "." else rel)
    except Exception:
        pass
    return sorted(out)

def _list_images(folder):
    base = _safe_join(folder) or MEDIA_ROOT
    out = []
    try:
        for fn in sorted(os.listdir(base)):
            if fn.lower().endswith(IMAGE_EXTS):
                full = os.path.join(base, fn)
                if os.path.isfile(full):
                    out.append(os.path.relpath(full, MEDIA_ROOT))
    except Exception:
        pass
    return out


# ─── conversions vers YUV420p (BT.601 studio, cf. color_corrector) ───────────
def _rgb_to_yuv420(rgb):
    """rgb uint8 (H,W,3) → bytes Y + U + V (4:2:0)."""
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    y = ( 0.257 * r + 0.504 * g + 0.098 * b + 16.0)
    u = (-0.148 * r - 0.291 * g + 0.439 * b + 128.0)
    v = ( 0.439 * r - 0.368 * g - 0.071 * b + 128.0)
    y_u8 = np.clip(y * _SCALE, 0, _MAXV).astype(_NP_DT)
    u_u8 = np.clip(u[::_CH, ::_CW] * _SCALE, 0, _MAXV).astype(_NP_DT)
    v_u8 = np.clip(v[::_CH, ::_CW] * _SCALE, 0, _MAXV).astype(_NP_DT)
    return y_u8.tobytes() + u_u8.tobytes() + v_u8.tobytes()

def _alpha_to_yuv420(alpha):
    """alpha uint8 (H,W) → key : Y=alpha (matte, mis à l'échelle N bits), U=V=neutre."""
    y = (np.asarray(alpha, dtype=np.uint16) * _SCALE).clip(0, _MAXV).astype(_NP_DT) if _DEEP \
        else np.ascontiguousarray(alpha, dtype=np.uint8)
    uv = np.full((UV_H, UV_W), _NEUTRAL, dtype=_NP_DT)
    return y.tobytes() + uv.tobytes() + uv.tobytes()


# ─── rendu de l'image courante → buffers Fill/Key ────────────────────────────
def _render(file, fit, x, y, size):
    full = _safe_join(file)
    if not full or not os.path.isfile(full):
        with frames_lock:
            frames["fill"] = _black_fill(); frames["key"] = _black_key()
        return False, "fichier introuvable"
    try:
        img = Image.open(full).convert("RGBA")
    except Exception as e:
        return False, "décodage : %s" % e
    w0, h0 = img.size
    if w0 <= 0 or h0 <= 0:
        return False, "image vide"

    # taille de base selon le mode de cadrage
    if fit == "stretch":
        bw, bh = WIDTH, HEIGHT
    elif fit == "fit":
        s = min(WIDTH / w0, HEIGHT / h0)
        bw, bh = max(1, round(w0 * s)), max(1, round(h0 * s))
    else:  # native = pixel-to-pixel
        bw, bh = w0, h0

    sc = max(0.01, float(size or 100) / 100.0)        # échelle %
    tw, th = max(1, round(bw * sc)), max(1, round(bh * sc))
    if (tw, th) != (w0, h0):
        img = img.resize((tw, th), RESAMPLE)
    src = np.asarray(img)                              # (th,tw,4) uint8

    # canvas W×H transparent : image centrée + décalage X/Y (% du cadre, origine centre)
    canvas = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    ox = int(round((WIDTH  - tw) / 2 + (float(x or 0) / 100.0) * WIDTH))
    oy = int(round((HEIGHT - th) / 2 + (float(y or 0) / 100.0) * HEIGHT))
    dx0, dy0 = max(0, ox), max(0, oy)
    dx1, dy1 = min(WIDTH, ox + tw), min(HEIGHT, oy + th)
    if dx1 > dx0 and dy1 > dy0:                         # portion visible (clipping)
        sx0, sy0 = dx0 - ox, dy0 - oy
        canvas[dy0:dy1, dx0:dx1, :] = src[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0), :]

    fill_bytes = _rgb_to_yuv420(canvas[:, :, :3])
    key_bytes  = _alpha_to_yuv420(canvas[:, :, 3])
    with frames_lock:
        frames["fill"] = fill_bytes; frames["key"] = key_bytes
    return True, "ok"


# ─── producteurs MXL (Fill + Key) ────────────────────────────────────────────
def feeder():
    # Domaine MXL = $MXL_DOMAIN ou /dev/shm/mxl (sous-rép. du tmpfs bind-monté). Deux flux
    # planar nommés comme les anciens shm (les consommateurs MXL les retrouvent par le NOM).
    inst = bobimxl.Instance()
    # Sortie déclarée entrelacée si SCAN=interlaced_* : libmxl dimensionne chaque grain à 1 CHAMP
    # (½ HEIGHT) et double la cadence en interne ; on écrit 2 grains-champs par trame.
    # MODE TRANCHE : les flowDef Fill/Key portent slice_height quand éligible (progressif +
    # hauteur divisible) → grains publiés en N tranches égales ; sinon inchangés (historique).
    _slkw = {{"slice_height": V_SLICE_H}} if V_SLICE_H else {{}}
    fill_w = bobimxl.Writer(inst, FILL_SHM, WIDTH, HEIGHT, CHROMA, BIT_DEPTH,
                            FPS_NUM, FPS_DEN, interlace=SCAN, **_slkw)
    key_w  = bobimxl.Writer(inst, KEY_SHM,  WIDTH, HEIGHT, CHROMA, BIT_DEPTH,
                            FPS_NUM, FPS_DEN, interlace=SCAN, **_slkw)
    if SLICE_MODE:
        log("slice_mode %s : sorties %s/%s %s" % ("actif" if V_SLICE_H else "INÉLIGIBLE",
            FILL_SHM, KEY_SHM,
            ("tranchées par %d lignes (%d tranches)" % (V_SLICE_H, _V_TOTAL_SLICES))
            if V_SLICE_H else "(%s) → chemin historique"
            % ("entrelacé" if INTERLACED else "hauteur %d sans diviseur" % HEIGHT)), "info")
    win_cnt = 0; win_start = time.time()
    period = FPS_DEN / FPS_NUM

    def _emit(w, payload, idx):
        _, gi, view = w.open_grain(index=idx)
        n = min(len(payload), len(view))
        view[:n] = np.frombuffer(payload, dtype=np.uint8, count=n)
        w.commit(gi)

    while True:
        if GENLOCK:
            # Coordonnée ET échéance viennent du point unique (bobimxl), sur l'horloge TAI. Le
            # calcul de grille était fait ici à la main, sur CLOCK_REALTIME — qui vaut l'UTC sur un
            # nœud correctement synchronisé, soit 37 s à côté de tous les autres producteurs.
            # `lookahead=1` : viser LE créneau suivant, et pas au-delà. Dormir jusqu'à l'instant
            # de la trame allouée consomme la marge : avec L créneaux d'avance, la tête avance de
            # L par tour et la cadence est divisée par L (mesuré sur avsync le 2026-08-02).
            # Rattrapage d'un creux d'ordonnancement inclus (on repart de la grille courante à
            # chaque appel).
            idx = fill_w.next_index(lookahead=1)
            bobimxl.sleep_until_tai(fill_w.index_time_ns(idx))
        else:
            t0 = time.time(); idx = None   # cadence libre → compteur interne du writer
        with frames_lock:
            fb = frames["fill"]; kb = frames["key"]
        if INTERLACED:
            # éclate la TRAME en 2 champs → index CHAMP (pair = 1er champ) = 2*trame, 2*trame+1
            f0, f1 = _split_fields(fb); k0, k1 = _split_fields(kb)
            fi0 = (2 * idx) if idx is not None else None
            fi1 = (2 * idx + 1) if idx is not None else None
            _emit(fill_w, f0, fi0); _emit(key_w, k0, fi0)
            _emit(fill_w, f1, fi1); _emit(key_w, k1, fi1)
            gidx = fi1 if fi1 is not None else fill_w._counter - 1
        elif V_SLICE_H:
            # MODE TRANCHE : émission par bandes des trames déjà rendues (commit progressif).
            # Mêmes index de grain que l'historique — seul le commit change.
            gidx, gi_f, view_f = fill_w.open_grain(index=idx)
            _emit_grain_sliced(fill_w, gi_f, view_f, fb)
            _, gi_k, view_k = key_w.open_grain(index=idx if idx is not None else gidx)
            _emit_grain_sliced(key_w, gi_k, view_k, kb)
        else:
            # Écriture zéro-copie : on copie les octets planar dans la vue numpy du grain, commit.
            gidx, gi_f, view_f = fill_w.open_grain(index=idx)
            bobimxl.blit(view_f, np.frombuffer(fb, dtype=np.uint8))
            fill_w.commit(gi_f)
            _, gi_k, view_k = key_w.open_grain(index=idx if idx is not None else gidx)
            bobimxl.blit(view_k, np.frombuffer(kb, dtype=np.uint8))
            key_w.commit(gi_k)
        win_cnt += 1
        el = time.time() - win_start
        if el >= 1.0:
            with metrics_lock:
                metrics["fps"] = round(win_cnt / el, 1); metrics["frame_index"] = gidx
            win_cnt = 0; win_start = time.time()
        if not GENLOCK:
            dt = period - (time.time() - t0)
            if dt > 0: time.sleep(dt)


# ─── HTTP : :8080 metrics + :8082 contrôle ───────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock:
            body = json.dumps(dict(metrics)).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

class ControlHandler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {{}}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {{}}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/state":
            with state_lock: s = dict(state)
            with metrics_lock: m = dict(metrics)
            s.update({{"fps": m["fps"], "frame_index": m["frame_index"],
                       "media_root": MEDIA_ROOT, "plugin_version": PLUGIN_VERSION,
                       "fill_shm": FILL_SHM, "key_shm": KEY_SHM,
                       "width": WIDTH, "height": HEIGHT, "scan": SCAN,
                       "log_level": LOG_LEVEL,   # lisible en condition de macro
                       "mxl_lib": _mxl_lib_state()}})
            self._json(200, s)
        elif u.path == "/folders":
            self._json(200, {{"folders": _list_folders(), "media_root": MEDIA_ROOT}})
        elif u.path == "/images":
            folder = (q.get("folder") or [""])[0]
            self._json(200, {{"images": _list_images(folder), "folder": folder}})
        elif u.path == "/thumb":
            self._serve_thumb((q.get("path") or [""])[0])
        else:
            self._json(404, {{"error": "not found"}})

    def _serve_thumb(self, rel):
        full = _safe_join(rel)
        if not full or not os.path.isfile(full):
            self._json(404, {{"error": "introuvable"}}); return
        try:
            im = Image.open(full); im.thumbnail((160, 160))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (32, 36, 42))   # fond neutre sous l'alpha
                bg.paste(im, mask=im.split()[-1]); im = bg
            else:
                im = im.convert("RGB")
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=80)
            data = buf.getvalue()
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(500, {{"error": str(e)}})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/select":
            b = self._read()
            with state_lock:
                if "folder" in b: state["folder"] = (b.get("folder") or "").strip()
                state["file"] = (b.get("file") or state["file"] or "").strip()
                state["fit"]  = b.get("fit") or state["fit"]
                state["x"]    = float(b.get("x", state["x"]) or 0)
                state["y"]    = float(b.get("y", state["y"]) or 0)
                state["size"] = float(b.get("size", state["size"]) or 100)
                s = dict(state)
            ok, detail = _render(s["file"], s["fit"], s["x"], s["y"], s["size"])
            with metrics_lock:
                metrics["file"] = s["file"] if ok else metrics["file"]
            self._json(200 if ok else 400, {{"ok": ok, "detail": detail, "file": s["file"]}})
        elif u.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            b = self._read()
            ok = set_log_level(b.get("level") or b.get("log_level"))
            self._json(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})
        else:
            self._json(404, {{"error": "not found"}})
    def log_message(self, *a): pass


try:
    os.makedirs(MEDIA_ROOT, exist_ok=True)
except Exception:
    pass

threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(), daemon=True).start()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(), daemon=True).start()
threading.Thread(target=feeder, daemon=True).start()

# Rendu initial si une image est déjà configurée au déploiement.
if (state["file"] or "").strip():
    try:
        _render(state["file"], state["fit"], state["x"], state["y"], state["size"])
    except Exception as e:
        log("render initial: %s" % (e,), "warning")

while True:
    time.sleep(1)
