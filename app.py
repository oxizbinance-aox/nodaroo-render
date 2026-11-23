#!/usr/bin/env python3
"""
Nodaroo Cinematic Render Service — app.py
================================================
A small Flask service that renders cinematic 2D/3D videos on a
Pterodactyl (Nodaroo) Python server. Built for AOX's "Kang Muse" server.

What it does
------------
* 2D cinematic renderer (default path, works with only Python + Pillow +
  ffmpeg; numpy is optional): takes a JSON scene spec and renders a
  video in the
  tetsuoai style AOX uses — dark background, pink/magenta accent
  (255, 90, 180), big kinetic headline, film grain + vignette,
  slow push-in, hard cuts. Default 1280x720 @ 24fps.
* Blender 3D path (optional, graceful): if a `blender` binary is on
  PATH, a .blend file can be uploaded and rendered headless, then
  encoded to mp4. If Blender is missing the job fails with a clear
  message instead of crashing the server.

Endpoints
---------
  GET  /              status JSON (alive, specs, capabilities, jobs)
  GET  /console       browser console page (one-click 2D renders)
  POST /render        submit a job (JSON for 2D, multipart for Blender)
  GET  /jobs          list jobs (status + progress)
  GET  /jobs/<id>     one job's status + log tail
  GET  /download/<id> download the finished mp4

Jobs run one at a time in a background worker thread — never two
renders at once on a small CPU-only server. Bad jobs fail with a log
entry; they never crash the server. All logging goes to stdout so it
shows up in the Pterodactyl console.

Environment
-----------
  PORT / SERVER_PORT   port to listen on (default 8080), bind 0.0.0.0
  MAX_TOTAL_SECONDS    max total seconds for a 2D job (default 120)
  MAX_BLENDER_FRAMES   max frames for a Blender job (default 600)

Only free / open-source tools are used.
"""

import base64
import collections
import html
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # slim install: flask + pillow only (see README)
    np = None
    _HAS_NUMPY = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

PORT = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT") or 8080)
MAX_TOTAL_SECONDS = float(os.environ.get("MAX_TOTAL_SECONDS", "120"))
MAX_BLENDER_FRAMES = int(os.environ.get("MAX_BLENDER_FRAMES", "600"))
MAX_SCENE_SECONDS = 30.0          # per-scene cap (protects small servers)
MAX_JOBS_KEPT = 50                # prune old finished jobs past this
ZOOM = 1.15                       # push-in zoom factor (base is 15% larger)
ACCENT = (255, 90, 180)           # pink/magenta accent (tetsuoai style)
WHITE = (255, 255, 255)
GREY = (205, 205, 210)
DARK_BG = "#0b0b16"

try:
    from flask import Flask, jsonify, request, send_file
except ImportError:  # pragma: no cover - handled at runtime with a clear error
    print("[FATAL] Flask is not installed. Add it to requirements.txt "
          "and restart the server.", flush=True)
    raise

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Startup diagnostics (printed to stdout -> Pterodactyl console)
# ---------------------------------------------------------------------------

def _total_ram_gb():
    """Best-effort total RAM in GiB (Linux /proc/meminfo, else unknown)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024.0 / 1024.0, 2)
    except Exception:
        pass
    return None


def _which_or_none(name):
    return shutil.which(name) or None


def ffmpeg_exe():
    """System ffmpeg first, else the imageio-ffmpeg bundled binary.

    imageio is OPTIONAL (not in the slim requirements.txt): this only
    works when imageio[ffmpeg] was installed, e.g. via PY_PACKAGES.
    Returns None when no ffmpeg is available at all.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def blender_exe():
    return shutil.which("blender")


def get_specs():
    disk = shutil.disk_usage(BASE_DIR)
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 1,
        "ram_gb": _total_ram_gb(),
        "disk_free_gb": round(disk.free / 1024 ** 3, 2),
        "disk_total_gb": round(disk.total / 1024 ** 3, 2),
        "ffmpeg": ffmpeg_exe(),
        "blender": blender_exe(),
        "node": _which_or_none("node"),
        "numpy": _HAS_NUMPY,
    }


def print_diagnostics():
    s = get_specs()
    print("=" * 62, flush=True)
    print("Nodaroo Cinematic Render Service — starting", flush=True)
    print(f"  Python   : {s['python']} ({s['platform']})", flush=True)
    print(f"  CPU      : {s['cpu_count']} cores", flush=True)
    print(f"  RAM      : {s['ram_gb']} GiB total" if s["ram_gb"] else "  RAM      : unknown", flush=True)
    print(f"  Disk     : {s['disk_free_gb']} GiB free / {s['disk_total_gb']} GiB total", flush=True)
    print(f"  ffmpeg   : {s['ffmpeg'] or 'NOT FOUND (2D render needs it)'}", flush=True)
    print(f"  numpy    : {'yes (fast 2D path)' if _HAS_NUMPY else 'no (slim install — pure-Pillow 2D path)'}", flush=True)
    print(f"  blender  : {s['blender'] or 'NOT FOUND (3D path disabled)'}", flush=True)
    print(f"  node     : {s['node'] or 'NOT FOUND (not required)'}", flush=True)
    print(f"  port     : {PORT} (bind 0.0.0.0)", flush=True)
    print(f"  jobs dir : {JOBS_DIR}", flush=True)
    print(f"  tunnel   : {'disabled (DISABLE_TUNNEL set)' if _tunnel_disabled() else 'starting in background — URL appears below + in GET / (public_url)'}", flush=True)
    print("=" * 62, flush=True)


# ---------------------------------------------------------------------------
# Cloudflare Tunnel — optional public URL (trycloudflare.com quick tunnel)
# ---------------------------------------------------------------------------
# The Nodaroo public address (au1.nodaroo.fun:25571) does not resolve or
# answer from the internet (23 Nov 2026: AOX's laptop got NXDOMAIN, the
# cloud browser got name_not_resolved, and the one resolver that did
# answer gave a 198.18.x.x address that never answers HTTP). The server
# has exactly 1 network allocation and no alternative public IP, so the
# app exposes itself through an OUTBOUND Cloudflare quick tunnel
# instead. No account, no token, no config: cloudflared dials out to
# Cloudflare and prints a public https://<random>.trycloudflare.com URL
# that forwards to localhost:PORT.
#
# The URL CHANGES on every restart — read it from the console log
# ("[tunnel] public URL: ...") or from GET / ("public_url").
#
# This is OPTIONAL: if the binary cannot be downloaded or the tunnel
# fails, the app still starts and serves locally (public_url stays
# null). It never blocks or crashes startup. Disable it entirely with
# the env var DISABLE_TUNNEL=1.
#
# Only the Python standard library is used (urllib, subprocess,
# threading) — no new dependency in requirements.txt.

CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/"
                   "latest/download/cloudflared-linux-amd64")
CLOUDFLARED_DIR = os.path.join(BASE_DIR, ".local", "bin")
CLOUDFLARED_BIN = os.path.join(CLOUDFLARED_DIR, "cloudflared")
_TUNNEL_URL_RE = re.compile(
    r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
# NOTE: the (?!api\.) lookahead matters — cloudflared logs the API
# endpoint https://api.trycloudflare.com while requesting the tunnel,
# and that is NOT the tunnel's public URL. The real quick-tunnel URL
# is a random word combo, e.g. https://word-word-word.trycloudflare.com.
PUBLIC_URL = None
_PUBLIC_URL_LOCK = threading.Lock()


def _tunnel_disabled():
    return os.environ.get("DISABLE_TUNNEL", "").strip().lower() in (
        "1", "true", "yes")


def get_public_url():
    with _PUBLIC_URL_LOCK:
        return PUBLIC_URL


def _set_public_url(url):
    global PUBLIC_URL
    with _PUBLIC_URL_LOCK:
        PUBLIC_URL = url


def ensure_cloudflared(timeout=180):
    """Download the cloudflared binary (Linux amd64) if it is missing.

    Downloads straight into the server dir (.local/bin), NOT /tmp —
    the Nodaroo container's pip temp area only fits ~40 MB, while the
    server dir has a 50 GB quota. Returns the binary path, or None
    when the download (or the sanity check) fails.
    """
    if os.path.isfile(CLOUDFLARED_BIN) and os.access(CLOUDFLARED_BIN, os.X_OK):
        return CLOUDFLARED_BIN
    try:
        os.makedirs(CLOUDFLARED_DIR, exist_ok=True)
    except Exception as exc:
        print(f"[tunnel] WARNING: cannot create {CLOUDFLARED_DIR} ({exc}) — "
              "continuing WITHOUT a public URL", flush=True)
        return None
    tmp_path = CLOUDFLARED_BIN + ".download"
    try:
        print(f"[tunnel] downloading cloudflared (~38 MB) ...", flush=True)
        req = urllib.request.Request(
            CLOUDFLARED_URL,
            headers={"User-Agent": "nodaroo-render/1.2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(tmp_path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
        print(f"[tunnel] downloaded {got / 1024 / 1024:.1f} MB"
              + (f" (expected {total / 1024 / 1024:.1f} MB)" if total else ""),
              flush=True)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, CLOUDFLARED_BIN)
        proc = subprocess.run([CLOUDFLARED_BIN, "--version"],
                              capture_output=True, text=True, timeout=30)
        ver = (proc.stdout or proc.stderr or "").strip().splitlines()
        print(f"[tunnel] cloudflared ready: {ver[0] if ver else 'ok'}",
              flush=True)
        return CLOUDFLARED_BIN
    except Exception as exc:
        print(f"[tunnel] WARNING: cloudflared download failed ({exc}) — "
              "continuing WITHOUT a public URL", flush=True)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return None


def _tunnel_reader(proc):
    """Drain cloudflared's output forever; pick up the public URL.

    Runs in a daemon thread. When the process exits, the public URL
    is cleared (the supervisor restarts the tunnel).
    """
    try:
        for line in proc.stdout:
            m = _TUNNEL_URL_RE.search(line)
            if m and get_public_url() != m.group(0):
                _set_public_url(m.group(0))
                print(f"[tunnel] public URL: {m.group(0)}", flush=True)
    except Exception:
        pass
    try:
        proc.wait()
    except Exception:
        pass
    print(f"[tunnel] cloudflared exited (code {proc.returncode}); "
          "public URL is dead", flush=True)
    _set_public_url(None)


def _start_tunnel_proc():
    """Start one cloudflared quick tunnel process. Returns it, or None."""
    exe = ensure_cloudflared()
    if not exe:
        return None
    cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{PORT}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as exc:
        print(f"[tunnel] WARNING: could not start cloudflared ({exc}) — "
              "continuing WITHOUT a public URL", flush=True)
        return None
    threading.Thread(target=_tunnel_reader, args=(proc,), daemon=True).start()
    return proc


def tunnel_loop():
    """Background supervisor: keep a cloudflared quick tunnel up.

    Runs forever as a daemon thread. If the tunnel dies (or never
    prints a URL), it restarts after a backoff. It never raises — a
    failed tunnel must never affect the Flask app.
    """
    if _tunnel_disabled():
        print("[tunnel] disabled via DISABLE_TUNNEL — no public URL",
              flush=True)
        return
    backoff = 5
    while True:
        try:
            proc = _start_tunnel_proc()
            if proc is None:
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            # wait until the URL appears or the process dies (<= 120s)
            deadline = time.time() + 120
            while time.time() < deadline:
                if get_public_url():
                    backoff = 5
                    break
                if proc.poll() is not None:
                    break
                time.sleep(1)
            if get_public_url():
                # tunnel is up; wait for it to die, then restart it
                while get_public_url() and proc.poll() is None:
                    time.sleep(5)
                print("[tunnel] tunnel went down; restarting...",
                      flush=True)
                continue
            print("[tunnel] cloudflared produced no URL; retrying...",
                  flush=True)
            try:
                proc.terminate()
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        except Exception as exc:  # supervisor must never die
            print(f"[tunnel] WARNING: supervisor error ({exc}); retrying...",
                  flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def start_tunnel_background():
    """Launch the tunnel supervisor in a daemon thread. Never raises."""
    try:
        t = threading.Thread(target=tunnel_loop, daemon=True)
        t.start()
    except Exception as exc:
        print(f"[tunnel] WARNING: could not start tunnel thread ({exc}) — "
              "continuing WITHOUT a public URL", flush=True)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def hex_to_rgb(value, default=(11, 11, 22)):
    """'#ff5ab4' -> (255, 90, 180). Accepts '#rgb', '#rrggbb', or a tuple."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(int(v) for v in value)
    if not isinstance(value, str):
        return default
    v = value.strip().lstrip("#")
    try:
        if len(v) == 3:
            return tuple(int(c * 2, 16) for c in v)
        if len(v) == 6:
            return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        pass
    return default


def darken(rgb, factor=0.45):
    return tuple(max(0, int(c * factor)) for c in rgb)


def load_font(size, bold=True):
    """Best bold sans font available; falls back to PIL's default font."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        os.path.join(BASE_DIR, "DejaVuSans-Bold.ttf"),
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, int(size))
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=int(size))
    except TypeError:
        return ImageFont.load_default()


def wrap_words(draw, words, font, max_width):
    """Greedy word-wrap. Returns a list of lines, each a list of words."""
    lines, current, cur_w = [], [], 0
    space_w = draw.textlength(" ", font=font)
    for word in words:
        ww = draw.textlength(word, font=font)
        add = ww if not current else space_w + ww
        if current and cur_w + add > max_width:
            lines.append(current)
            current, cur_w = [word], ww
        else:
            current.append(word)
            cur_w += add
    if current:
        lines.append(current)
    return lines


def draw_tracked(draw, xy, text, font, fill, tracking=3):
    """Draw text with extra letter spacing (for small caps labels)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def line_width(draw, words, font):
    """Pixel width of a word list joined by single spaces."""
    if not words:
        return 0
    w = sum(draw.textlength(w, font=font) for w in words)
    w += draw.textlength(" ", font=font) * (len(words) - 1)
    return w


# ---------------------------------------------------------------------------
# Job store + background worker
# ---------------------------------------------------------------------------

JOBS = {}                    # id -> job dict
JOBS_LOCK = threading.Lock()
QUEUE = collections.deque()  # job ids, FIFO


def new_job_id():
    return uuid.uuid4().hex[:8]


def job_log(job, message):
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    with JOBS_LOCK:
        job["log"].append(line)
        if len(job["log"]) > 300:
            job["log"] = job["log"][-300:]
    print(f"[job {job['id']}] {message}", flush=True)
    # also persist the log so a restart does not lose it
    try:
        with open(os.path.join(job["dir"], "log.txt"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def job_set(job, **fields):
    with JOBS_LOCK:
        job.update(fields)


def prune_old_jobs():
    """Keep at most MAX_JOBS_KEPT finished jobs (oldest removed first)."""
    with JOBS_LOCK:
        finished = [j for j in JOBS.values() if j["status"] in ("done", "failed")]
        if len(finished) <= MAX_JOBS_KEPT:
            return
        finished.sort(key=lambda j: j["created_at"])
        for old in finished[: len(finished) - MAX_JOBS_KEPT]:
            del JOBS[old["id"]]
            shutil.rmtree(old["dir"], ignore_errors=True)


def create_job(job_type, spec):
    jid = new_job_id()
    job_dir = os.path.join(JOBS_DIR, jid)
    os.makedirs(job_dir, exist_ok=True)
    job = {
        "id": jid,
        "type": job_type,
        "status": "queued",
        "progress": 0.0,
        "spec": spec,
        "dir": job_dir,
        "output": None,
        "error": None,
        "log": [],
        "created_at": time.time(),
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    QUEUE.append(jid)
    prune_old_jobs()
    return job


def worker_loop():
    """Process queued jobs one at a time, forever. Never crashes."""
    while True:
        try:
            jid = QUEUE.popleft()
        except IndexError:
            time.sleep(0.5)
            continue
        job = JOBS.get(jid)
        if job is None:
            continue
        try:
            job_set(job, status="running", progress=0.0)
            job_log(job, f"starting {job['type']} render")
            if job["type"] == "2d":
                run_2d_job(job)
            elif job["type"] == "blender":
                run_blender_job(job)
            else:
                raise ValueError(f"unknown job type: {job['type']}")
            job_set(job, status="done", progress=1.0)
            job_log(job, "DONE")
        except Exception as exc:  # a bad job must never kill the worker
            job_set(job, status="failed", error=str(exc))
            job_log(job, f"FAILED: {exc}")



# ---------------------------------------------------------------------------
# 2D cinematic renderer (Pillow, streamed to ffmpeg; numpy is an optional
# fast path — every numpy operation below has a pure-Pillow fallback)
# ---------------------------------------------------------------------------

_vignette_cache = {}


def vignette_mask(width, height, strength=0.42):
    """Radial darkening mask, cached per resolution. 1.0 = centre, ~0.6 = corner.

    numpy path returns a float32 array (H x W); the pure-Pillow fallback
    returns an "L" image (255 = centre, darker at the corners) computed
    on a small grid with the same formula and upscaled — the falloff is
    smooth, so the low-res mask is visually identical.
    """
    key = (width, height, strength, _HAS_NUMPY)
    if key in _vignette_cache:
        return _vignette_cache[key]
    if _HAS_NUMPY:
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        cx, cy = width / 2.0, height / 2.0
        dist = np.sqrt(((xx - cx) / (width * 0.62)) ** 2 + ((yy - cy) / (height * 0.62)) ** 2)
        mask = 1.0 - strength * np.clip(dist - 0.45, 0, 1) ** 2
        mask = np.clip(mask, 0, 1).astype(np.float32)
    else:
        gw, gh = 96, 54
        vals = []
        for gy in range(gh):
            for gx in range(gw):
                dx = ((gx + 0.5) / gw - 0.5) / 0.62
                dy = ((gy + 0.5) / gh - 0.5) / 0.62
                dist = math.sqrt(dx * dx + dy * dy)
                d = min(max(dist - 0.45, 0.0), 1.0)
                vals.append(int(round(255 * min(max(1.0 - strength * d * d, 0.0), 1.0))))
        mask = Image.new("L", (gw, gh))
        mask.putdata(vals)
        mask = mask.resize((width, height), Image.BICUBIC)
    _vignette_cache[key] = mask
    return mask


def vertical_gradient(width, height, top_rgb, bottom_rgb):
    """Smooth top->bottom gradient.

    numpy path returns a uint8 RGB array (H x W x 3); the pure-Pillow
    fallback returns a PIL RGB image (built as a 1 x H strip, then
    stretched — exact for a vertical gradient).
    """
    if _HAS_NUMPY:
        t = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
        top = np.array(top_rgb, dtype=np.float32)[None, None, :]
        bottom = np.array(bottom_rgb, dtype=np.float32)[None, None, :]
        grad = top * (1 - t) + bottom * t
        return np.repeat(grad.astype(np.uint8), width, axis=1)
    strip = Image.new("RGB", (1, height))
    rows = [tuple(int(round(top_rgb[c] + (bottom_rgb[c] - top_rgb[c])
                           * y / max(1, height - 1)))
                  for c in range(3)) for y in range(height)]
    strip.putdata(rows)
    return strip.resize((width, height))


def render_scene_base(scene, bw, bh):
    """Render one scene's static content at the (zoomed) base resolution.

    Returns a PIL RGB image of size (bw, bh). Text is drawn at base
    scale so the per-frame crop/resize gives the push-in motion.
    """
    bg_top = hex_to_rgb(scene.get("bg_color", DARK_BG))
    bg_bottom = hex_to_rgb(scene.get("bg_color2") or darken(bg_top, 0.4))

    base = None
    b64 = scene.get("bg_image_b64")
    if b64:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            # cover-fit: scale so the image covers bw x bh, then centre-crop
            scale = max(bw / img.width, bh / img.height)
            img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1),
                             Image.LANCZOS)
            left, top = (img.width - bw) // 2, (img.height - bh) // 2
            base = img.crop((left, top, left + bw, top + bh))
            # darken so text stays readable
            overlay = Image.new("RGB", (bw, bh), (0, 0, 0))
            base = Image.blend(base, overlay, 0.55)
        except Exception as exc:
            print(f"[render] bg_image_b64 failed ({exc}); using gradient", flush=True)
            base = None
    if base is None:
        if _HAS_NUMPY:
            arr = vertical_gradient(bw, bh, bg_top, bg_bottom)
            # soft radial glow, slightly above centre, in the accent hue
            yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
            glow = np.exp(-(((xx - bw * 0.5) / (bw * 0.55)) ** 2
                            + ((yy - bh * 0.38) / (bh * 0.55)) ** 2))
            glow = (glow * 22).astype(np.uint8)
            arr = np.clip(arr.astype(np.int16) + glow[:, :, None], 0, 255).astype(np.uint8)
            base = Image.fromarray(arr, "RGB")
        else:
            # pure Pillow: vertical gradient + the same elliptical glow,
            # computed on a small grid with the same formula and upscaled.
            base = vertical_gradient(bw, bh, bg_top, bg_bottom)
            gw, gh = 96, 54
            vals = []
            for gy in range(gh):
                for gx in range(gw):
                    fx = ((gx + 0.5) / gw - 0.5) / 0.55
                    fy = ((gy + 0.5) / gh - 0.38) / 0.55
                    vals.append(int(round(22 * math.exp(-(fx * fx + fy * fy)))))
            glow = Image.new("L", (gw, gh))
            glow.putdata(vals)
            glow = glow.resize((bw, bh), Image.BICUBIC)
            base = ImageChops.add(base, Image.merge("RGB", (glow, glow, glow)))

    draw = ImageDraw.Draw(base)
    accent = hex_to_rgb(scene.get("accent", "#ff5ab4"), ACCENT)
    headline_color = hex_to_rgb(scene.get("headline_color", "#ffffff"), WHITE)
    sub_color = hex_to_rgb(scene.get("subtext_color", "#cdccd2"), GREY)

    # NOTE: the top label is a HUD element — it is drawn on the FINAL
    # frame (see draw_label_overlay), not on the zooming base, so the
    # letterbox bars can never cover it.

    # --- headline (wrapped, centred, accent word in pink) ---
    headline = (scene.get("headline") or "").strip()
    subtext = (scene.get("subtext") or "").strip()
    accent_word = (scene.get("accent_word") or "").strip().lower()

    def is_accent(word):
        return accent_word and word.strip(".,!?\"'").lower() == accent_word

    if headline:
        hs = float(scene.get("headline_size", 0.085))  # fraction of width
        hf = load_font(int(bw * hs), bold=True)
        max_w = int(bw * 0.86)
        lines = wrap_words(draw, headline.split(), hf, max_w)
        line_h = int(bw * hs * 1.18)
        total_h = line_h * len(lines)
        # headline block sits just below centre (room for subtext underneath)
        y = int(bh * 0.52) - total_h // 2
        for line in lines:
            lw = line_width(draw, line, hf)
            x = (bw - lw) // 2
            for word in line:
                fill = accent if is_accent(word) else headline_color
                draw.text((x, y), word, font=hf, fill=fill)
                x += draw.textlength(word, font=hf) + draw.textlength(" ", font=hf)
            y += line_h
        sub_y = y + int(bh * 0.03)
    else:
        sub_y = int(bh * 0.55)

    # --- subtext ---
    if subtext:
        sf = load_font(int(bw * 0.032), bold=False)
        # wrap subtext too (it can be a full sentence)
        sub_lines = wrap_words(draw, subtext.split(), sf, int(bw * 0.80))
        sy = sub_y
        for line in sub_lines:
            lw = line_width(draw, line, sf)
            draw.text(((bw - lw) // 2, sy), " ".join(line), font=sf, fill=sub_color)
            sy += int(bw * 0.032 * 1.5)

    return base


def finish_frame(frame_pil, width, height, grain, letterbox, vig):
    """Grain + vignette (+ optional letterbox) on the final W x H frame."""
    if _HAS_NUMPY:
        if grain and grain > 0:
            arr = np.asarray(frame_pil).astype(np.int16)
            noise = np.random.randint(-int(grain), int(grain) + 1,
                                      size=(height, width, 1), dtype=np.int16)
            arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
            frame_pil = Image.fromarray(arr, "RGB")
        if vig is not None:
            arr = np.asarray(frame_pil).astype(np.float32) * vig[:, :, None]
            frame_pil = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    else:
        if grain and grain > 0:
            # effect_noise is Gaussian around 128; adding it with an
            # offset of -128 adds ~zero-mean noise, like the numpy path.
            # sigma ~ grain/sqrt(3) matches the numpy path's uniform
            # +/-grain spread (same standard deviation, same look).
            noise = Image.effect_noise((width, height),
                                       max(1, int(grain * 0.6))).convert("RGB")
            frame_pil = ImageChops.add(frame_pil, noise, 1.0, -128)
        if vig is not None:
            # vig is an "L" mask here: composite the frame over black.
            frame_pil = Image.composite(frame_pil,
                                        Image.new("RGB", (width, height), (0, 0, 0)),
                                        vig)
    if letterbox:
        draw = ImageDraw.Draw(frame_pil)
        bar = int(height * 0.09)
        draw.rectangle([0, 0, width, bar], fill=(0, 0, 0))
        draw.rectangle([0, height - bar, width, height], fill=(0, 0, 0))
    return frame_pil


def draw_label_overlay(frame_pil, scene, width, height, letterbox):
    """Draw the small top label (e.g. 'MESSY VIRGO') + accent rule.

    This is a HUD element: it is drawn on the final W x H frame, AFTER
    the push-in crop/resize, so it never zooms and the letterbox bars
    can never cover it. When letterbox is on it sits just below the top
    bar; otherwise near the top edge.
    """
    label = (scene.get("label") or "").strip().upper()
    if not label:
        return
    accent = hex_to_rgb(scene.get("accent", "#ff5ab4"), ACCENT)
    bar = int(height * 0.09) if letterbox else 0
    lf = load_font(int(height * 0.034), bold=True)
    draw = ImageDraw.Draw(frame_pil)
    x = int(width * 0.06)
    y = bar + int(height * 0.025) if letterbox else int(height * 0.055)
    draw_tracked(draw, (x, y), label, lf, accent, tracking=4)
    rule_y = y + int(height * 0.052)
    draw.line([(x, rule_y), (x + int(width * 0.09), rule_y)],
              fill=accent, width=max(2, int(height * 0.006)))


def iter_scene_frames(scene, width, height, fps, grain, letterbox, vig):
    """Yield RGB frames (W x H) for one scene, with its motion effect.

    numpy path yields uint8 arrays; the pure-Pillow fallback yields PIL
    RGB images. Both expose .tobytes() in raw rgb24 layout, which is
    what the ffmpeg encoder consumes.
    """
    n = max(1, int(round(float(scene.get("duration", 3.0)) * fps)))
    bw, bh = int(width * ZOOM), int(height * ZOOM)
    base = render_scene_base(scene, bw, bh)
    effect = (scene.get("effect") or "push-in").lower()
    for t in range(n):
        p = t / max(1, n - 1)
        if effect == "push-in":
            zoom = ZOOM - (ZOOM - 1.0) * p      # crop shrinks: Z -> 1.0
        elif effect == "zoom-out":
            zoom = 1.0 + (ZOOM - 1.0) * p        # crop grows: 1.0 -> Z
        else:                                    # static
            zoom = ZOOM
        cw, ch = int(width * zoom), int(height * zoom)
        left, top = (bw - cw) // 2, (bh - ch) // 2
        frame = base.crop((left, top, left + cw, top + ch))
        if (cw, ch) != (width, height):
            frame = frame.resize((width, height), Image.LANCZOS)
        frame = finish_frame(frame, width, height, grain, letterbox, vig)
        draw_label_overlay(frame, scene, width, height, letterbox)
        yield np.asarray(frame) if _HAS_NUMPY else frame


def frame_stream(job):
    """Yield every frame of a 2D job, honouring cut/fade transitions.

    Only a small tail buffer (<= fade_frames) is held in memory, so a
    long job never balloons RAM on a small server.
    """
    scenes = job["spec"]["scenes"]
    fps = job["spec"]["fps"]
    width, height = job["spec"]["width"], job["spec"]["height"]
    grain = int(job["spec"].get("grain", 7))
    letterbox = bool(job["spec"].get("letterbox", False))
    fade = max(0, int(job["spec"].get("fade_frames", 12)))
    vig = vignette_mask(width, height) if job["spec"].get("vignette", True) else None

    pending = collections.deque()  # un-emitted tail frames of previous scene
    for idx, scene in enumerate(scenes):
        n = max(1, int(round(float(scene.get("duration", 3.0)) * fps)))
        transition = (scene.get("transition") or "cut").lower() if idx > 0 else "cut"
        gen = iter_scene_frames(scene, width, height, fps, grain, letterbox, vig)
        use_fade = (transition == "fade" and fade > 0
                    and len(pending) == fade and n >= fade)
        if use_fade:
            for j in range(fade):
                a = pending.popleft()
                b = next(gen)
                alpha = (j + 1) / (fade + 1)
                if _HAS_NUMPY:
                    blended = (a.astype(np.float32) * (1 - alpha)
                               + b.astype(np.float32) * alpha)
                    yield np.clip(blended, 0, 255).astype(np.uint8)
                else:
                    yield Image.blend(a, b, alpha)
            remaining = n - fade
        else:
            while pending:
                yield pending.popleft()
            remaining = n
        for _ in range(remaining):
            frame = next(gen)
            pending.append(frame)
            if len(pending) > fade:
                yield pending.popleft()
    while pending:
        yield pending.popleft()


def encode_frames_to_mp4(frame_iter, out_path, width, height, fps, job, total_frames):
    """Stream raw RGB frames into ffmpeg (system binary or imageio-ffmpeg)."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError(
            "No ffmpeg available on this server. Install ffmpeg, or add "
            "imageio[ffmpeg] to the panel's PY_PACKAGES field (it is an "
            "optional enhancement, not in the slim requirements.txt) so a "
            "bundled binary is used.")
    cmd = [exe, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
           "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", "20", "-preset", "veryfast", "-movflags", "+faststart",
           "-loglevel", "error",
           out_path]
    # ffmpeg stderr goes to a file (not a pipe): a pipe can fill up and
    # deadlock the encode, and communicate() after a manual stdin.close()
    # raises "flush of closed file".
    err_path = os.path.join(job["dir"], "ffmpeg-err.log")
    done = 0
    with open(err_path, "wb") as err_file:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=err_file)
        try:
            for frame in frame_iter:
                proc.stdin.write(frame.tobytes())
                done += 1
                if total_frames and done % 24 == 0:
                    job_set(job, progress=0.05 + 0.9 * done / total_frames)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
        proc.wait()
    with open(err_path, "rb") as f:
        err = f.read()
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-1500:] if err else ""
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {tail}")
    if done == 0:
        raise RuntimeError("no frames were rendered")
    return done


def run_2d_job(job):
    spec = job["spec"]
    scenes = spec.get("scenes") or []
    if not scenes:
        raise ValueError("2D job needs at least one scene")
    width = int(spec.get("width", 1280))
    height = int(spec.get("height", 720))
    fps = int(spec.get("fps", 24))
    if width < 160 or height < 90 or width > 3840 or height > 2160:
        raise ValueError(f"bad resolution {width}x{height} (160x90 .. 3840x2160)")
    if fps < 1 or fps > 60:
        raise ValueError(f"bad fps {fps} (1..60)")
    total_seconds = sum(min(MAX_SCENE_SECONDS, float(s.get("duration", 3.0)))
                        for s in scenes)
    if total_seconds > MAX_TOTAL_SECONDS:
        raise ValueError(f"total duration {total_seconds:.1f}s exceeds the "
                         f"{MAX_TOTAL_SECONDS:.0f}s limit for this server")
    total_frames = sum(max(1, int(round(float(s.get("duration", 3.0)) * fps)))
                       for s in scenes)
    out_name = spec.get("output_name") or f"render-{job['id']}.mp4"
    out_name = os.path.basename(out_name)
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_path = os.path.join(job["dir"], out_name)

    job_log(job, f"2D render: {len(scenes)} scene(s), {width}x{height}@{fps}, "
                 f"{total_seconds:.1f}s ({total_frames} frames)")
    job_set(job, progress=0.05)
    frames = encode_frames_to_mp4(frame_stream(job), out_path, width, height,
                                  fps, job, total_frames)
    job["output"] = out_path
    job_log(job, f"encoded {frames} frames -> {out_name} "
                 f"({os.path.getsize(out_path)} bytes)")


# ---------------------------------------------------------------------------
# Blender 3D path (optional — graceful when Blender is missing)
# ---------------------------------------------------------------------------

def normalize_engine(engine, blender):
    """Map an EEVEE engine name to what the installed Blender accepts.

    Blender 4.2+ calls it BLENDER_EEVEE; 4.1 and earlier call the new
    EEVEE BLENDER_EEVEE_NEXT. Accept either spelling from the caller
    and return the one this Blender version understands. CYCLES and
    BLENDER_WORKBENCH pass through unchanged.
    """
    if engine not in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        return engine
    try:
        out = subprocess.run([blender, "--version"], capture_output=True,
                             text=True, timeout=30).stdout
        # first line: "Blender 5.3.0 ..."
        ver = out.splitlines()[0].split()[1]
        major, minor = (int(x) for x in ver.split(".")[:2])
        modern = (major, minor) >= (4, 2)
    except Exception:
        modern = True  # assume a modern Blender
    return "BLENDER_EEVEE" if modern else "BLENDER_EEVEE_NEXT"


def run_blender_job(job):
    spec = job["spec"]
    blender = blender_exe()
    if not blender:
        raise RuntimeError(
            "Blender is NOT installed on this server, so the 3D path cannot "
            "run. Use the 2D path instead, or install Blender on the server "
            "first (the service never downloads Blender by itself).")
    blend_path = spec.get("blend_path")
    if not blend_path or not os.path.exists(blend_path):
        raise ValueError("Blender job needs an uploaded .blend file "
                         "(multipart field 'blend')")
    start = int(spec.get("frame_start", 1))
    end = int(spec.get("frame_end", 24))
    fps = int(spec.get("fps", 24))
    res_x = int(spec.get("resolution_x", spec.get("width", 1280)))
    res_y = int(spec.get("resolution_y", spec.get("height", 720)))
    engine = normalize_engine(spec.get("engine", "BLENDER_EEVEE"), blender)
    if end < start or (end - start + 1) > MAX_BLENDER_FRAMES:
        raise ValueError(f"frame range {start}-{end} invalid or over the "
                         f"{MAX_BLENDER_FRAMES}-frame limit")
    if fps < 1 or fps > 60:
        raise ValueError(f"bad fps {fps} (1..60)")

    frames_dir = os.path.join(job["dir"], "frames")
    os.makedirs(frames_dir, exist_ok=True)
    out_name = spec.get("output_name") or f"render-{job['id']}.mp4"
    out_name = os.path.basename(out_name)
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_path = os.path.join(job["dir"], out_name)

    # Set resolution/fps/format inside Blender (the .blend may say otherwise).
    py_expr = ("import bpy; s=bpy.context.scene; "
               f"s.render.resolution_x={res_x}; s.render.resolution_y={res_y}; "
               f"s.render.fps={fps}; "
               "s.render.image_settings.file_format='PNG';")
    cmd = [blender, "-b", blend_path, "-E", engine,
           "--python-expr", py_expr,
           "-o", os.path.join(frames_dir, "frame_####"),
           "-s", str(start), "-e", str(end), "-a"]
    job_log(job, f"Blender: {engine}, frames {start}-{end}, {res_x}x{res_y}@{fps}")
    job_set(job, progress=0.05)
    # Blender's stdout goes to a file: a pipe can fill up on a long render
    # and deadlock the process.
    blender_log = os.path.join(job["dir"], "blender.log")
    with open(blender_log, "w") as log_file:
        proc = subprocess.run(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                              text=True, timeout=7200)
    with open(blender_log) as f:
        tail = f.read()[-2000:]
    job_log(job, "blender output tail:\n" + tail)
    if proc.returncode != 0:
        raise RuntimeError(f"Blender failed (exit {proc.returncode}). "
                           f"Tail: {tail}")
    job_set(job, progress=0.7)

    # Encode the PNG sequence to mp4.
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("Blender rendered the frames, but no ffmpeg is "
                           "available to encode them to mp4.")
    pattern = os.path.join(frames_dir, "frame_%04d.png")
    cmd = [exe, "-y", "-framerate", str(fps), "-i", pattern,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-preset", "veryfast", "-movflags", "+faststart",
           "-loglevel", "error", out_path]
    enc_log = os.path.join(job["dir"], "ffmpeg-err.log")
    with open(enc_log, "w") as log_file:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=log_file, text=True, timeout=3600)
    if proc.returncode != 0:
        with open(enc_log) as f:
            err_tail = f.read()[-1500:]
        raise RuntimeError("ffmpeg encode failed: " + err_tail)
    job["output"] = out_path
    job_log(job, f"encoded Blender frames -> {out_name} "
                 f"({os.path.getsize(out_path)} bytes)")


# ---------------------------------------------------------------------------
# Browser console page (GET /console)
# ---------------------------------------------------------------------------
# AOX cannot run curl (the Pterodactyl console is not a shell) and has
# no Postman, and Muse's automated traffic is blocked by Cloudflare
# bot protection — so the service serves its own one-click console
# page. Same origin as the API, so no CORS is needed. One
# self-contained HTML page (inline CSS + JS, no external dependencies
# or CDN), dark theme with the pink Messy accent (#ff5ab4).
#
# The page pre-fills the job JSON from example-2d-job.json when that
# file is present next to app.py; otherwise it uses the built-in copy
# below (the example file is an optional upload, so the console must
# work without it).

DEFAULT_EXAMPLE_JOB = {
    "type": "2d",
    "width": 1280,
    "height": 720,
    "fps": 24,
    "letterbox": True,
    "grain": 7,
    "fade_frames": 12,
    "output_name": "the-red-screen-test.mp4",
    "scenes": [
        {"headline": "THE RED SCREEN", "accent_word": "RED",
         "subtext": "Saturday night. Every screen is red.",
         "label": "MESSY VIRGO", "bg_color": "#0b0b16",
         "bg_color2": "#2a0f2e", "duration": 3.0, "effect": "push-in",
         "transition": "cut"},
        {"headline": "CHECK FIRST", "accent_word": "CHECK",
         "subtext": "Macro score 46. Neutral. Not panic.",
         "label": "MESSY VIRGO", "bg_color": "#0b0b16",
         "bg_color2": "#0f1a2e", "duration": 3.0, "effect": "push-in",
         "transition": "fade"},
        {"headline": "DID NOT SELL", "accent_word": "SELL",
         "subtext": "A red screen is not a verdict.",
         "label": "MESSY VIRGO", "bg_color": "#0b0b16",
         "bg_color2": "#2a0f2e", "duration": 3.0, "effect": "zoom-out",
         "transition": "cut"},
    ],
}


def load_example_job():
    """The example 2D job: example-2d-job.json if present, else the
    built-in copy above."""
    try:
        with open(os.path.join(BASE_DIR, "example-2d-job.json")) as f:
            job = json.load(f)
        if isinstance(job, dict) and job.get("scenes"):
            return job
    except Exception:
        pass
    return json.loads(json.dumps(DEFAULT_EXAMPLE_JOB))


CONSOLE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Render Console — Nodaroo</title>
<style>
:root{--bg:#0b0b16;--panel:#151528;--text:#e8e8f0;--muted:#9a9ab0;--accent:#ff5ab4;--border:#2a2a44;--ok:#3ddc84;--err:#ff5a5a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
h1 span{color:var(--accent)}
p.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
.meta{font-size:13px;color:var(--muted);background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:18px;word-break:break-all}
.meta a{color:var(--accent)}
label{display:block;font-size:13px;color:var(--muted);margin:14px 0 5px}
input[type=text],textarea{width:100%;background:#0f0f1e;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:15px}
input[type=text]:focus,textarea:focus{outline:none;border-color:var(--accent)}
textarea{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;min-height:280px;resize:vertical}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
button{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:11px 20px;font-size:15px;font-weight:600;cursor:pointer}
button.secondary{background:transparent;color:var(--text);border:1px solid var(--border)}
button:disabled{opacity:.5;cursor:wait}
#status{margin-top:18px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:14px;display:none}
#status .jid{font-family:ui-monospace,Menlo,Consolas,monospace}
#status .done a{color:var(--ok);font-weight:700}
#log{white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);max-height:220px;overflow:auto;margin-top:10px}
.hint{font-size:12px;color:var(--muted);margin-top:4px}
</style>
</head>
<body>
<div class="wrap">
<h1>Nodaroo <span>Render Console</span></h1>
<p class="sub">2D cinematic render — edit, hit Render, download the mp4. No curl, no Postman.</p>
<div class="meta" id="meta">Public URL: <span id="puburl">loading…</span> · <a href="/">raw status JSON</a></div>
<label for="headline">Headline</label>
<input type="text" id="headline" value="__HEADLINE__">
<div class="hint">Big bold centred headline. The accent word below is drawn in pink.</div>
<label for="accent_word">Accent word (pink)</label>
<input type="text" id="accent_word" value="__ACCENT_WORD__">
<div class="hint">One word from the headline — that word renders in pink (#ff5ab4).</div>
<label for="subtext">Subtext</label>
<input type="text" id="subtext" value="__SUBTEXT__">
<div class="hint">Smaller grey line under the headline.</div>
<label for="label">Label (top-left)</label>
<input type="text" id="label" value="__LABEL__">
<div class="hint">Small pink caps label, e.g. MESSY VIRGO.</div>
<label for="jobjson">Job JSON (scenes)</label>
<textarea id="jobjson" spellcheck="false">__EXAMPLE_JSON__</textarea>
<div class="hint">The fields above edit scene 1 of this JSON. Edit the JSON directly for multiple scenes, timings, effects and colours — it is the source of truth for the render.</div>
<div class="row">
<button id="renderBtn" type="button">Render</button>
<button id="resetBtn" class="secondary" type="button">Reset example</button>
</div>
<div id="status"></div>
</div>
<script>
(function(){
var $=function(id){return document.getElementById(id)};
var jobEl=$('jobjson'),statusEl=$('status'),renderBtn=$('renderBtn');
var pollTimer=null;
var EXAMPLE_JSON=jobEl.value;
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function showStatus(html){statusEl.style.display='block';statusEl.innerHTML=html}
function parseJob(){try{return{ok:true,job:JSON.parse(jobEl.value)}}catch(e){return{ok:false,error:String(e)}}}
function fillFieldsFromJob(){
  var r=parseJob();
  if(!r.ok||!Array.isArray(r.job.scenes)||!r.job.scenes.length)return;
  var s=r.job.scenes[0];
  $('headline').value=s.headline||'';
  $('accent_word').value=s.accent_word||'';
  $('subtext').value=s.subtext||'';
  $('label').value=s.label||'';
}
function applyFieldsToJob(){
  var r=parseJob();
  if(!r.ok||!Array.isArray(r.job.scenes)||!r.job.scenes.length)return;
  var s=r.job.scenes[0];
  s.headline=$('headline').value;
  s.accent_word=$('accent_word').value;
  s.subtext=$('subtext').value;
  s.label=$('label').value;
  jobEl.value=JSON.stringify(r.job,null,2);
}
['headline','accent_word','subtext','label'].forEach(function(id){
  $(id).addEventListener('input',applyFieldsToJob);
});
$('resetBtn').addEventListener('click',function(){
  jobEl.value=EXAMPLE_JSON;
  fillFieldsFromJob();
  if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  statusEl.style.display='none';
});
renderBtn.addEventListener('click',function(){
  var r=parseJob();
  if(!r.ok){showStatus('<b>Invalid JSON:</b> '+esc(r.error));return}
  renderBtn.disabled=true;
  renderBtn.textContent='Submitting…';
  fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(r.job)})
    .then(function(resp){return resp.json().then(function(data){return{ok:resp.ok,data:data}})})
    .then(function(res){
      if(!res.ok)throw new Error(res.data.error||('HTTP error'));
      showStatus('Job <span class="jid">'+esc(res.data.job_id)+'</span> — queued. Rendering…');
      poll(res.data.job_id);
    })
    .catch(function(e){
      showStatus('<b style="color:var(--err)">Failed to submit:</b> '+esc(String(e)));
      renderBtn.disabled=false;
      renderBtn.textContent='Render';
    });
});
function poll(jid){
  if(pollTimer)clearInterval(pollTimer);
  var tick=function(){
    fetch('/jobs/'+encodeURIComponent(jid))
      .then(function(resp){return resp.json().then(function(data){return{ok:resp.ok,data:data}})})
      .then(function(res){
        if(!res.ok)throw new Error(res.data.error||('HTTP error'));
        var d=res.data;
        var pct=Math.round((d.progress||0)*100);
        var html='Job <span class="jid">'+esc(d.id)+'</span> — <b>'+esc(d.status)+'</b> ('+pct+'%)';
        if(d.error)html+='<br><b style="color:var(--err)">Error:</b> '+esc(d.error);
        if(d.status==='done'){
          if(pollTimer){clearInterval(pollTimer);pollTimer=null}
          html+='<br><span class="done"><a href="/download/'+encodeURIComponent(d.id)+'">&#11015; Download '+(esc(d.output||'video'))+'</a></span>';
          renderBtn.disabled=false;
          renderBtn.textContent='Render';
        }else if(d.status==='failed'){
          if(pollTimer){clearInterval(pollTimer);pollTimer=null}
          renderBtn.disabled=false;
          renderBtn.textContent='Render';
        }
        if(d.log&&d.log.length)html+='<div id="log">'+esc(d.log.slice(-15).join('\\n'))+'</div>';
        showStatus(html);
      })
      .catch(function(e){
        if(pollTimer){clearInterval(pollTimer);pollTimer=null}
        showStatus('<b style="color:var(--err)">Status check failed:</b> '+esc(String(e)));
        renderBtn.disabled=false;
        renderBtn.textContent='Render';
      });
  };
  pollTimer=setInterval(tick,2000);
  tick();
}
fetch('/').then(function(r){return r.json()}).then(function(d){
  var el=$('puburl');
  if(d.public_url){el.innerHTML='<a href="'+esc(d.public_url)+'">'+esc(d.public_url)+'</a>'}
  else{el.textContent='(tunnel URL not up yet — this page’s address is the service)'}
}).catch(function(){$('puburl').textContent='(could not load status)'});
})();
</script>
</body>
</html>
"""


def console_page_html():
    """Render the console page with the example job pre-filled."""
    job = load_example_job()
    scenes = job.get("scenes") or []
    scene = scenes[0] if scenes else {}
    page = CONSOLE_PAGE
    page = page.replace("__EXAMPLE_JSON__",
                        html.escape(json.dumps(job, indent=2)))
    page = page.replace("__HEADLINE__",
                        html.escape(str(scene.get("headline", "")), quote=True))
    page = page.replace("__ACCENT_WORD__",
                        html.escape(str(scene.get("accent_word", "")), quote=True))
    page = page.replace("__SUBTEXT__",
                        html.escape(str(scene.get("subtext", "")), quote=True))
    page = page.replace("__LABEL__",
                        html.escape(str(scene.get("label", "")), quote=True))
    return page


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

def job_summary(job):
    with JOBS_LOCK:
        return {
            "id": job["id"],
            "type": job["type"],
            "status": job["status"],
            "progress": round(job["progress"], 3),
            "error": job["error"],
            "output": (os.path.basename(job["output"])
                       if job.get("output") else None),
            "created_at": job["created_at"],
        }


@app.route("/", methods=["GET"])
def index():
    specs = get_specs()
    with JOBS_LOCK:
        counts = collections.Counter(j["status"] for j in JOBS.values())
    return jsonify({
        "service": "nodaroo-cinematic-render",
        "alive": True,
        "version": "1.3.0",
        "console": "/console",
        "public_url": get_public_url(),
        "specs": {
            "cpu_count": specs["cpu_count"],
            "ram_gb": specs["ram_gb"],
            "disk_free_gb": specs["disk_free_gb"],
            "disk_total_gb": specs["disk_total_gb"],
            "python": specs["python"],
        },
        "capabilities": {
            "ffmpeg": bool(specs["ffmpeg"]),
            "ffmpeg_exe": specs["ffmpeg"],
            "numpy": bool(specs["numpy"]),
            "blender": bool(specs["blender"]),
            "blender_exe": specs["blender"],
            "node": bool(specs["node"]),
            "paths": ["2d", "blender" if specs["blender"] else "2d-only"],
        },
        "jobs": dict(counts),
        "limits": {
            "max_2d_seconds": MAX_TOTAL_SECONDS,
            "max_blender_frames": MAX_BLENDER_FRAMES,
        },
        "endpoints": ["GET /", "GET /console", "POST /render", "GET /jobs",
                      "GET /jobs/<id>", "GET /download/<id>"],
    })


@app.route("/console", methods=["GET"])
def console():
    """Browser console page — one-click 2D renders (the page itself is
    built by console_page_html() in the console section above)."""
    return console_page_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/render", methods=["POST"])
def submit_render():
    """Submit a render job.

    2D (JSON body):
        {"type": "2d", "width": 1280, "height": 720, "fps": 24,
         "letterbox": true, "grain": 7,
         "scenes": [{"headline": "...", "subtext": "...", "label": "...",
                     "accent_word": "RED", "bg_color": "#0b0b16",
                     "duration": 3.0, "effect": "push-in",
                     "transition": "cut"}]}
    Blender (multipart/form-data):
        type=blender, blend=<.blend file>, frame_start, frame_end,
        fps, resolution_x, resolution_y, engine, output_name
    """
    try:
        # --- Blender path: multipart upload ---
        if request.files and "blend" in request.files:
            f = request.files["blend"]
            if not f.filename.lower().endswith(".blend"):
                return jsonify({"error": "uploaded file must be a .blend file"}), 400
            spec = {
                "frame_start": int(request.form.get("frame_start", 1)),
                "frame_end": int(request.form.get("frame_end", 24)),
                "fps": int(request.form.get("fps", 24)),
                "resolution_x": int(request.form.get("resolution_x", 1280)),
                "resolution_y": int(request.form.get("resolution_y", 720)),
                "engine": request.form.get("engine", "BLENDER_EEVEE_NEXT"),
                "output_name": request.form.get("output_name") or None,
            }
            job = create_job("blender", spec)
            blend_path = os.path.join(job["dir"], "scene.blend")
            f.save(blend_path)
            spec["blend_path"] = blend_path
            job_log(job, f"Blender job accepted: {f.filename} "
                         f"frames {spec['frame_start']}-{spec['frame_end']}")
            return jsonify({"job_id": job["id"], "status": "queued",
                            "type": "blender"}), 202

        # --- 2D path: JSON body ---
        spec = request.get_json(force=True, silent=True)
        if not isinstance(spec, dict):
            return jsonify({"error": "POST a JSON 2D job, or a multipart "
                                     "form with a .blend file"}), 400
        if spec.get("type", "2d") != "2d":
            return jsonify({"error": "unknown job type "
                                     f"'{spec.get('type')}' (use '2d' or a "
                                     "multipart .blend upload)"}), 400
        scenes = spec.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            return jsonify({"error": "2D job needs a non-empty 'scenes' list"}), 400
        total = sum(min(MAX_SCENE_SECONDS, float(s.get("duration", 3.0)))
                    for s in scenes if isinstance(s, dict))
        if total > MAX_TOTAL_SECONDS:
            return jsonify({"error": f"total duration {total:.1f}s exceeds "
                                     f"the {MAX_TOTAL_SECONDS:.0f}s limit"}), 400
        job = create_job("2d", spec)
        job_log(job, f"2D job accepted: {len(scenes)} scene(s), "
                     f"{total:.1f}s total")
        return jsonify({"job_id": job["id"], "status": "queued",
                        "type": "2d"}), 202
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"bad job spec: {exc}"}), 400
    except Exception as exc:  # never 500 on a bad request
        return jsonify({"error": str(exc)}), 400


@app.route("/jobs", methods=["GET"])
def list_jobs():
    with JOBS_LOCK:
        jobs = [job_summary(j) for j in
                sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)]
    return jsonify({"jobs": jobs})


@app.route("/jobs/<jid>", methods=["GET"])
def get_job(jid):
    job = JOBS.get(jid)
    if job is None:
        return jsonify({"error": f"no such job: {jid}"}), 404
    out = job_summary(job)
    with JOBS_LOCK:
        out["log"] = job["log"][-50:]
    return jsonify(out)


@app.route("/download/<jid>", methods=["GET"])
def download_job(jid):
    job = JOBS.get(jid)
    if job is None:
        return jsonify({"error": f"no such job: {jid}"}), 404
    if job["status"] != "done" or not job.get("output") \
            or not os.path.exists(job["output"]):
        return jsonify({"error": f"job {jid} is {job['status']}; "
                                 "nothing to download yet"}), 409
    return send_file(job["output"], as_attachment=True,
                     download_name=os.path.basename(job["output"]),
                     mimetype="video/mp4")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print_diagnostics()
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    # Cloudflare quick tunnel (optional, non-blocking): gives the service
    # a public https://<random>.trycloudflare.com URL because the
    # Nodaroo public address does not resolve/answer. If it fails,
    # the app still serves locally — public_url just stays null.
    start_tunnel_background()
    print(f"[ready] listening on 0.0.0.0:{PORT} — "
          f"GET / for status, POST /render to queue a job", flush=True)
    # threaded dev server is fine for a single-user render box
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
