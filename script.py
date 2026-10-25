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
# Formes abrégées acceptées (« p », « i » : celles du mélangeur) — passées telles quelles à libmxl,
# elles faisaient échouer la création du flux en boucle (« Invalid interlace_mode: p », 2026-10-20).
SCAN = str(CONFIG.get("scan") or "progressive")
SCAN = {{"p": "progressive", "i": "interlaced_tff"}}.get(SCAN, SCAN)
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

def _write_field(vw, src, parity):
    """Copie le champ `parity` (0 = lignes paires) de la TRAME planar `src` (numpy uint8) DANS la
    vue du grain-champ `vw` : une copie strided par plan. L'ancien `_split_fields` (tobytes +
    concaténation + recopie, ~45 ms par image en 1080i 10 bits) tournait dans le thread de sortie
    au premier tick après un changement d'image : 15 créneaux sautés sur 10 changements, mesuré au
    banc le 2026-10-10 — et il aurait tourné à CHAQUE trame d'un fondu."""
    off = doff = 0
    for (psz, rb) in _PLANES:
        rows = psz // rb
        frows = (rows - parity + 1) // 2
        dst = vw[doff:doff + frows * rb]
        if dst.size == frows * rb:
            dst.reshape(frows, rb)[:] = src[off:off + psz].reshape(rows, rb)[parity::2]
        off += psz; doff += frows * rb

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
    # Fondu entre deux images (ms) ; 0 = coupe franche (comportement historique). Défaut du
    # réglage `transition_ms`, surchargé à chaque /select par `fade_ms`, ou à chaud par /transition.
    "transition_ms": max(0.0, float(CONFIG.get("transition_ms") or 0)),
    # MODE TAKE (comme un mélangeur) : choisir une image la met en PREVIEW, `/take` l'envoie à
    # l'antenne. `take_direct` = ancien comportement (choisir = antenne). Défaut : mode Take.
    "take_direct": _as_bool_cfg(CONFIG.get("take_direct"), default=False),
    "preview": {{"file": "", "fit": "native", "x": 0.0, "y": 0.0, "size": 100.0}},
}}

_BLACK_B = (16 << (BIT_DEPTH - 8)).to_bytes(_BPS, "little")   # Y noir studio (16/64/256)
_KEY0_B  = (0).to_bytes(_BPS, "little")                        # Y matte transparent (0)
_NEUT_B  = _NEUTRAL.to_bytes(_BPS, "little")                   # U/V neutre (128/512/2048)
def _black_fill():
    return (_BLACK_B * (WIDTH * HEIGHT)) + (_NEUT_B * (2 * UV_W * UV_H))
def _black_key():
    return (_KEY0_B * (WIDTH * HEIGHT)) + (_NEUT_B * (2 * UV_W * UV_H))

frames_lock = threading.Lock()
# fade : fondu en cours (None = aucun) — écarts précalculés, nombre de trames, trame courante.
frames = {{"fill": _black_fill(), "key": _black_key(), "fade": None}}

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


# ─── conversion RGB → YUV planar : BT.709 plage studio ───────────────────────────
# BT.709 parce que c'est ce qu'ANNONCE le flowDef (bobimxl.Writer, colorspace="BT709" par défaut,
# non surchargé ici). Jusqu'en 0.15.0 la matrice était BT.601 en dur : sur une sortie HD, rouges et
# verts décalés systématiquement, sans aucune trace. Coefficients dérivés de Kr = 0,2126,
# Kb = 0,0722 sur 8 bits studio (Y 16-235, C 16-240), puis mis à l'échelle de la profondeur.
def _rgb_to_yuv420(rgb):
    """rgb uint8 (H,W,3) → bytes Y + U + V au sous-échantillonnage du flux (CHROMA)."""
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    y = ( 0.18259 * r + 0.61423 * g + 0.06201 * b + 16.0)
    u = (-0.10064 * r - 0.33857 * g + 0.43922 * b + 128.0)
    v = ( 0.43922 * r - 0.39894 * g - 0.04027 * b + 128.0)
    # Chroma par MOYENNE des pixels regroupés, pas un sur deux : la décimation crénelait les
    # contours colorés (texte, logos) — précisément le contenu d'une image fixe.
    h, w = u.shape
    if _CH > 1 or _CW > 1:
        u = u[:h - h % _CH, :w - w % _CW].reshape(h // _CH, _CH, w // _CW, _CW).mean(axis=(1, 3))
        v = v[:h - h % _CH, :w - w % _CW].reshape(h // _CH, _CH, w // _CW, _CW).mean(axis=(1, 3))
    y_u8 = np.clip(y * _SCALE, 0, _MAXV).astype(_NP_DT)
    u_u8 = np.clip(u * _SCALE, 0, _MAXV).astype(_NP_DT)
    v_u8 = np.clip(v * _SCALE, 0, _MAXV).astype(_NP_DT)
    return y_u8.tobytes() + u_u8.tobytes() + v_u8.tobytes()

def _alpha_to_yuv420(alpha):
    """alpha uint8 (H,W) → key : Y=alpha (matte, mis à l'échelle N bits), U=V=neutre."""
    y = (np.asarray(alpha, dtype=np.uint16) * _SCALE).clip(0, _MAXV).astype(_NP_DT) if _DEEP \
        else np.ascontiguousarray(alpha, dtype=np.uint8)
    uv = np.full((UV_H, UV_W), _NEUTRAL, dtype=_NP_DT)
    return y.tobytes() + uv.tobytes() + uv.tobytes()


# ─── rendu de l'image courante → buffers Fill/Key ────────────────────────────
# ── Fondu : l'écart (nouvelle − ancienne) est calculé UNE fois, en float32, au départ du fondu ;
# chaque trame ne coûte plus qu'un a + d·w, découpé en bandes sur 3 threads (numpy relâche le GIL
# dans ses boucles). Mesuré : ~5,5 ms par plan 1080 10 bits 4:2:2, contre ~17 ms en int32 alloué
# à chaque trame — qui ne tenait pas les 20 ms d'une trame à 50 i/s (250 créneaux sautés sur dix
# fondus, banc du 2026-10-10).
from concurrent.futures import ThreadPoolExecutor
# Bandes = cœurs disponibles − 1 (un cœur reste au thread de sortie), plafonné à 3. Mesuré à
# 1080p50 avec des fondus de 500 ms : sans épinglage (cas réel de stills, qui ne déclare pas de
# profil de cœurs) 3 bandes → 0 créneau manqué ; épinglé sur 3 cœurs, 3 bandes → 6 à 57 manqués
# sur dix fondus (le thread de sortie se fait préempter), 2 bandes → 1.
try:
    _NCPU = len(os.sched_getaffinity(0))
except Exception:
    _NCPU = os.cpu_count() or 2
# Plafond porté à 6 le 2026-10-23 : Fill ET Key se mélangent ensemble sur le pool (une image à
# alpha fond les deux), mesuré sur dell-1 (nœud saturé en bande passante mémoire, 1080p 10 bits) :
# Fill + Key en 8,7 ms médiane, 14,5 ms au pire sur 6 fils, contre 13,7 / 20,7 ms sur 3.
_FADE_BANDS = max(1, min(6, _NCPU - 1))
_FADE_POOL = ThreadPoolExecutor(max_workers=_FADE_BANDS, thread_name_prefix="fondu")
# Bloc de calcul du fondu, en échantillons : son tampon float32 (512 Ko) reste en cache. Mesuré sur
# dell-1 le 2026-10-23 : un mélange plein cadre, avec son tampon intermédiaire de 16 Mo, puis
# recopié dans le grain coûtait 28 ms médiane par flux ; par blocs écrits DANS le grain, 7,5 ms.
_FADE_CHUNK = 1 << 17
_fade_tl = threading.local()


def _prep_fade(prev_bytes, new_bytes):
    """Départ (ancienne + 0,5 : l'arrondi y est déjà) et écart (nouvelle − ancienne), en float32,
    préparés PAR BANDES sur le pool : d'un bloc, la conversion de deux images de 8 Mo tenait le
    GIL assez longtemps pour faire sauter 2-3 créneaux au DÉPART de chaque fondu à 50 i/s.
    Rend (départ, écart, tampon de sortie réutilisé)."""
    pv = np.frombuffer(prev_bytes, dtype=_NP_DT); nv = np.frombuffer(new_bytes, dtype=_NP_DT)
    n = pv.size; step = n // _FADE_BANDS
    a = np.empty(n, np.float32); d = np.empty(n, np.float32)
    def _band(i):
        sl = slice(i * step, (i + 1) * step if i < _FADE_BANDS - 1 else n)
        np.copyto(a[sl], pv[sl]); np.copyto(d[sl], nv[sl]); np.subtract(d[sl], a[sl], out=d[sl])
        np.add(a[sl], 0.5, out=a[sl])
    list(_FADE_POOL.map(_band, range(_FADE_BANDS)))
    return (a, d, np.empty(n, _NP_DT))


def _fade_weight(fd):
    """Poids de la nouvelle image pour la trame courante du fondu `fd`."""
    return min(1.0, (fd["i"] + 1) / float(fd["n"]))


def _rows_span(r0, r1):
    """Plages d'ÉCHANTILLONS du payload planar couvrant les lignes image [r0, r1) sur les trois
    plans. `r0` et `r1` sont multiples de _CH (lignes chroma entières)."""
    ys, uvs = Y_SIZE // _BPS, UV_SIZE // _BPS
    c0, c1 = r0 // _CH, r1 // _CH
    return [(r0 * WIDTH, r1 * WIDTH), (ys + c0 * UV_W, ys + c1 * UV_W),
            (ys + uvs + c0 * UV_W, ys + uvs + c1 * UV_W)]


def _mix_range(pr, w, out, lo, hi):
    """out[lo:hi] = départ + écart·w, par blocs dont le tampon reste en cache : aucun tampon plein
    cadre, et `out` peut être directement le grain du bus."""
    a, d = pr[0], pr[1]
    t = getattr(_fade_tl, "buf", None)
    if t is None:
        t = _fade_tl.buf = np.empty(_FADE_CHUNK, np.float32)
    w = np.float32(w)
    for s in range(lo, hi, _FADE_CHUNK):
        e = min(hi, s + _FADE_CHUNK); b = t[:e - s]
        np.multiply(d[s:e], w, out=b); np.add(b, a[s:e], out=b)
        np.copyto(out[s:e], b, casting="unsafe")


def _mix_rows(pairs, w, r0, r1):
    """Mélange les lignes [r0, r1) de chaque (préparé, sortie) de `pairs`, ensemble, sur le pool."""
    spans = _rows_span(r0, r1)
    per = max(1, sum(hi - lo for lo, hi in spans) * len(pairs) // _FADE_BANDS)
    jobs = []
    for pr, out in pairs:
        for lo, hi in spans:
            for s in range(lo, hi, per):
                jobs.append((pr, out, s, min(hi, s + per)))
    list(_FADE_POOL.map(lambda j: _mix_range(j[0], w, j[1], j[2], j[3]), jobs))


def _mix_prepped(pr, w):
    """a + d·w arrondi, à la profondeur du flux. Rend une vue uint8 d'un tampon RÉUTILISÉ : la
    copier (bytes) si elle doit survivre à la trame suivante."""
    _mix_rows([(pr, pr[2])], w, 0, HEIGHT)
    return pr[2].view(np.uint8)


def _on_air(snap, private=False):
    """(fill, key) à l'antenne pour l'instantané `snap` de frames (pris sous verrou). `private` :
    calcul dans des tampons À SOI — sinon un appel hors du thread de sortie écrirait dans les
    tampons que celui-ci est en train de publier."""
    fd = snap.get("fade")
    if not fd:
        return snap["fill"], snap["key"]
    w = _fade_weight(fd)
    own = (lambda pr: (pr[0], pr[1], np.empty_like(pr[2]))) if private else (lambda pr: pr)
    f = _mix_prepped(own(fd["f"]), w)
    k = snap["key"] if fd["k"] is None else _mix_prepped(own(fd["k"]), w)
    return f, k


def _set_image(fill_bytes, key_bytes, fade_ms, rgba=None, meta=None):
    """Remplace l'image à l'antenne — d'un coup, ou par un fondu de `fade_ms` démarrant à la
    trame suivante. Remplissage ET clé changent ensemble, sous le même verrou ; tout le CALCUL se
    fait hors verrou (le thread de sortie l'attend à chaque trame)."""
    n = int(round(max(0.0, float(fade_ms or 0)) / 1000.0 * FPS_NUM / FPS_DEN))
    fade = None
    if n >= 2:
        with frames_lock:
            snap = dict(frames); snap["fade"] = dict(frames["fade"]) if frames["fade"] else None
        # Un fondu interrompu repart de ce qui est À L'ANTENNE maintenant, pas de son départ.
        cur_f, cur_k = _on_air(snap, private=True)
        cur_f, cur_k = bytes(cur_f), bytes(cur_k)
        fade = {{"f": _prep_fade(cur_f, fill_bytes),
                 # clé identique (images opaques : cas courant) → on ne mélange que le remplissage
                 "k": None if cur_k == key_bytes else _prep_fade(cur_k, key_bytes),
                 "n": n, "i": 0}}
    with frames_lock:
        frames["fade"] = fade
        frames["fill"] = fill_bytes; frames["key"] = key_bytes
        # Le JPEG « programme » dérive de ce qui vient d'être posé ; ancien état gardé pour le fondu.
        with prog_lock:
            prog["prev"] = prog["rgba"]; prog["rgba"] = rgba; prog["meta"] = meta
            prog["since"] = time.time(); prog["gen"] += 1


def _compose(file, fit, x, y, size):
    """Le CADRAGE — SEUL calcul de l'image de sortie, partagé par la sortie (`_render`) et la
    preview (`_do_preview`) : ce qu'on voit en preview EST ce qui passera, au pixel près.
    Rend (canvas RGBA uint8 (HEIGHT,WIDTH,4), meta) ou (None, message d'erreur)."""
    full = _safe_join(file)
    if not full or not os.path.isfile(full):
        return None, "fichier introuvable"
    try:
        img = Image.open(full)
        src_fmt = img.format or ""
        img = img.convert("RGBA")
    except Exception as e:
        return None, "décodage : %s" % e
    w0, h0 = img.size
    if w0 <= 0 or h0 <= 0:
        return None, "image vide"

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

    canvas = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    ox = int(round((WIDTH  - tw) / 2 + (float(x or 0) / 100.0) * WIDTH))
    oy = int(round((HEIGHT - th) / 2 + (float(y or 0) / 100.0) * HEIGHT))
    dx0, dy0 = max(0, ox), max(0, oy)
    dx1, dy1 = min(WIDTH, ox + tw), min(HEIGHT, oy + th)
    if dx1 > dx0 and dy1 > dy0:                         # portion visible (clipping)
        sx0, sy0 = dx0 - ox, dy0 - oy
        canvas[dy0:dy1, dx0:dx1, :] = src[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0), :]
    meta = {{"file": file, "name": os.path.basename(file), "src_w": w0, "src_h": h0,
             "format": src_fmt, "alpha": bool(src[:, :, 3].min() < 255),
             "fit": fit, "x": float(x or 0), "y": float(y or 0), "size": float(size or 100),
             "drawn_w": tw, "drawn_h": th, "out_w": WIDTH, "out_h": HEIGHT}}
    return canvas, meta


# ─── Programme / preview : JPEG à la demande ─────────────────────────────────
# Aucun encodage en continu : on encode UNE fois quand quelqu'un le demande, on met en cache, et
# on ne ré-encode qu'après un changement (`gen`). Pendant un fondu, le poids est quantifié par
# dixièmes (au plus ~10 encodages par fondu, seulement si l'UI regarde).
PV_W = 640
prog_lock = threading.Lock()
prog = {{"gen": 0, "rgba": None, "prev": None, "meta": None, "since": None}}
prev_lock = threading.Lock()
prev = {{"gen": 0, "rgba": None, "meta": None, "error": ""}}
_jpeg_cache = {{}}          # (origine, gen, vue, pas de fondu) → bytes ; une entrée par origine+vue
_jpeg_lock = threading.Lock()
_jpeg_stats = {{"encodes": 0}}
_checker_cache = {{}}


def _checker(w, h):
    k = (w, h)
    if k not in _checker_cache:
        yy, xx = np.mgrid[0:h, 0:w]
        v = np.where(((yy // 8) + (xx // 8)) % 2 == 0, 90, 140).astype(np.uint8)
        _checker_cache[k] = Image.fromarray(np.dstack([v, v, v, np.full_like(v, 255)]), "RGBA")
    return _checker_cache[k]


def _small(rgba):
    """Canvas RGBA plein format → vignette PV_W de large (RGBA)."""
    im = Image.fromarray(rgba, "RGBA")
    return im.resize((PV_W, max(2, round(PV_W * HEIGHT / WIDTH))), Image.BILINEAR)


def _jpeg_of(im, view):
    """vignette RGBA → JPEG. `fill` : l'image sur un DAMIER (la transparence se voit) ;
    `key` : l'alpha en gris (ce que porte le flux _key)."""
    if view == "key":
        out = im.getchannel("A").convert("RGB")
    else:
        out = Image.alpha_composite(_checker(*im.size), im).convert("RGB")
    buf = io.BytesIO(); out.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _cached_jpeg(origin, gen, view, step, make):
    key = (origin, view)
    with _jpeg_lock:
        hit = _jpeg_cache.get(key)
        if hit and hit[0] == (gen, step):
            return hit[1]
    data = make()
    with _jpeg_lock:
        _jpeg_cache[key] = ((gen, step), data)
        _jpeg_stats["encodes"] += 1
    return data


def _program_jpeg(view):
    with prog_lock:
        gen, cur, old, meta = prog["gen"], prog["rgba"], prog["prev"], prog["meta"]
    with frames_lock:
        fd = frames["fade"]
        w = min(1.0, (fd["i"] + 1) / float(fd["n"])) if fd else None
    step = None if w is None else int(round(w * 10))
    def make():
        black = np.zeros((HEIGHT, WIDTH, 4), np.uint8)
        c = cur if cur is not None else black
        if step is None:
            return _jpeg_of(_small(c), view)
        o = old if old is not None else black
        return _jpeg_of(Image.blend(_small(o), _small(c), step / 10.0), view)
    return _cached_jpeg("program", gen, view, step, make)


def _preview_jpeg(view):
    with prev_lock:
        gen, cur = prev["gen"], prev["rgba"]
    if cur is None:
        return None
    return _cached_jpeg("preview", gen, view, None, lambda: _jpeg_of(_small(cur), view))


def _do_preview(file, fit, x, y, size):
    """Prépare la preview : même cadrage que la sortie, RIEN n'est écrit sur le bus."""
    canvas, meta = _compose(file, fit, x, y, size)
    with state_lock:
        state["preview"] = {{"file": file, "fit": fit, "x": float(x or 0), "y": float(y or 0),
                             "size": float(size or 100)}}
    with prev_lock:
        prev["gen"] += 1
        prev["rgba"] = canvas
        prev["meta"] = meta if canvas is not None else None
        prev["error"] = "" if canvas is not None else meta
    return canvas is not None, ("ok" if canvas is not None else meta)


def _render(file, fit, x, y, size, fade_ms=None):
    if fade_ms is None:
        with state_lock:
            fade_ms = state["transition_ms"]
    canvas, meta = _compose(file, fit, x, y, size)
    if canvas is None:
        if meta == "fichier introuvable":
            _set_image(_black_fill(), _black_key(), fade_ms, None, None)
        return False, meta
    fill_bytes = _rgb_to_yuv420(canvas[:, :, :3])
    key_bytes  = _alpha_to_yuv420(canvas[:, :, 3])
    _set_image(fill_bytes, key_bytes, fade_ms, canvas, meta)
    return True, "ok"


def feeder():
    # Domaine MXL = $MXL_DOMAIN ou /dev/shm/mxl (sous-rép. du tmpfs bind-monté). Deux flux
    # planar nommés comme les anciens shm (les consommateurs MXL les retrouvent par le NOM).
    #
    # ★ L'INSTANCE SE FERME QUAND feeder() SORT. `_supervised` la relance à chaque exception ;
    # sans cette fermeture, chaque relance laissait une instance MXL ouverte, avec son fil de
    # surveillance et son descripteur inotify. Le 2026-10-20, un flux impossible à créer a fait
    # 955 relances en 82 min : le plafond inotify du NŒUD (1024) a été atteint, et plus aucun
    # conteneur MXL ne pouvait démarrer sur dell-1.
    inst = bobimxl.Instance()
    opened = []
    try:
        _feed(inst, opened)
    finally:
        for w in opened:
            try: w.close()
            except Exception: pass
        try: inst.close()
        except Exception: pass


def _feed(inst, opened):
    # Sortie déclarée entrelacée si SCAN=interlaced_* : libmxl dimensionne chaque grain à 1 CHAMP
    # (½ HEIGHT) et double la cadence en interne ; on écrit 2 grains-champs par trame.
    # MODE TRANCHE : les flowDef Fill/Key portent slice_height quand éligible (progressif +
    # hauteur divisible) → grains publiés en N tranches égales ; sinon inchangés (historique).
    _slkw = {{"slice_height": V_SLICE_H}} if V_SLICE_H else {{}}
    fill_w = bobimxl.Writer(inst, FILL_SHM, WIDTH, HEIGHT, CHROMA, BIT_DEPTH,
                            FPS_NUM, FPS_DEN, interlace=SCAN, **_slkw)
    opened.append(fill_w)
    key_w  = bobimxl.Writer(inst, KEY_SHM,  WIDTH, HEIGHT, CHROMA, BIT_DEPTH,
                            FPS_NUM, FPS_DEN, interlace=SCAN, **_slkw)
    opened.append(key_w)
    if SLICE_MODE:
        log("slice_mode %s : sorties %s/%s %s" % ("actif" if V_SLICE_H else "INÉLIGIBLE",
            FILL_SHM, KEY_SHM,
            ("tranchées par %d lignes (%d tranches)" % (V_SLICE_H, _V_TOTAL_SLICES))
            if V_SLICE_H else "(%s) → chemin historique"
            % ("entrelacé" if INTERLACED else "hauteur %d sans diviseur" % HEIGHT)), "info")
    win_cnt = 0; win_start = time.time()
    period = FPS_DEN / FPS_NUM
    # Créneaux de grille MANQUÉS, comptés ICI, par celui qui écrit (un écart d'index entre deux
    # trames successives) : c'est la seule mesure qui ne dépend pas d'un observateur qui pourrait
    # lui-même prendre du retard. Publié sur :8080 (`slots_missed`) — à comparer à l'intention.
    last_idx = None; missed = 0
    # Case de l'anneau → image qu'elle CONTIENT déjà (l'objet bytes lui-même, comparé par `is`).
    # Une image fixe n'a pas à être recopiée à chaque trame : la case de l'anneau la porte encore
    # depuis le tour précédent. Mesuré le 2026-10-23 sur dell-1 (stills-Satis, 1080p 10 bits) :
    # une copie de 8 Mo vers /dev/shm coûtait 5,8 ms en médiane, jusqu'à 9 ms (nœud saturé en
    # bande passante mémoire), soit 12 à 18 ms pour Fill + Key sur un budget de 20 ms, d'où 5 à
    # 12 créneaux ratés par seconde, alors que le réveil n'avait que 0,1 ms de retard. Pendant
    # un fondu, la sortie est un tampon RÉUTILISÉ dont le contenu change à chaque trame : on ne
    # mémorise rien. La clé est l'adresse de la case : une instance recréée repart à vide, car
    # `_feed` est relancé.
    held = {{}}

    def _held(view, payload, cacheable):
        """True si la case `view` porte déjà `payload` ; sinon la note comme le portant."""
        addr = view.ctypes.data
        if cacheable and held.get(addr) is payload:
            return True
        held[addr] = payload if cacheable else None
        return False

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
            if last_idx is not None and idx > last_idx + 1:
                missed += idx - last_idx - 1
            last_idx = idx
            # Réveil au DÉBUT du créneau, pas à son centre : `index_time_ns(X)` est le MILIEU du
            # créneau X (current_index passe à X 10,0 ms avant, mesuré sur dell-1 le 2026-10-25).
            # L'image est connue d'avance : dormir jusqu'au centre la publiait à 10,3 ms sur 20, et
            # un mélangeur « au plus rapide » abandonnait l'attente à la demi-période.
            t_idx = fill_w.index_time_ns(idx)
            bobimxl.sleep_until_tai(t_idx - (fill_w.index_time_ns(idx + 1) - t_idx) // 2)
        else:
            t0 = time.time(); idx = None   # cadence libre → compteur interne du writer
        with frames_lock:
            snap = {{"fill": frames["fill"], "key": frames["key"],
                     "fade": dict(frames["fade"]) if frames["fade"] else None}}
            if frames["fade"] is not None:
                frames["fade"]["i"] += 1
                if frames["fade"]["i"] >= frames["fade"]["n"]:
                    frames["fade"] = None
        fd = snap["fade"]
        still = fd is None                     # image fixe : objets bytes stables d'une trame à l'autre
        if INTERLACED:
            fb, kb = _on_air(snap)             # mélange HORS verrou
            # éclate la TRAME en 2 champs → index CHAMP (pair = 1er champ) = 2*trame, 2*trame+1
            fsrc = np.frombuffer(fb, dtype=np.uint8); ksrc = np.frombuffer(kb, dtype=np.uint8)
            first = 1 if FIELD_ORDER == "bff" else 0
            fi0 = (2 * idx) if idx is not None else None
            fi1 = (2 * idx + 1) if idx is not None else None
            for fi, par in ((fi0, first), (fi1, 1 - first)):
                for w_, src_ in ((fill_w, fsrc), (key_w, ksrc)):
                    _, gi_, vw_ = w_.open_grain(index=fi)
                    _write_field(vw_, src_, par)
                    w_.commit(gi_)
            gidx = fi1 if fi1 is not None else fill_w._counter - 1
        elif fd is not None:
            # FONDU : le mélange s'écrit DIRECTEMENT dans les grains, Fill et Key ensemble sur le
            # pool — ni tampon plein cadre, ni recopie. Une clé inchangée (image opaque) n'est
            # pas mélangée : elle passe par le cache des cases comme une image fixe. En mode
            # tranche, le grain est publié par tiers (commit progressif), pas d'un bloc.
            w = _fade_weight(fd)
            gidx, gi_f, view_f = fill_w.open_grain(index=idx)
            _, gi_k, view_k = key_w.open_grain(index=idx if idx is not None else gidx)
            _held(view_f, None, False)
            pairs = [(fd["f"], view_f.view(_NP_DT))]
            if fd["k"] is None:
                if not _held(view_k, snap["key"], True):
                    bobimxl.blit(view_k, np.frombuffer(snap["key"], dtype=np.uint8))
            else:
                _held(view_k, None, False)
                pairs.append((fd["k"], view_k.view(_NP_DT)))
            if V_SLICE_H:
                step = V_SLICE_H * -(-_V_TOTAL_SLICES // 3)
                for r1 in range(step, HEIGHT, step):
                    _mix_rows(pairs, w, r1 - step, r1)
                    for w_, gi_ in ((fill_w, gi_f), (key_w, gi_k)):
                        w_.commit(gi_, valid_slices=r1 // V_SLICE_H)
                _mix_rows(pairs, w, (HEIGHT - 1) // step * step, HEIGHT)
            else:
                _mix_rows(pairs, w, 0, HEIGHT)
            fill_w.commit(gi_f)
            key_w.commit(gi_k)
        elif V_SLICE_H:
            # MODE TRANCHE : émission par bandes des trames déjà rendues (commit progressif).
            # Mêmes index de grain que l'historique — seul le commit change.
            fb, kb = snap["fill"], snap["key"]
            gidx, gi_f, view_f = fill_w.open_grain(index=idx)
            if _held(view_f, fb, still):
                fill_w.commit(gi_f)
            else:
                _emit_grain_sliced(fill_w, gi_f, view_f, fb)
            _, gi_k, view_k = key_w.open_grain(index=idx if idx is not None else gidx)
            if _held(view_k, kb, still):
                key_w.commit(gi_k)
            else:
                _emit_grain_sliced(key_w, gi_k, view_k, kb)
        else:
            # Écriture zéro-copie : on copie les octets planar dans la vue numpy du grain, commit.
            fb, kb = snap["fill"], snap["key"]
            gidx, gi_f, view_f = fill_w.open_grain(index=idx)
            if not _held(view_f, fb, still):
                bobimxl.blit(view_f, np.frombuffer(fb, dtype=np.uint8))
            fill_w.commit(gi_f)
            _, gi_k, view_k = key_w.open_grain(index=idx if idx is not None else gidx)
            if not _held(view_k, kb, still):
                bobimxl.blit(view_k, np.frombuffer(kb, dtype=np.uint8))
            key_w.commit(gi_k)
        win_cnt += 1
        el = time.time() - win_start
        # frame_index à CHAQUE trame : publié une fois par fenêtre d'une seconde, il datait de 0 à
        # ~1 s au moment où on le lisait, et un débit calculé dessus sur 30 s affichait 50,59 i/s
        # pour 49,999 réels (relevé du 2026-10-23 sur stills-Satis).
        with metrics_lock:
            metrics["frame_index"] = gidx
        if el >= 1.0:
            with metrics_lock:
                metrics["fps"] = round(win_cnt / el, 1)
                metrics["slots_missed"] = missed
            win_cnt = 0; win_start = time.time()
        if not GENLOCK:
            dt = period - (time.time() - t0)
            if dt > 0: time.sleep(dt)


# ─── HTTP : :8080 metrics + :8082 contrôle ───────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock:
            payload = dict(metrics)
        # Avance des index émis sur la grille TAI (bobimxl.index_report, grid_ms en ms signées, positif =
        # futur) : seul témoin d'une sortie écrite dans le futur, la cadence restant parfaite.
        payload["index_ahead"] = bobimxl.index_report()
        body = json.dumps(payload).encode()
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
                       "fading": frames["fade"] is not None,   # fondu en cours (condition de macro)
                       "program_file": s.get("file", ""),      # image À L'ANTENNE
                       "preview_file": (s.get("preview") or {{}}).get("file", ""),   # image en PREVIEW
                       "on_air_since": prog["since"], "program_gen": prog["gen"],
                       "program_meta": prog["meta"], "preview_meta": prev["meta"],
                       "preview_error": prev["error"], "preview_gen": prev["gen"],
                       "jpeg_encodes": _jpeg_stats["encodes"],   # preuve : pas d'encodage en continu
                       "mxl_lib": _mxl_lib_state()}})
            self._json(200, s)
        elif u.path == "/folders":
            self._json(200, {{"folders": _list_folders(), "media_root": MEDIA_ROOT}})
        elif u.path == "/images":
            folder = (q.get("folder") or [""])[0]
            self._json(200, {{"images": _list_images(folder), "folder": folder}})
        elif u.path == "/thumb":
            self._serve_thumb((q.get("path") or [""])[0])
        elif u.path in ("/program.jpg", "/preview.jpg"):
            view = "key" if (q.get("view") or [""])[0] == "key" else "fill"
            try:
                data = _program_jpeg(view) if u.path == "/program.jpg" else _preview_jpeg(view)
            except Exception as e:
                return self._json(500, {{"error": str(e)}})
            if data is None:
                self.send_response(204); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
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

    def _go_air(self, b, force_fade=False):
        """Met l'image à l'ANTENNE (corps de /select, /take, /cut). Même chemin pour les trois :
        la sortie ne connaît qu'`_render`. Un changement de cadrage de la MÊME image reste une
        coupe (le fondu ne vaut que pour un changement d'image), sauf `force_fade` (Take/Cut,
        où la durée est explicite)."""
        with state_lock:
            prev_file = state["file"]
            if "folder" in b: state["folder"] = (b.get("folder") or "").strip()
            state["file"] = (b.get("file") or state["file"] or "").strip()
            state["fit"]  = b.get("fit") or state["fit"]
            state["x"]    = float(b.get("x", state["x"]) or 0)
            state["y"]    = float(b.get("y", state["y"]) or 0)
            state["size"] = float(b.get("size", state["size"]) or 100)
            s = dict(state)
        fade = b.get("fade_ms")
        try:
            fade = None if fade is None or fade == "" else max(0.0, min(10000.0, float(fade)))
        except (TypeError, ValueError):
            return self._json(400, {{"error": "fade_ms : nombre de millisecondes attendu"}})
        if fade is None and s["file"] == prev_file and not force_fade:
            fade = 0.0
        ok, detail = _render(s["file"], s["fit"], s["x"], s["y"], s["size"], fade_ms=fade)
        with metrics_lock:
            metrics["file"] = s["file"] if ok else metrics["file"]
        self._json(200 if ok else 400, {{"ok": ok, "detail": detail, "file": s["file"]}})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/select":
            b = self._read()
            self._go_air(b)
        elif u.path == "/preview":
            b = self._read()
            with state_lock:
                pv = dict(state["preview"])
                for k in ("file", "fit"):
                    if k in b and b[k] is not None: pv[k] = str(b[k]).strip()
                for k in ("x", "y", "size"):
                    if k in b and b[k] not in (None, ""):
                        try: pv[k] = float(b[k])
                        except (TypeError, ValueError): return self._json(400, {{"error": "%s : nombre attendu" % k}})
            if not pv["file"]:
                return self._json(400, {{"ok": False, "detail": "aucune image"}})
            ok, detail = _do_preview(pv["file"], pv["fit"], pv["x"], pv["y"], pv["size"])
            self._json(200 if ok else 400, {{"ok": ok, "detail": detail, "preview": pv}})
        elif u.path in ("/take", "/cut"):
            b = self._read()
            with state_lock: pv = dict(state["preview"])
            if not pv["file"]:
                return self._json(400, {{"ok": False, "detail": "rien en preview"}})
            body = dict(pv)
            body["fade_ms"] = 0 if u.path == "/cut" else b.get("fade_ms")
            self._go_air(body, force_fade=True)
        elif u.path == "/take_direct":
            b = self._read()
            v = b.get("enabled", b.get("take_direct"))
            v = _as_bool_cfg(v, default=False)
            with state_lock: state["take_direct"] = v
            self._json(200, {{"ok": True, "take_direct": v}})
        elif u.path == "/transition":
            # Durée de fondu PAR DÉFAUT, à chaud (0 = coupe franche). Le réglage persistant est
            # `transition_ms` du config_schema.
            b = self._read()
            try:
                v = max(0.0, min(10000.0, float(b.get("ms", b.get("transition_ms", 0)) or 0)))
            except (TypeError, ValueError):
                return self._json(400, {{"error": "ms : nombre attendu"}})
            with state_lock: state["transition_ms"] = v
            self._json(200, {{"ok": True, "transition_ms": v}})
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
def _supervised(fn):
    """Relance le feeder s'il lève : une exception tuait le thread sans un mot, la sortie se
    figeait et le conteneur restait « up ». Compteur `feeder_restarts` publié sur :8080."""
    def _run():
        while True:
            try:
                fn()
            except Exception:
                import traceback
                log("%s : exception — relance dans 1 s\n%s" % (fn.__name__, traceback.format_exc()), "error")
                with metrics_lock:
                    metrics["feeder_restarts"] = metrics.get("feeder_restarts", 0) + 1
            time.sleep(1)
    threading.Thread(target=_run, name=fn.__name__, daemon=True).start()

_supervised(feeder)

# Rendu initial si une image est déjà configurée au déploiement.
if (state["file"] or "").strip():
    try:
        _render(state["file"], state["fit"], state["x"], state["y"], state["size"])
        _do_preview(state["file"], state["fit"], state["x"], state["y"], state["size"])
    except Exception as e:
        log("render initial: %s" % (e,), "warning")

while True:
    time.sleep(1)
