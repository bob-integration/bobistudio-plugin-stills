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
import os, io, time, json, threading, urllib.parse, shutil, subprocess, tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
from PIL import Image, ImageOps
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
# AUX outputs (0.19.14): 0 to 4 extra Fill + Key pairs, each holding its own image on air,
# outside PVW/PGM. Their number is a deployment setting (new flows: redeploy).
try:
    AUX_N = max(0, min(4, int(CONFIG.get("aux_outputs") or 0)))
except (TypeError, ValueError):
    AUX_N = 0
AUX_SHM = [(HOSTNAME + "_aux%d" % (i + 1), HOSTNAME + "_aux%d_key" % (i + 1)) for i in range(AUX_N)]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".heic", ".heif")
HEIF_EXTS = (".heic", ".heif")

# HEIC/HEIF (iPhone photos, 0.19.25): Pillow does not read them. Two decoders are accepted, whichever
# the runtime image carries: the pillow-heif opener, or libheif's `heif-dec` command (Debian
# libheif-examples). Neither: the file is refused with a message that says why, not "decoding failed".
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_DECODER = "pillow-heif"
except Exception:
    HEIF_DECODER = "heif-dec" if shutil.which("heif-dec") else ""

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE = Image.LANCZOS

# ─── état + buffers courants ─────────────────────────────────────────────────
def _norm_frame(d):
    """An image and its framing, cleaned: {{file, fit, x, y, size}} (empty file = no image)."""
    d = d if isinstance(d, dict) else {{}}
    try:
        return {{"file": str(d.get("file") or "").strip(),
                 "fit": d.get("fit") if d.get("fit") in ("native", "fit", "stretch") else "fit",
                 "x": float(d.get("x") or 0), "y": float(d.get("y") or 0),
                 "size": float(d.get("size") or 100)}}
    except (TypeError, ValueError):
        return {{"file": "", "fit": "fit", "x": 0.0, "y": 0.0, "size": 100.0}}


_PERSISTED = CONFIG.get("stills_state") if isinstance(CONFIG.get("stills_state"), dict) else {{}}
state_lock = threading.Lock()
state = {{
    "folder": CONFIG.get("folder") or "",
    "file":   CONFIG.get("file") or "",
    "fit":    CONFIG.get("fit") or "fit",      # native | fit | stretch (default "fit" since 0.19.25)
    "x":      float(CONFIG.get("x") or 0),      # décalage % de la largeur (centré)
    "y":      float(CONFIG.get("y") or 0),      # décalage % de la hauteur (centré)
    "size":   float(CONFIG.get("size") or 100), # échelle %
    # Fondu entre deux images (ms) ; 0 = coupe franche (comportement historique). Défaut du
    # réglage `transition_ms`, surchargé à chaque /select par `fade_ms`, ou à chaud par /transition.
    "transition_ms": max(0.0, float(CONFIG["transition_ms"]) if CONFIG.get("transition_ms") not in (None, "") else 150.0),
    # Type de transition (fondu, additif, volet) et réglage du volet : réglés à chaud (/transition),
    # persistés avec l'image par défaut et les AUX (`stills_state.transition`). Normalisés plus bas.
    "transition": dict({{"type": "dissolve", "wipe": {{}}}}, **dict(_PERSISTED.get("transition") or {{}})),
    # MODE TAKE (comme un mélangeur) : choisir une image la met en PREVIEW, `/take` l'envoie à
    # l'antenne. `take_direct` = ancien comportement (choisir = antenne). Défaut : mode Take.
    "take_direct": _as_bool_cfg(CONFIG.get("take_direct"), default=False),
    "preview": {{"file": "", "fit": "fit", "x": 0.0, "y": 0.0, "size": 100.0}},
    # Default image, recalled in one gesture (key, macro), and the AUX images. Persisted by the
    # orchestrator (`control.persist`, param `stills_state`): set here, they survive a redeploy.
    "default": _norm_frame(_PERSISTED.get("default") or CONFIG.get("default_image")),
    "aux": [_norm_frame(x) for x in (list(_PERSISTED.get("aux") or []) + [None] * AUX_N)[:AUX_N]],
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

# ─── Noyau C de conversion (libbobi_stills, script_templates/stillskernel.c, 2026-11-15) ─────────
# Même calcul que _rgb_to_yuv420 / _alpha_to_yuv420 ci-dessus, en une passe et sur plusieurs cœurs :
# préparer une image 1080p coûtait 89 ms de conversion en numpy. Bibliothèque absente (image
# d'exécution plus ancienne) : les fonctions numpy restent le chemin, rien ne change.
import ctypes as _ct
_SK = None
try:
    _ldr = getattr(bobimxl, "charger_noyau", None)
    if _ldr is not None:
        _vp, _i = _ct.c_void_p, _ct.c_int
        _SK = _ldr("stills", {{"bobi_stills_fill_u8": [_vp, _i, _i, _vp, _vp, _vp, _i, _i, _i, _i],
                               "bobi_stills_fill_u16": [_vp, _i, _i, _vp, _vp, _vp, _i, _i, _i, _i],
                               "bobi_stills_key_u8": [_vp, _i, _i, _vp, _vp, _i, _i, _i, _i],
                               "bobi_stills_key_u16": [_vp, _i, _i, _vp, _vp, _i, _i, _i, _i]}})
        if _SK is not None:
            _SK.bobi_stills_fill_u8.restype = _SK.bobi_stills_fill_u16.restype = _i
except Exception as _e:
    print("stills: kernel unavailable (%s) — numpy conversion" % (_e,), flush=True)
    _SK = None

# Dithered dissolve (libbobi_stills ABI 2, compute image >= 0.83, 2026-11-15). Between two similar
# images a pixel moves a few code values over the whole fade: in 8 bits it steps 0, 1 or 2 codes per
# frame, and pixels with similar differences step on the SAME frame — a jerky fade, measured on a
# tester's recording. A fixed per-pixel threshold in [0, 1) spreads those steps. Older library:
# _DITHER stays None and the fade mixes as before (mvk, truncated).
_DITHER = None
if _SK is not None and hasattr(_SK, "bobi_stills_mixd_u8"):
    try:
        _pd, _dbl = _ct.c_ssize_t, _ct.c_double
        for _fn in (_SK.bobi_stills_mixd_u8, _SK.bobi_stills_mixd_u16):
            _fn.restype = None
            _fn.argtypes = [_vp, _pd, _vp, _pd, _vp, _pd, _i, _i, _dbl, _dbl, _vp, _i]
        # Every threshold of the 4096 occurs once per tile: an unbiased spread, no pattern drift.
        _DITHER = ((np.random.default_rng(0x5715).permutation(4096).astype(np.float32) + 0.5)
                   / np.float32(4096)).reshape(64, 64)
    except Exception as _e:
        print("stills: dithered mix unavailable (%s)" % (_e,), flush=True)
        _DITHER = None
metrics["fade_dither"] = _DITHER is not None


def _mixd_into(o, a, b, fa, fb):
    """o ← clamp(trunc(a·fa + b·fb + threshold)) by the C kernel. False when not applicable."""
    if _DITHER is None or o.dtype != _NP_DT or not (o.shape == a.shape == b.shape) or o.ndim != 2:
        return False
    for t in (o, a, b):
        if t.strides[1] != t.itemsize or t.strides[0] % t.itemsize:
            return False
    fn = _SK.bobi_stills_mixd_u16 if o.dtype == np.uint16 else _SK.bobi_stills_mixd_u8
    fn(o.ctypes.data, o.strides[0] // o.itemsize, a.ctypes.data, a.strides[0] // a.itemsize,
       b.ctypes.data, b.strides[0] // b.itemsize, o.shape[0], o.shape[1], float(fa), float(fb),
       _DITHER.ctypes.data, int(_MAXV))
    return True


def _canvas_to_yuv(canvas):
    """Canvas RGBA uint8 (HEIGHT, WIDTH, 4) → (Fill, Key) en octets planar, prêts pour le bus."""
    if _SK is not None and canvas.dtype == np.uint8 and canvas.shape == (HEIGHT, WIDTH, 4):
        rgba = np.ascontiguousarray(canvas)
        f = np.empty(V_FRAME_SIZE // _BPS, _NP_DT); k = np.empty(V_FRAME_SIZE // _BPS, _NP_DT)
        n_y = WIDTH * HEIGHT; n_uv = UV_W * UV_H
        a = f.ctypes.data; isz = f.itemsize
        fill = _SK.bobi_stills_fill_u16 if _DEEP else _SK.bobi_stills_fill_u8
        key = _SK.bobi_stills_key_u16 if _DEEP else _SK.bobi_stills_key_u8
        if fill(rgba.ctypes.data, HEIGHT, WIDTH, a, a + n_y * isz, a + (n_y + n_uv) * isz,
                _CW, _CH, _SCALE, _MAXV) == 0:
            key(rgba.ctypes.data, HEIGHT, WIDTH, k.ctypes.data, k.ctypes.data + n_y * isz, n_uv,
                _SCALE, _MAXV, _NEUTRAL)
            return f.tobytes(), k.tobytes()
    return _rgb_to_yuv420(canvas[:, :, :3]), _alpha_to_yuv420(canvas[:, :, 3])


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


# ─── TRANSITIONS PAR LE NOYAU C (0.19.18, 2026-11-15) ────────────────────────────────────────
# Fondu et volets passent par le noyau du mélangeur (libbobi_mvk, déjà dans l'image d'exécution :
# bobimxl.mvk_mixf_into / mvk_mixmap_into) : UNE passe par plan, de l'ancienne et de la nouvelle image
# telles quelles vers le grain, en C et sur plusieurs cœurs, hors du verrou de Python. Plus de
# préparation au départ (deux tableaux flottants de 16 Mo par plan) : le départ du fondu ne coûte
# plus rien. Noyau absent (image trop ancienne) : le fondu garde son calcul numpy d'avant, le volet
# un repli numpy.
_MVK = bool(getattr(bobimxl, "mvk_available", lambda: False)())
# « additive » : comme le mélangeur (blend_yuv_additive) — à mi-course les deux luminances
# s'ajoutent (écrêtées), la chrominance reste un fondu pondéré ; la découpe (Key) reste un fondu.
TRANSITION_TYPES = ("dissolve", "additive", "wipe")
WIPE_DEFAULT = dict(direction="linear", angle=0.0, softness=0.10, reverse=False)
_Y_N = WIDTH * HEIGHT
_UV_N = UV_W * UV_H


def _planes(buf):
    """(Y, U, V) : vues 2D en échantillons d'une trame planar (bytes ou tableau)."""
    a = np.frombuffer(buf, dtype=_NP_DT) if isinstance(buf, (bytes, bytearray, memoryview)) else buf.view(_NP_DT)
    return (a[:_Y_N].reshape(HEIGHT, WIDTH), a[_Y_N:_Y_N + _UV_N].reshape(UV_H, UV_W),
            a[_Y_N + _UV_N:_Y_N + 2 * _UV_N].reshape(UV_H, UV_W))


def _mix_ends(pairs, w, m, r0, r1):
    """Lignes [r0, r1) de chaque (ancienne, nouvelle, sortie[, additif]) — plans (Y, U, V) — de
    `pairs` : fondu de poids `w` (m None), additif sur la luminance si demandé, ou volet de masque
    `m` (part de la nouvelle, plein cadre)."""
    c0, c1 = r0 // _CH, -(-r1 // _CH)
    for A, B, O, *opt in pairs:
        add = bool(opt and opt[0])
        for k in range(3):
            lo, hi = (r0, r1) if k == 0 else (c0, c1)
            a, b, o = A[k][lo:hi], B[k][lo:hi], O[k][lo:hi]
            if m is None and add and k == 0:
                ca, cb = min(1.0, 2.0 * (1.0 - w)), min(1.0, 2.0 * w)
                if _mixd_into(o, a, b, ca, cb):
                    continue
                if _MVK and bobimxl.mvk_mixf_into(o, a, b, ca, cb, clip=True, maxv=_MAXV):
                    continue
                o[...] = np.clip(a.astype(np.float32) * ca + b.astype(np.float32) * cb, 0, _MAXV).astype(_NP_DT)
                continue
            if m is None:
                if _mixd_into(o, a, b, 1.0 - w, w):
                    continue
                if _MVK and bobimxl.mvk_mixf_into(o, a, b, 1.0 - w, w):
                    continue
                mm = np.float32(w)
            else:
                mm, cs = (m[lo:hi], 1) if k == 0 else (m[lo * _CH:hi * _CH:_CH], _CW)
                if _MVK and bobimxl.mvk_mixmap_into(o, a, b, mm, m_colstep=cs):
                    continue
                mm = mm[:, ::cs] if cs > 1 else mm
            o[...] = (a.astype(np.float32) * (1.0 - mm) + b.astype(np.float32) * mm).astype(_NP_DT)


def _wipe_norm_s(raw):
    """Réglage de volet du Still : forme (noms du mélangeur), angle, douceur, sens — bornés."""
    w = dict(WIPE_DEFAULT)
    raw = raw if isinstance(raw, dict) else {{}}
    d = str(raw.get("direction") or w["direction"]).strip().lower()
    w["direction"] = d if d in WIPE_NAMES else "linear"
    try:
        w["angle"] = float(raw.get("angle", w["angle"]) or 0.0) % 360.0
    except (TypeError, ValueError):
        pass
    try:
        w["softness"] = max(0.0, min(1.0, float(raw.get("softness", w["softness"]))))
    except (TypeError, ValueError):
        pass
    w["reverse"] = _as_bool_cfg(raw.get("reverse"), default=False)
    return w


def _wipe_mask(w, alpha, owner):
    """Part de la nouvelle image pour une trame de volet (course du bord comme au mélangeur :
    α = 0 → tout l'ancienne, α = 1 → toute la nouvelle, quelle que soit la forme)."""
    d, M, px, tl = _wipe_field(_wipe_geom(w))
    s = max(px, float(w.get("softness", 0.10) or 0.0) * M)
    mg = 0.01 * s
    e = (-0.5 * s - mg) + max(0.0, min(1.0, alpha)) * (M + s + 2.0 * mg)
    if owner is None:
        return _ramp_into(np.empty((HEIGHT, WIDTH), np.float32), d, e, s, M, bool(w.get("reverse")), tl, False)
    return _ramp_into(_wipe_buf(owner, 0), d, e, s, M, bool(w.get("reverse")), tl, True)


def _field_ready(w, wait_s=0.3):
    """Le champ du volet est-il prêt ? Sinon on le lance en tâche de fond et on l'attend au plus
    `wait_s` SANS verrou (la sortie continue) — comme le départ d'un volet du mélangeur."""
    geom = _wipe_geom(w)
    with _wipe_fields_lock:
        if (geom, False) in _wipe_fields:
            return True
    _wipe_prefetch(w)
    t_end = time.monotonic() + wait_s
    while time.monotonic() < t_end:
        time.sleep(0.005)
        with _wipe_fields_lock:
            if (geom, False) in _wipe_fields:
                return True
    return False


# ─── Géométrie des volets : REPRISE DU MÉLANGEUR (plugins/mixer/script.py, 0.23.x) ─────────────
# Mêmes formes, même champ de distance, même rampe par tuiles : un volet du Still est le volet du
# mélangeur, au pixel près. À factoriser dans bobimxl (commun aux deux) à la prochaine version du
# cœur ; d'ici là, toute correction se fait des DEUX côtés.
WIPE_SHAPES = ("linear", "iris", "box", "diamond", "star", "heart", "clock")
# Les trois anciens noms sont des volets LINÉAIRES à angle prédéfini : acceptés partout (presets
# persistés, macros, ATEM), normalisés en « linear » + angle là où l'angle se règle.
WIPE_LEGACY_ANGLES = {{"horizontal": 0.0, "vertical": 90.0, "diagonal": 45.0}}
WIPE_NAMES = ("horizontal", "vertical", "diagonal") + WIPE_SHAPES
WIPE_REPEAT_MAX = 16


_STAR_K = 0.381966          # rayon intérieur / extérieur de l'étoile régulière à 5 branches


def _wipe_geom(w):
    """Clé de GÉOMÉTRIE d'un réglage (forme, angle effectif, centre, symétrie, répétition).
    Un ancien nom donne son angle prédéfini, auquel s'ajoute `angle` s'il y en a un (AUTO d'un
    keyer : pas d'angle propre, le nom suffit)."""
    name = str(w.get("direction") or "linear").strip().lower()
    shape = "linear" if name in WIPE_LEGACY_ANGLES else (name if name in WIPE_SHAPES else "linear")
    try:
        ang = (WIPE_LEGACY_ANGLES.get(name, 0.0) + float(w.get("angle", 0.0) or 0.0)) % 360.0
    except Exception:
        ang = WIPE_LEGACY_ANGLES.get(name, 0.0)
    def _f(k, dflt):
        try:
            return round(max(0.0, min(1.0, float(w.get(k, dflt)))), 4)
        except Exception:
            return dflt
    try:
        rep = max(1, min(WIPE_REPEAT_MAX, int(w.get("repeat", 1) or 1)))
    except Exception:
        rep = 1
    return (shape, round(ang, 3), _f("center_x", 0.5), _f("center_y", 0.5),
            bool(w.get("symmetry")), rep)


def _rot_cs(ang):
    """(cos, sin) de l'angle du volet — angles droits EXACTS (cos 90° ≠ 0 en flottant)."""
    q = ang % 360.0
    exact = {{0.0: (1.0, 0.0), 90.0: (0.0, 1.0), 180.0: (-1.0, 0.0), 270.0: (0.0, -1.0)}}
    return exact.get(q, (float(np.cos(np.radians(q))), float(np.sin(np.radians(q)))))


def _rot_coords(cx, cy, c, s, r0, r1):
    """Coordonnées (u, v) des centres de pixel des lignes [r0, r1), relatives à (cx, cy) en pixels,
    tournées de l'angle (c, s) (sens horaire à l'écran, y vers le bas) : u le long de l'axe du
    volet, v en travers."""
    xs = (np.arange(WIDTH, dtype=np.float32) + np.float32(0.5 - cx))[None, :]
    ys = (np.arange(r0, r1, dtype=np.float32) + np.float32(0.5 - cy))[:, None]
    u = xs * np.float32(c) + ys * np.float32(s)
    v = ys * np.float32(c) - xs * np.float32(s)
    return u, v


_heart_tab = []     # [(angles, rayons, rayon mini)] — table polaire du cœur, calculée une fois


def _heart_table():
    """Rayon du bord du cœur (X² + Y² − 1)³ − X²·Y³ = 0 dans chaque direction (Y vers le haut) :
    1024 directions, premier passage par la frontière le long du rayon (forme étoilée autour de
    l'origine). Quelques ms, une fois — remplace le précalcul plein cadre en tâche de fond."""
    if _heart_tab:
        return _heart_tab[0]
    n = 1024
    phi = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    t = np.linspace(0.0005, 2.0, 4000)[:, None]
    X = t * np.cos(phi)[None, :]
    Y = t * np.sin(phi)[None, :]
    q = X * X + Y * Y - 1.0
    f = q * q * q - X * X * Y * Y * Y
    i = np.argmax(f >= 0.0, axis=0)
    R = t[i, 0]
    # prolongée d'un tour de chaque côté : np.interp sans couture en 0 / 2π
    ph = np.concatenate([phi - 2.0 * np.pi, phi, phi + 2.0 * np.pi])
    rr = np.concatenate([R, R, R])
    _heart_tab.append((ph, rr, float(R.min())))
    return _heart_tab[0]


def _shape_dist(shape, u, v, hx, hy, sym):
    """Distance NORMALISÉE d'une forme centrée (1 = la forme couvre le rectangle hx × hy, ou le
    cercle de la demi-diagonale pour les formes rondes). Pas le linéaire (cf. appelants)."""
    rd = float(np.hypot(hx, hy))
    if shape == "box":
        return np.maximum(np.abs(u) * np.float32(1.0 / hx), np.abs(v) * np.float32(1.0 / hy))
    if shape == "diamond":
        return (np.abs(u) * np.float32(0.5 / hx) + np.abs(v) * np.float32(0.5 / hy))
    r = np.sqrt(u * u + v * v)
    if shape == "iris":
        return r * np.float32(1.0 / rd)
    if shape == "clock":
        # Balayage angulaire à partir du HAUT, sens horaire (symétrie : les deux sens à la fois).
        ph = np.arctan2(u, -v)
        if sym:
            return np.abs(ph) * np.float32(1.0 / np.pi)
        return np.mod(ph, np.float32(2.0 * np.pi)) * np.float32(0.5 / np.pi)
    if shape == "star":
        ph = np.mod(np.arctan2(u, -v), np.float32(2.0 * np.pi / 5.0))
        ph = np.minimum(ph, np.float32(2.0 * np.pi / 5.0) - ph)
        k = _STAR_K; c0 = float(np.cos(np.pi / 5.0)); s0 = float(np.sin(np.pi / 5.0))
        rho = np.float32(k * s0) / (np.float32(k * s0) * np.cos(ph) + np.float32(1.0 - k * c0) * np.sin(ph))
        return r * np.float32(k / rd) / rho
    # heart
    ph, rr, rmin = _heart_table()
    phi = np.mod(np.arctan2(-v, u), np.float32(2.0 * np.pi))
    R = np.interp(phi.ravel(), ph, rr).astype(np.float32).reshape(phi.shape)
    return r * np.float32(rmin / rd) / R


def _shape_px(shape, hx, hy):
    """Un pixel, en unités de d (douceur minimale : un bord net reste anti-crénelé)."""
    if shape in ("box", "diamond"):
        return 1.0 / min(hx, hy)
    if shape == "clock":
        return 0.002
    return 1.0 / float(np.hypot(hx, hy))


_wipe_fields = dict()
_wipe_fields_lock = threading.Lock()
_WIPE_FIELDS_MAX = 6
_FIELD_ROWS = 64        # calcul par paquets de lignes : jamais un long calcul d'un seul tenant
_field_cost_ms = dict()  # forme → durée MESURÉE du dernier calcul de champ (cf. _pattern_maps)


def _field_rows(shape, cx, cy, c, s, sym, rep, umin, r0, r1, pattern=False):
    """Distance normalisée des lignes [r0, r1) (avant recalage du minimum) et un pixel en unités de d.
    `pattern` (keyer motif) : le linéaire est une BARRE centrée sur le pivot (d = 1 à la demi-étendue)."""
    u, v = _rot_coords(cx, cy, c, s, r0, r1)
    hx = WIDTH / 2.0; hy = HEIGHT / 2.0
    if shape == "linear":
        lh = hx * abs(c) + hy * abs(s)              # demi-étendue de la projection, image centrée
        P = 2.0 * lh / rep
        if sym or pattern:
            # Porte coulissante : B part de l'axe (pivot = centre) vers les deux bords ; en
            # répétition, chaque bande s'ouvre depuis son milieu.
            if rep > 1:
                # bandes calées sur le cadre quand le centre est au milieu (N pair : demi-période)
                ph = (P / 2.0) if rep % 2 == 0 else 0.0
                u = np.mod(u - np.float32(ph) + np.float32(P / 2.0), np.float32(P)) - np.float32(P / 2.0)
            d = np.abs(u)
        else:
            d = u - np.float32(umin)
            if rep > 1:
                d = np.mod(d, np.float32(P))        # stores : N bandes balayées ensemble
        k = (rep if pattern else 1) / lh            # motif : la taille 1 remplit chaque bande
        return d * np.float32(k), k
    if rep > 1:
        # Damier : N × N cellules au format de l'image, grille tournée avec la forme et déplacée
        # avec le centre ; centre au milieu → cellules calées sur le cadre (N pair : demi-cellule).
        cw = WIDTH / float(rep); chh = HEIGHT / float(rep)
        pu_ = (cw / 2.0) if rep % 2 == 0 else 0.0
        pv_ = (chh / 2.0) if rep % 2 == 0 else 0.0
        u = np.mod(u - np.float32(pu_) + np.float32(cw / 2.0), np.float32(cw)) - np.float32(cw / 2.0)
        v = np.mod(v - np.float32(pv_) + np.float32(chh / 2.0), np.float32(chh)) - np.float32(chh / 2.0)
        hx = cw / 2.0; hy = chh / 2.0
    return _shape_dist(shape, u, v, hx, hy, sym), _shape_px(shape, hx, hy)


def _wipe_field(geom, pattern=False):
    """(d float32 plein cadre, M, un pixel) d'une géométrie (forme, angle, centre, symétrie,
    répétition) — mis en cache ; le SENS n'en fait pas partie (cf. _ramp_into), N/R ne recalcule
    rien. Calcul par paquets de lignes : en tâche de fond, il rend la main entre deux paquets.
    `pattern` (keyer motif) : distance au centre telle quelle (la TAILLE s'y mesure), pas recalée
    sur le cadre."""
    ck = (geom, bool(pattern))
    with _wipe_fields_lock:
        got = _wipe_fields.get(ck)
    if got is not None:
        return got
    shape, ang, cx, cy, sym, rep = geom
    c, s = _rot_cs(ang)
    cxp = cx * WIDTH; cyp = cy * HEIGHT
    # minimum de la projection : à un coin (u est affine en x, y)
    umin = min((x + 0.5 - cxp) * c + (y + 0.5 - cyp) * s for x in (0, WIDTH - 1) for y in (0, HEIGHT - 1))
    t0 = time.monotonic()
    d = np.empty((HEIGHT, WIDTH), dtype=np.float32)
    px = 1.0
    for r0 in range(0, HEIGHT, _FIELD_ROWS):
        r1 = min(HEIGHT, r0 + _FIELD_ROWS)
        blk, px = _field_rows(shape, cxp, cyp, c, s, sym, rep, umin, r0, r1, pattern)
        d[r0:r1] = blk
    if not pattern:
        d -= np.float32(d.min())                # centre hors cadre : la course commence au cadre
    got = (d, max(float(d.max()), 1e-6), px, (None if pattern else _field_tiles(d)))
    _field_cost_ms[shape] = (time.monotonic() - t0) * 1000.0
    with _wipe_fields_lock:
        if len(_wipe_fields) >= _WIPE_FIELDS_MAX:
            _wipe_fields.pop(next(iter(_wipe_fields)))
        _wipe_fields[ck] = got
    return got


_wipe_inflight = set()      # géométries en cours de calcul en tâche de fond (un seul fil par géométrie)


def _wipe_prefetch_run(geom):
    try:
        _wipe_field(geom)
    finally:
        with _wipe_fields_lock:
            _wipe_inflight.discard(geom)


def _wipe_prefetch(w):
    """Précalcule en tâche de fond la géométrie d'un réglage (appelé quand un réglage de volet
    change) : le départ du volet trouve son champ prêt au lieu de le calculer dans la trame.
    0.23.58 : un seul fil par géométrie (un angle tourné vite n'empile plus les calculs)."""
    geom = _wipe_geom(w)
    with _wipe_fields_lock:
        if (geom, False) in _wipe_fields or geom in _wipe_inflight:
            return
        _wipe_inflight.add(geom)
    threading.Thread(target=_wipe_prefetch_run, args=(geom,), daemon=True).start()


_TILE_H = 8
_TILE_W = 64
# Classe de chaque tuile au dernier remplissage d'un tampon (id du tampon → int8 : 0, 1, 2 = calculée) :
# une tuile restée 0 ou 1 n'est pas réécrite — le bord ne traverse que quelques tuiles par trame.
_tile_cls = dict()


def _field_tiles(d):
    """(d vu en tuiles, min, max) par tuile, ou None si l'image ne se découpe pas en tuiles."""
    if HEIGHT % _TILE_H or WIDTH % _TILE_W:
        return None
    d4 = d.reshape(HEIGHT // _TILE_H, _TILE_H, WIDTH // _TILE_W, _TILE_W)
    return (d4, d4.min(axis=(1, 3)), d4.max(axis=(1, 3)))


def _ramp_calc(out, d, k, s, rev):
    if rev:
        np.subtract(d, np.float32(k), out=out)
    else:
        np.subtract(np.float32(k), d, out=out)
    np.multiply(out, np.float32(1.0 / s), out=out)
    np.clip(out, 0.0, 1.0, out=out)
    return out


def _ramp_cls(tmin, tmax, k, s, rev):
    """Classe de chaque tuile d'une rampe de seuil k, largeur s (cf. _ramp_into) : int8 0 ou 1 = la
    tuile entière vaut cette constante (classement CONSERVATEUR, marge d'un millième de douceur :
    le calcul y donnerait exactement la même valeur), 2 = le bord la traverse, à calculer."""
    g = 1e-3 * s
    if rev:     # part de B = clip((d − k) / s) : 1 si d ≥ k + s, 0 si d ≤ k
        one = tmin >= k + s + g
        zero = tmax <= k - g
    else:       # part de B = clip((k − d) / s) : 1 si d ≤ k − s, 0 si d ≥ k
        one = tmax <= k - s - g
        zero = tmin >= k + g
    return np.where(one, 1, np.where(zero, 0, 2)).astype(np.int8)


def _tile_fill(out, cls, keep):
    """Remplit les tuiles CONSTANTES (classe 0 ou 1) de `out` ; les tuiles de classe 2 sont laissées
    à l'appelant. `keep` (tampon PERSISTANT) : seules les tuiles dont la classe a changé depuis le
    dernier remplissage de CE tampon sont réécrites. Beaucoup de changements (début d'un AUTO : la
    trame précédente du tampon était la fin du dernier, toute à 1) → une écriture CONTIGUË du cadre
    entier plutôt qu'une dispersion tuile par tuile (mesuré le 2026-10-29 sur dell-1, 1080p : la
    dispersion de toutes les tuiles coûte plusieurs millisecondes, l'écriture contiguë moins d'une)."""
    o4 = out.reshape(HEIGHT // _TILE_H, _TILE_H, WIDTH // _TILE_W, _TILE_W)
    prev = _tile_cls.get(id(out)) if keep else None
    if prev is None or prev.shape != cls.shape:
        o4[...] = (cls == 1).astype(np.float32)[:, None, :, None]
    else:
        ch = (cls != prev) & (cls != 2)
        if 4 * int(np.count_nonzero(ch)) > ch.size:
            o4[...] = (cls == 1).astype(np.float32)[:, None, :, None]
        else:
            ic, jc = np.nonzero((cls != prev) & (cls != 2))
            if ic.size:
                o4[ic, :, jc, :] = cls[ic, jc].astype(np.float32)[:, None, None]
    if keep:
        _tile_cls[id(out)] = cls
    else:
        _tile_cls.pop(id(out), None)
    return o4


def _ramp_k(e, s, M, rev):
    """Seuil de la rampe de bord e, largeur s (cf. _ramp_into) — REV : mesuré sur M − d."""
    return (M - e - 0.5 * s) if rev else (e + 0.5 * s)


def _ramp_into(out, d, e, s, M, rev, tiles=None, keep=False):
    """out ← part de B, EN PLACE : rampe de largeur s centrée sur le bord e, 1 côté d < e − s/2,
    0 côté d > e + s/2. REV : la même sur M − d (le volet part de l'autre bout, une forme se
    referme), sans toucher au champ en cache. `tiles` (cf. _field_tiles) : seules les tuiles que
    le bord traverse sont calculées ; `keep` (tampon PERSISTANT, cf. _wipe_buf) : les tuiles restées
    à 0 ou 1 depuis son dernier remplissage ne sont pas réécrites. Jamais sur un tampon neuf : son
    id peut être celui d'un tableau libéré."""
    k = _ramp_k(e, s, M, rev)
    if tiles is None:
        _tile_cls.pop(id(out), None)
        return _ramp_calc(out, d, k, s, rev)
    d4, tmin, tmax = tiles
    cls = _ramp_cls(tmin, tmax, k, s, rev)
    o4 = _tile_fill(out, cls, keep)
    ia, ja = np.nonzero(cls == 2)
    if ia.size:
        sub = d4[ia, :, ja, :]
        o4[ia, :, ja, :] = _ramp_calc(sub, sub, k, s, rev)
    return out


_wipe_bufs = dict()


def _wipe_buf(owner, slot):
    if owner is None:
        return np.empty((HEIGHT, WIDTH), dtype=np.float32)
    key = (owner, slot)
    b = _wipe_bufs.get(key)
    if b is None:
        b = _wipe_bufs[key] = [0, np.empty((HEIGHT, WIDTH), dtype=np.float32),
                               np.empty((HEIGHT, WIDTH), dtype=np.float32)]
    b[0] ^= 1
    return b[1 + b[0]]


def _on_air(snap, private=False):
    """(fill, key) à l'antenne pour l'instantané `snap` de frames (pris sous verrou). `private` :
    calcul dans des tampons À SOI — sinon un appel hors du thread de sortie écrirait dans les
    tampons que celui-ci est en train de publier."""
    fd = snap.get("fade")
    if not fd:
        return snap["fill"], snap["key"]
    w = _fade_weight(fd)
    if fd.get("ends"):
        m = _wipe_mask(fd["wipe"], w, None if private else "onair") if fd.get("wipe") else None
        outs = []
        for e, add in ((fd["f"], fd.get("additive")), (fd["k"], False)):
            if e is None:
                outs.append(snap["key"]); continue
            buf = np.empty(V_FRAME_SIZE // _BPS, _NP_DT) if private else e[2]
            _mix_ends([(e[0], e[1], _planes(buf), add)], w, m, 0, HEIGHT)
            outs.append(buf.view(np.uint8))
        return outs[0], outs[1]
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
        with state_lock:
            tr = dict(state["transition"])
        wipe = _wipe_norm_s(tr.get("wipe")) if tr.get("type") == "wipe" else None
        additive = tr.get("type") == "additive"
        if wipe is not None and not _field_ready(wipe):
            log("stills: wipe field not ready — this transition is a dissolve", "warning")
            wipe = None
        if _MVK or _DITHER is not None or wipe is not None or additive:
            # (ancienne, nouvelle, tampon de sortie de _on_air) en plans : rien à préparer.
            ends = lambda old, new: (_planes(old), _planes(new), np.empty(V_FRAME_SIZE // _BPS, _NP_DT))
            fade = {{"ends": True, "wipe": wipe, "additive": additive, "n": n, "i": 0, "f": ends(cur_f, fill_bytes),
                     # clé identique (images opaques : cas courant) → on ne mélange que le remplissage
                     "k": None if cur_k == key_bytes else ends(cur_k, key_bytes)}}
        else:
            fade = {{"f": _prep_fade(cur_f, fill_bytes),
                     "k": None if cur_k == key_bytes else _prep_fade(cur_k, key_bytes),
                     "n": n, "i": 0}}
    with frames_lock:
        frames["fade"] = fade
        frames["fill"] = fill_bytes; frames["key"] = key_bytes
        # Le JPEG « programme » dérive de ce qui vient d'être posé ; ancien état gardé pour le fondu.
        with prog_lock:
            prog["prev"] = prog["rgba"]; prog["rgba"] = rgba; prog["meta"] = meta
            prog["since"] = time.time(); prog["gen"] += 1


def _heif_decode(full):
    """HEIC/HEIF -> PIL image through `heif-dec` (orientation already applied by libheif)."""
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="stills-heif-")
    os.close(fd)
    try:
        r = subprocess.run(["heif-dec", full, tmp], capture_output=True, timeout=30)
        if r.returncode != 0:
            raise ValueError((r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()[-200:] or "heif-dec %d" % r.returncode)
        im = Image.open(tmp)
        im.load()
        return im
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _open_image(full):
    """Open an image file as the operator sees it on their screen: (PIL image RGBA, format, notes).

    - EXIF orientation applied: a portrait phone photo came out lying on its side (0.19.25);
    - embedded ICC profile converted to sRGB (CMYK print files included);
    - 16-bit greyscale scaled down instead of clipped to white;
    - HEIC/HEIF through whichever decoder the runtime image carries (HEIF_DECODER).
    Raises ValueError with a message meant for the operator."""
    notes = {{}}
    if full.lower().endswith(HEIF_EXTS):
        if not HEIF_DECODER:
            raise ValueError("HEIC : l'image d'exécution de ce conteneur ne sait pas le lire "
                             "(décodeur absent ; mettre à jour l'image bobi-compute du nœud)")
        img = _heif_decode(full) if HEIF_DECODER == "heif-dec" else Image.open(full)
        fmt = "HEIF"
    else:
        img = Image.open(full)
        fmt = img.format or ""
    try:
        o = img.getexif().get(0x0112) or 1
    except Exception:
        o = 1
    if o != 1:
        img = ImageOps.exif_transpose(img)
        notes["rotated"] = int(o)
    icc = img.info.get("icc_profile")
    if icc:
        try:
            from PIL import ImageCms
            alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
            src_mode = "CMYK" if img.mode == "CMYK" else ("RGBA" if alpha else "RGB")
            out_mode = "RGBA" if alpha else "RGB"
            img = ImageCms.profileToProfile(img.convert(src_mode), ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                            ImageCms.createProfile("sRGB"), outputMode=out_mode)
            notes["icc"] = True
        except Exception as e:                    # unreadable profile: the pixels as they are
            log("profil ICC ignoré (%s) : %s" % (os.path.basename(full), e), "warning")
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        a = np.asarray(img).astype(np.uint32)
        img = Image.fromarray((np.clip(a, 0, 65535) >> 8).astype(np.uint8), "L")
    return img.convert("RGBA"), fmt, notes


def _compose(file, fit, x, y, size):
    """Le CADRAGE — SEUL calcul de l'image de sortie, partagé par la sortie (`_render`) et la
    preview (`_do_preview`) : ce qu'on voit en preview EST ce qui passera, au pixel près.
    Rend (canvas RGBA uint8 (HEIGHT,WIDTH,4), meta) ou (None, message d'erreur)."""
    full = _safe_join(file)
    if not full or not os.path.isfile(full):
        return None, "fichier introuvable"
    try:
        img, src_fmt, notes = _open_image(full)
    except ValueError as e:
        return None, str(e)
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
    meta.update(notes)
    meta["warn"] = _framing_warnings(w0, h0, fit, tw, th, max(0, dx1 - dx0), max(0, dy1 - dy0))
    return canvas, meta


def _framing_warnings(w0, h0, fit, tw, th, vis_w, vis_h):
    """What the operator should know before a TAKE, as codes the UI words: the image does not show
    whole (cropped), is enlarged past its own definition (upscaled, soft), or is deformed (distorted).
    Nothing is said for what the operator chose on purpose and can see: bands around a fitted image."""
    out = []
    if vis_w < tw - 1 or vis_h < th - 1:
        out.append("cropped")
    if tw > w0 * 1.05 or th > h0 * 1.05:
        out.append("upscaled")
    if fit == "stretch" and abs((w0 / h0) / (WIDTH / HEIGHT) - 1) > 0.02:
        out.append("distorted")
    return out


# ─── Programme / preview : JPEG à la demande ─────────────────────────────────
# Aucun encodage en continu : on encode UNE fois quand quelqu'un le demande, on met en cache, et
# on ne ré-encode qu'après un changement (`gen`). Pendant un fondu, le poids est quantifié par
# dixièmes (au plus ~10 encodages par fondu, seulement si l'UI regarde).
PV_W = 640
prog_lock = threading.Lock()
prog = {{"gen": 0, "rgba": None, "prev": None, "meta": None, "since": None}}
prev_lock = threading.Lock()
prev = {{"gen": 0, "rgba": None, "meta": None, "error": ""}}
# What each AUX output carries (cut only, no fade): YUV bytes for the writers, the RGBA canvas
# for its monitor, a generation for the JPEG cache.
aux_lock = threading.Lock()
aux = [{{"fill": _black_fill(), "key": _black_key(), "rgba": None, "meta": None, "gen": 0, "error": ""}}
       for _ in range(AUX_N)]


def _aux_set(i, fr):
    """Puts frame `fr` ({{file, fit, x, y, size}}; empty file = black, transparent) on AUX i."""
    if fr["file"]:
        canvas, meta = _compose(fr["file"], fr["fit"], fr["x"], fr["y"], fr["size"])
        if canvas is None:
            with aux_lock:
                aux[i]["error"] = meta
            return False, meta
        fb, kb = _canvas_to_yuv(canvas)
    else:
        canvas, meta, fb, kb = None, None, _black_fill(), _black_key()
    with state_lock:
        state["aux"][i] = fr
    with aux_lock:
        aux[i].update(fill=fb, key=kb, rgba=canvas, meta=meta, error="")
        aux[i]["gen"] += 1
    return True, "ok"


def _aux_jpeg(i, view):
    with aux_lock:
        gen, cur = aux[i]["gen"], aux[i]["rgba"]
    if cur is None:
        return None
    return _cached_jpeg("aux%d" % i, gen, view, None, lambda: _jpeg_of(_small(cur), view))
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
    # The image READY TO AIR is prepared now, while a still image is on air (the output thread then
    # only commits): a TAKE has nothing left to compute. Computed at the TAKE, the PNG decode, the
    # framing and the YUV conversion held Python for tens of ms right as the transition started —
    # 1 or 2 frames published 18-27 ms late, repeated by the SDI output, at every TAKE (measured
    # 2026-11-15 on a DL360, 0.19.17).
    yuv = None
    if canvas is not None:
        yuv = (_frame_key(file, fit, x, y, size),) + _canvas_to_yuv(canvas)
    with prev_lock:
        prev["gen"] += 1
        prev["rgba"] = canvas
        prev["meta"] = meta if canvas is not None else None
        prev["error"] = "" if canvas is not None else meta
        prev["yuv"] = yuv
    return canvas is not None, ("ok" if canvas is not None else meta)


def _frame_key(file, fit, x, y, size):
    return (str(file or ""), str(fit or "fit"), float(x or 0), float(y or 0), float(size or 100))


def _render(file, fit, x, y, size, fade_ms=None):
    if fade_ms is None:
        with state_lock:
            fade_ms = state["transition_ms"]
    # The image is already prepared in preview (same file, same framing): air it as it is.
    with prev_lock:
        ready = prev.get("yuv")
        if ready is not None and ready[0] == _frame_key(file, fit, x, y, size):
            canvas, meta = prev["rgba"], prev["meta"]
        else:
            ready = None
    if ready is not None:
        _set_image(ready[1], ready[2], fade_ms, canvas, meta)
        return True, "ok"
    canvas, meta = _compose(file, fit, x, y, size)
    if canvas is None:
        if meta == "fichier introuvable":
            _set_image(_black_fill(), _black_key(), fade_ms, None, None)
        return False, meta
    fill_bytes, key_bytes = _canvas_to_yuv(canvas)
    _set_image(fill_bytes, key_bytes, fade_ms, canvas, meta)
    return True, "ok"


# ─── Output thread: real-time priority and warm-up (0.19.30, 2026-11-22) ─────────────────────
# Measured on r620-2 (1 s dissolves at 1080p50, 100 per run, 22 busy loops on the shared cores):
# at normal priority the output missed 760-970 slots on the normal kernel and 3 200-8 800 on the
# PREEMPT_RT kernel; in SCHED_FIFO it missed 6, all in the first fade. A real-time kernel only
# helps a thread that is real-time. Since 0.19.31 the priority comes from the orchestrator: the
# manifest declares the role (`resources.realtime: program`), the core grants SYS_NICE and passes
# the grid's number in CONFIG, bobimxl applies it (rt_promote) and keeps what was obtained.
RT_PRIORITY = bobimxl.rt_priority(CONFIG) if hasattr(bobimxl, "rt_promote") else 0


def _output_thread_setup():
    """Run by the output thread before its loop: priority first, then everything that is costly on
    FIRST use, so that it happens here, off air, and not in the first fade."""
    if hasattr(bobimxl, "rt_promote"):
        # pid 0 inside: the calling THREAD only, the HTTP threads stay at normal priority. The fade
        # threads are started by this one and inherit SCHED_FIFO, which they need.
        bobimxl.rt_promote(RT_PRIORITY)
        rt = bobimxl.rt_state()
    else:
        rt = dict(rt_priority=0, rt_priority_error="orchestrator too old: no bobimxl.rt_promote")
    with metrics_lock:
        metrics.update(rt)
    # The first blit of a size calibrates non-temporal stores (bobimxl._nt_calibrer) on the calling
    # thread: about 200 ms on r620-2, so the first fade after a start froze 10-11 slots (a key copy
    # was the process's first blit). Calibrate now, for the size every blit of this script uses.
    try:
        bobimxl.blit(np.empty(V_FRAME_SIZE, np.uint8), np.zeros(V_FRAME_SIZE, np.uint8))
    except Exception as e:
        log("stills: blit warm-up failed (%s)" % (e,), "warning")
    # Fade workers start on first submit. Started here, by this thread, they inherit its policy;
    # started by an HTTP thread (_prep_fade), they would stay at normal priority.
    try:
        bar = threading.Barrier(_FADE_BANDS, timeout=2.0)
        list(_FADE_POOL.map(lambda _i: bar.wait(), range(_FADE_BANDS)))
    except Exception as e:
        log("stills: fade workers warm-up incomplete (%s)" % (e,), "warning")
    # Same for the OpenMP team of the mix kernels (one per calling thread), and its creation cost
    # (11 ms measured on the first mvk call).
    try:
        t = np.zeros((16, 64), _NP_DT)
        if not _mixd_into(t, t, t, 0.5, 0.5) and _MVK:
            bobimxl.mvk_mixf_into(t, t, t, 0.5, 0.5)
    except Exception as e:
        log("stills: mix kernel warm-up failed (%s)" % (e,), "warning")


def feeder():
    _output_thread_setup()
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
    aux_w = []
    for fname, kname in AUX_SHM:
        pair = tuple(bobimxl.Writer(inst, nm, WIDTH, HEIGHT, CHROMA, BIT_DEPTH, FPS_NUM, FPS_DEN,
                                    interlace=SCAN, **_slkw) for nm in (fname, kname))
        opened.extend(pair)
        aux_w.append(pair)
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
    # DEADLINE WITNESS (P1, 2026-11-23): the common block read by the orchestrator. A frame is due
    # at the END of its grid slot (TAI), the point past which a reader gets the previous one; a
    # skipped slot (index gap, as `slots_missed`) is missed too. Free run: one period after start.
    _DW = (bobimxl.DeadlineWitness(period, clock=bobimxl.now_tai)
           if hasattr(bobimxl, "DeadlineWitness") else None)
    _dw_due = None
    # PUBLISH LAG (2026-11-15): time from the start of a frame's slot to the end of its last
    # commit. A frame committed late is not « missed » (it is in its slot), but an output that
    # reads it at the slot boundary gets the previous one: repeat, then a double step. Fades
    # stuttered that way on an SDI output while slots_missed stayed at 0.
    lag = {{"win_max": 0.0, "fade_max": 0.0, "over10": 0, "over15": 0, "over20": 0, "dup_index": 0}}
    t_slot = None
    # Case de l'anneau → image qu'elle CONTIENT déjà (l'objet bytes lui-même, comparé par `is`).
    # Une image fixe n'a pas à être recopiée à chaque trame : la case de l'anneau la porte encore
    # depuis le tour précédent. Mesuré le 2026-10-23 sur dell-1 (stills-Atelier, 1080p 10 bits) :
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

    def _put_still(w, payload, idx):
        """One still image on writer `w` (AUX outputs): fields if interlaced, bands in slice mode,
        and no copy at all when the ring slot already holds it."""
        if INTERLACED:
            src = np.frombuffer(payload, dtype=np.uint8)
            first = 1 if FIELD_ORDER == "bff" else 0
            for fi, par in (((2 * idx) if idx is not None else None, first),
                            ((2 * idx + 1) if idx is not None else None, 1 - first)):
                _, gi_, vw_ = w.open_grain(index=fi)
                _write_field(vw_, src, par)
                w.commit(gi_)
            return
        _, gi_, vw_ = w.open_grain(index=idx)
        if _held(vw_, payload, True):
            w.commit(gi_)
        elif V_SLICE_H:
            _emit_grain_sliced(w, gi_, vw_, payload)
        else:
            bobimxl.blit(vw_, np.frombuffer(payload, dtype=np.uint8))
            w.commit(gi_)

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
                if _DW is not None:
                    _DW.skipped(idx - last_idx - 1)
            if last_idx is not None and idx == last_idx:
                lag["dup_index"] += 1          # same slot written twice: one weight never shown
            last_idx = idx
            # Réveil au DÉBUT du créneau, pas à son centre : `index_time_ns(X)` est le MILIEU du
            # créneau X (current_index passe à X 10,0 ms avant, mesuré sur dell-1 le 2026-10-25).
            # L'image est connue d'avance : dormir jusqu'au centre la publiait à 10,3 ms sur 20, et
            # un mélangeur « au plus rapide » abandonnait l'attente à la demi-période.
            t_idx = fill_w.index_time_ns(idx)
            t_slot = t_idx - (fill_w.index_time_ns(idx + 1) - t_idx) // 2
            if _DW is not None:
                _dw_due = t_slot + (fill_w.index_time_ns(idx + 1) - t_idx)   # end of the slot
            bobimxl.sleep_until_tai(t_slot)
        else:
            t0 = time.time(); idx = None   # cadence libre → compteur interne du writer
            if _DW is not None:
                _dw_due = bobimxl.now_tai() + int(period * 1e9)
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
            if fd.get("ends"):
                # Noyau C : de l'ancienne et de la nouvelle image vers le grain, en une passe.
                m = _wipe_mask(fd["wipe"], w, "feed") if fd.get("wipe") else None
                pairs = [(fd["f"][0], fd["f"][1], _planes(view_f), fd.get("additive"))]
                mix = lambda a, b: _mix_ends(pairs, w, m, a, b)
            else:
                pairs = [(fd["f"], view_f.view(_NP_DT))]
                mix = lambda a, b: _mix_rows(pairs, w, a, b)
            if fd["k"] is None:
                if not _held(view_k, snap["key"], True):
                    bobimxl.blit(view_k, np.frombuffer(snap["key"], dtype=np.uint8))
            else:
                _held(view_k, None, False)
                pairs.append((fd["k"][0], fd["k"][1], _planes(view_k)) if fd.get("ends")
                             else (fd["k"], view_k.view(_NP_DT)))
            if V_SLICE_H:
                step = V_SLICE_H * -(-_V_TOTAL_SLICES // 3)
                for r1 in range(step, HEIGHT, step):
                    mix(r1 - step, r1)
                    for w_, gi_ in ((fill_w, gi_f), (key_w, gi_k)):
                        w_.commit(gi_, valid_slices=r1 // V_SLICE_H)
                mix((HEIGHT - 1) // step * step, HEIGHT)
            else:
                mix(0, HEIGHT)
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
        if aux_w:
            with aux_lock:
                payloads = [(a["fill"], a["key"]) for a in aux]
            for (wf, wk), (pf, pk) in zip(aux_w, payloads):
                _put_still(wf, pf, idx)
                _put_still(wk, pk, idx)
        if _DW is not None:
            _DW.done(_dw_due)
        if t_slot is not None:
            l_ms = (bobimxl.now_tai() - t_slot) / 1e6
            lag["win_max"] = max(lag["win_max"], l_ms)
            if fd is not None:
                lag["fade_max"] = max(lag["fade_max"], l_ms)
            lag["over10"] += l_ms > 10; lag["over15"] += l_ms > 15; lag["over20"] += l_ms > 20
        win_cnt += 1
        el = time.time() - win_start
        # frame_index à CHAQUE trame : publié une fois par fenêtre d'une seconde, il datait de 0 à
        # ~1 s au moment où on le lisait, et un débit calculé dessus sur 30 s affichait 50,59 i/s
        # pour 49,999 réels (relevé du 2026-10-23 sur stills-Atelier).
        with metrics_lock:
            metrics["frame_index"] = gidx
        if el >= 1.0:
            with metrics_lock:
                metrics["fps"] = round(win_cnt / el, 1)
                metrics["slots_missed"] = missed
                metrics["publish_lag_max_ms"] = round(lag["win_max"], 2)     # this last second
                metrics["publish_lag_fade_max_ms"] = round(lag["fade_max"], 2)   # worst, in any fade
                metrics["publish_late_10ms"] = lag["over10"]
                metrics["publish_late_15ms"] = lag["over15"]
                metrics["publish_late_20ms"] = lag["over20"]
                metrics["slot_written_twice"] = lag["dup_index"]
                if _DW is not None:
                    metrics["deadline"] = _DW.report()   # cf. DEADLINE WITNESS
            lag["win_max"] = 0.0
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
            for i in range(4):
                s["aux%d_file" % (i + 1)] = s["aux"][i]["file"] if i < AUX_N else ""
            s.update({{"fps": m["fps"], "frame_index": m["frame_index"],
                       "media_root": MEDIA_ROOT, "plugin_version": PLUGIN_VERSION,
                       "fill_shm": FILL_SHM, "key_shm": KEY_SHM,
                       "width": WIDTH, "height": HEIGHT, "scan": SCAN,
                       "log_level": LOG_LEVEL,   # lisible en condition de macro
                       "fading": frames["fade"] is not None,   # fondu en cours (condition de macro)
                       "fade_dither": _DITHER is not None,     # dithered dissolve (kernel ABI 2)
                       "rt_priority": m.get("rt_priority", 0),  # applied (0 = normal or refused)
                       "program_file": s.get("file", ""),      # image À L'ANTENNE
                       "preview_file": (s.get("preview") or {{}}).get("file", ""),   # image en PREVIEW
                       "default_file": (s.get("default") or {{}}).get("file", ""),   # image par défaut
                       "transition_type": (s.get("transition") or {{}}).get("type", "dissolve"),
                       "wipe_direction": ((s.get("transition") or {{}}).get("wipe") or {{}}).get("direction", "linear"),
                       "wipe_angle": ((s.get("transition") or {{}}).get("wipe") or {{}}).get("angle", 0.0),
                       "wipe_softness": ((s.get("transition") or {{}}).get("wipe") or {{}}).get("softness", 0.10),
                       "wipe_reverse": bool(((s.get("transition") or {{}}).get("wipe") or {{}}).get("reverse")),
                       "wipe_shapes": list(WIPE_SHAPES),
                       "mvk": _MVK,
                       "aux_count": AUX_N,
                       "aux_shm": [f for f, _k in AUX_SHM],
                       "aux_gen": [a["gen"] for a in aux],
                       "aux_error": [a["error"] for a in aux],
                       "on_air_since": prog["since"], "program_gen": prog["gen"],
                       "program_meta": prog["meta"], "preview_meta": prev["meta"],
                       "preview_error": prev["error"], "preview_gen": prev["gen"],
                       "jpeg_encodes": _jpeg_stats["encodes"],   # preuve : pas d'encodage en continu
                       "mxl_lib": _mxl_lib_state()}})
            self._json(200, s)
        elif u.path == "/persisted":
            # Persistence snapshot (`control.persist`): exactly what goes into params.stills_state.
            with state_lock:
                d = {{"default": dict(state["default"]), "aux": [dict(x) for x in state["aux"]],
                      "transition": {{"ms": state["transition_ms"], "type": state["transition"]["type"],
                                      "wipe": dict(state["transition"]["wipe"])}}}}
            self._json(200, d)
        elif u.path == "/aux.jpg":
            try:
                i = int((q.get("n") or ["1"])[0]) - 1
            except ValueError:
                i = -1
            if not 0 <= i < AUX_N:
                return self._json(404, {{"error": "pas de sortie AUX %s" % (i + 1)}})
            data = _aux_jpeg(i, "key" if (q.get("view") or [""])[0] == "key" else "fill")
            if data is None:
                self.send_response(204); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        elif u.path == "/default":
            # Persistence snapshot (`control.persist`): exactly what goes into params.default_image.
            with state_lock: d = dict(state["default"])
            self._json(200, d)
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
            im = _open_image(full)[0]; im.thumbnail((160, 160))
            bg = Image.new("RGB", im.size, (32, 36, 42))   # fond neutre sous l'alpha
            bg.paste(im, mask=im.split()[-1]); im = bg
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=80)
            data = buf.getvalue()
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(500, {{"error": str(e)}})

    def _go_air(self, b, force_fade=False):
        self._json(*_air(b, force_fade))

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
            self._json(*_air_swapping(body))
        elif u.path == "/default/recall":
            b = self._read()
            with state_lock: d = dict(state["default"])
            if not d["file"]:
                return self._json(400, {{"ok": False, "detail": "aucune image par défaut"}})
            d["fade_ms"] = b.get("fade_ms")
            self._json(*_air_swapping(d))
        elif u.path == "/default/set":
            self._json(*_default_set(self._read()))
        elif u.path == "/aux":
            self._json(*_aux_post(self._read()))
        elif u.path == "/step":
            self._json(*_step(self._read()))
        elif u.path == "/take_direct":
            b = self._read()
            v = b.get("enabled", b.get("take_direct"))
            v = _as_bool_cfg(v, default=False)
            with state_lock: state["take_direct"] = v
            self._json(200, {{"ok": True, "take_direct": v}})
        elif u.path == "/transition":
            # Transition À CHAUD : durée (`ms`, 0 = coupe franche), type (dissolve | additive |
            # wipe) et réglage du volet (direction = forme, angle, softness, reverse) — persistés
            # par l'orchestrateur (control.persist). Champ absent = inchangé.
            b = self._read()
            with state_lock:
                tr = {{"type": state["transition"]["type"], "wipe": dict(state["transition"]["wipe"])}}
                ms = state["transition_ms"]
            if "ms" in b or "transition_ms" in b:
                try:
                    ms = max(0.0, min(10000.0, float(b.get("ms", b.get("transition_ms", 0)) or 0)))
                except (TypeError, ValueError):
                    return self._json(400, {{"error": "ms : nombre attendu"}})
            t = b.get("type")
            if t not in (None, ""):
                if t not in TRANSITION_TYPES:
                    return self._json(400, {{"error": "type : %s attendu" % " | ".join(TRANSITION_TYPES)}})
                tr["type"] = t
            raw = dict(tr["wipe"])
            raw.update(b.get("wipe") if isinstance(b.get("wipe"), dict) else {{}})
            for k in ("direction", "angle", "softness", "reverse"):
                if k in b:
                    raw[k] = b[k]
            if "direction" in raw and str(raw["direction"]).strip().lower() not in WIPE_NAMES:
                return self._json(400, {{"error": "direction : %s attendu" % " | ".join(WIPE_NAMES)}})
            tr["wipe"] = _wipe_norm_s(raw)
            with state_lock:
                state["transition_ms"] = ms
                state["transition"] = tr
            if tr["type"] == "wipe":
                _wipe_prefetch(tr["wipe"])          # le champ se prépare pendant qu'on règle
            self._json(200, {{"ok": True, "transition_ms": ms, "type": tr["type"], "wipe": tr["wipe"]}})
        elif u.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            b = self._read()
            ok = set_log_level(b.get("level") or b.get("log_level"))
            self._json(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})
        else:
            self._json(404, {{"error": "not found"}})
    def log_message(self, *a): pass


def _air(b, force_fade=False):
    """Met l'image à l'ANTENNE (corps de /select, /take, /cut, rappel de l'image par défaut).
    Même chemin pour tous : la sortie ne connaît qu'`_render`. Un changement de cadrage de la
    MÊME image reste une coupe (le fondu ne vaut que pour un changement d'image), sauf
    `force_fade` (Take/Cut, où la durée est explicite). Rend (code HTTP, réponse)."""
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
        return 400, {{"error": "fade_ms : nombre de millisecondes attendu"}}
    if fade is None and s["file"] == prev_file and not force_fade:
        fade = 0.0
    ok, detail = _render(s["file"], s["fit"], s["x"], s["y"], s["size"], fade_ms=fade)
    with metrics_lock:
        metrics["file"] = s["file"] if ok else metrics["file"]
    return (200 if ok else 400), {{"ok": ok, "detail": detail, "file": s["file"]}}


def _clear_preview():
    with state_lock:
        state["preview"] = _norm_frame(None)
    with prev_lock:
        prev["gen"] += 1
        prev["rgba"] = None; prev["meta"] = None; prev["error"] = ""; prev["yuv"] = None


def _air_swapping(body):
    """Take, Cut, rappel de l'image par défaut : comme sur un mélangeur, les deux images
    S'ÉCHANGENT — ce qui était à l'antenne passe en preview, un second Take y revient. Rien à
    l'antenne avant (noir) → preview vide."""
    with state_lock:
        old = _norm_frame(state)
    with prog_lock:
        on_air = prog["meta"] is not None
        old_rgba, old_meta = prog["rgba"], prog["meta"]
    with frames_lock:
        old_fill, old_key = frames["fill"], frames["key"]      # what that image airs as, ready
    code, out = _air(body, force_fade=True)
    if code == 200:
        if on_air and old["file"] and old_rgba is not None:
            # The old program image is ALREADY rendered, with its framing: reuse it. Re-composing
            # it (_do_preview: decode, resize, convert) took the GIL right when the fade starts,
            # and the output thread published its first frames late — the SDI output showed a
            # repeat then a double step at every TAKE (measured 2026-11-15, 0.19.16).
            with state_lock:
                state["preview"] = old
            with prev_lock:
                prev["gen"] += 1
                prev["rgba"] = old_rgba; prev["meta"] = old_meta; prev["error"] = ""
                prev["yuv"] = (_frame_key(old["file"], old["fit"], old["x"], old["y"], old["size"]), old_fill, old_key)
        elif on_air and old["file"]:
            _do_preview(old["file"], old["fit"], old["x"], old["y"], old["size"])
        else:
            _clear_preview()
        out["preview_file"] = old["file"] if on_air else ""
    return code, out


def _default_set(b):
    """/default/set : {{"from": "preview"|"program"}} reprend l'image et son cadrage, ou
    {{file, fit, x, y, size}} explicite ; {{"file": ""}} retire l'image par défaut."""
    src = b.get("from")
    with state_lock:
        if src == "preview":
            d = _norm_frame(state["preview"])
        elif src == "program":
            d = _norm_frame(state)
        else:
            d = _norm_frame(b)
    if d["file"]:
        full = _safe_join(d["file"])
        if not full or not os.path.isfile(full):
            return 400, {{"ok": False, "detail": "fichier introuvable"}}
    elif src:
        return 400, {{"ok": False, "detail": "aucune image en %s" % src}}
    with state_lock:
        state["default"] = d
    return 200, {{"ok": True, "default": d}}


def _aux_post(b):
    """/aux {{n: 1..AUX_N, file, fit, x, y, size}} — or {{n, from: "preview"|"program"}} ; an
    empty file clears the output (black, transparent). Cut only: an AUX holds an image, it does
    not dissolve."""
    try:
        i = int(b.get("n")) - 1
    except (TypeError, ValueError):
        i = -1
    if not 0 <= i < AUX_N:
        return 400, {{"ok": False, "detail": "sortie AUX inconnue (%d configurée(s))" % AUX_N}}
    src = b.get("from")
    with state_lock:
        fr = _norm_frame(state["preview"] if src == "preview" else state if src == "program" else b)
    if src and not fr["file"]:
        return 400, {{"ok": False, "detail": "aucune image en %s" % src}}
    ok, detail = _aux_set(i, fr)
    return (200 if ok else 400), {{"ok": ok, "detail": detail, "n": i + 1, "file": fr["file"]}}


def _step(b):
    """/step {{delta: +1|-1}} : image suivante / précédente du DOSSIER de l'image en preview (à
    défaut, de celle à l'antenne), par ordre de nom. Elle passe en preview — à l'antenne en
    Take direct — avec le cadrage courant. Ne boucle pas : au bout, rien ne bouge (`edge`)."""
    try:
        delta = int(b.get("delta", 1))
    except (TypeError, ValueError):
        return 400, {{"error": "delta : entier attendu"}}
    with state_lock:
        pv, pg, direct = dict(state["preview"]), _norm_frame(state), state["take_direct"]
    ref = pv if pv["file"] else pg
    imgs = _list_images(os.path.dirname(ref["file"]))
    if not imgs:
        return 400, {{"ok": False, "detail": "aucune image dans le dossier"}}
    if ref["file"] in imgs:
        i = imgs.index(ref["file"]) + (1 if delta > 0 else -1 if delta < 0 else 0)
    else:
        i = 0 if delta >= 0 else len(imgs) - 1
    if not 0 <= i < len(imgs):
        return 200, {{"ok": True, "edge": True, "file": ref["file"]}}
    fr = dict(ref, file=imgs[i])
    if direct:
        return _air(fr)
    ok, detail = _do_preview(fr["file"], fr["fit"], fr["x"], fr["y"], fr["size"])
    return (200 if ok else 400), {{"ok": ok, "detail": detail, "file": fr["file"]}}



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

# Transition persistée : type, réglage du volet, durée. Le champ du volet est préparé tout de suite
# (tâche de fond) : le premier TAKE en volet n'attend pas.
with state_lock:
    _tr0 = state["transition"]
    if _tr0.get("ms") not in (None, ""):
        try:
            state["transition_ms"] = max(0.0, min(10000.0, float(_tr0["ms"])))
        except (TypeError, ValueError):
            pass
    state["transition"] = {{"type": _tr0.get("type") if _tr0.get("type") in TRANSITION_TYPES else "dissolve",
                            "wipe": _wipe_norm_s(_tr0.get("wipe"))}}
    if state["transition"]["type"] == "wipe":
        _wipe_prefetch(state["transition"]["wipe"])

# AUX : chaque sortie reprend l'image qu'elle portait (persistée).
for _i in range(AUX_N):
    if state["aux"][_i]["file"]:
        try:
            _aux_set(_i, state["aux"][_i])
        except Exception as e:
            log("aux %d initial: %s" % (_i + 1, e), "warning")

# Rendu initial si une image est déjà configurée au déploiement ; sinon l'image par défaut.
if not (state["file"] or "").strip() and state["default"]["file"]:
    state.update({{k: state["default"][k] for k in ("file", "fit", "x", "y", "size")}})
if (state["file"] or "").strip():
    try:
        _render(state["file"], state["fit"], state["x"], state["y"], state["size"])
        _do_preview(state["file"], state["fit"], state["x"], state["y"], state["size"])
    except Exception as e:
        log("render initial: %s" % (e,), "warning")

while True:
    time.sleep(1)
