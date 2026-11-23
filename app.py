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
import io
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
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
    print("=" * 62, flush=True)


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
        "version": "1.1.0",
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
        "endpoints": ["GET /", "POST /render", "GET /jobs",
                      "GET /jobs/<id>", "GET /download/<id>"],
    })


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
    print(f"[ready] listening on 0.0.0.0:{PORT} — "
          f"GET / for status, POST /render to queue a job", flush=True)
    # threaded dev server is fine for a single-user render box
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
