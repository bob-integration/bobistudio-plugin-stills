# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Stills plugin — affiche une IMAGE FIXE (avec transparence éventuelle) et PRODUIT
# deux shm vidéo YUV420p au format ring MXL :
#   Fill = {hostname}        (l'image cadrée, RGB)
#   Key  = {hostname}_key    (l'alpha en luma : blanc=opaque, noir=transparent)
#
# Ce fichier est un TEMPLATE str.format() : SEULS {config} / {hostname} /
# {plugin_version} sont des placeholders. TOUTE autre accolade littérale ({{ }})
# doit être doublée, sinon str.format() échoue.
# ─────────────────────────────────────────────────────────────────────────────
import os, io, mmap, struct, time, json, threading, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
from PIL import Image

CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

MEDIA_ROOT = CONFIG.get("media_root") or "/mnt/media"
WIDTH  = int(CONFIG.get("width") or 1280);  WIDTH  -= WIDTH % 2
HEIGHT = int(CONFIG.get("height") or 720);  HEIGHT -= HEIGHT % 2
FPS    = int(round(float(CONFIG.get("fps") or 25))) or 25

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
_CW = {{"420": 2, "422": 2, "444": 1}}.get(CHROMA, 2)   # diviseur largeur chroma
_CH = {{"420": 2, "422": 1, "444": 1}}.get(CHROMA, 1)   # diviseur hauteur chroma
PIX_FMT = ({{"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"}}.get(CHROMA, "yuv422p")
           + (("12le" if BIT_DEPTH >= 12 else "10le") if _DEEP else ""))
UV_W = WIDTH // _CW
UV_H = HEIGHT // _CH
V_HEADER_SIZE = 64
V_RING_SIZE   = CONFIG.get("shm_video_ring", 10)
Y_SIZE        = WIDTH * HEIGHT * _BPS
UV_SIZE       = UV_W * UV_H * _BPS
V_FRAME_SIZE  = Y_SIZE + 2 * UV_SIZE
V_TOTAL_SIZE  = V_HEADER_SIZE + V_RING_SIZE * V_FRAME_SIZE

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
metrics = {{"fps": 0.0, "frame_index": 0, "file": "", "plugin_version": PLUGIN_VERSION}}


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


# ─── producteurs shm (Fill + Key) ────────────────────────────────────────────
def _open_out(path, size):
    with open(path, "wb") as f:
        f.write(b"\x00" * size)
    f = open(path, "r+b")
    return f, mmap.mmap(f.fileno(), size)

def feeder():
    ff_f, ff = _open_out("/dev/shm/" + FILL_SHM, V_TOTAL_SIZE)
    kf_f, kf = _open_out("/dev/shm/" + KEY_SHM,  V_TOTAL_SIZE)
    idx = 1
    win_cnt = 0; win_start = time.time()
    period = 1.0 / FPS
    while True:
        t0 = time.time()
        with frames_lock:
            fb = frames["fill"]; kb = frames["key"]
        slot = idx % V_RING_SIZE
        off = V_HEADER_SIZE + slot * V_FRAME_SIZE
        ts = time.time_ns()
        ff[off:off + V_FRAME_SIZE] = fb
        ff[0:16] = struct.pack("QQ", idx, ts)
        kf[off:off + V_FRAME_SIZE] = kb
        kf[0:16] = struct.pack("QQ", idx, ts)
        idx += 1
        win_cnt += 1
        el = time.time() - win_start
        if el >= 1.0:                                  # fps sur fenêtre glissante (~1s)
            with metrics_lock:
                metrics["fps"] = round(win_cnt / el, 1); metrics["frame_index"] = idx
            win_cnt = 0; win_start = time.time()
        dt = period - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)


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
                       "width": WIDTH, "height": HEIGHT}})
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
        print("render initial:", e)

while True:
    time.sleep(1)
