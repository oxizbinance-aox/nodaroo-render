# Nodaroo Cinematic Render Service

A small Flask service that renders cinematic 2D/3D videos on a Nodaroo
(Pterodactyl) Python server. Built for AOX's **"Kang Muse"** server.

* **2D cinematic renderer** (default, works with only Python + Pillow +
  numpy + ffmpeg): JSON scene spec in, mp4 out. Tetsuoai style — dark
  background, pink/magenta accent (`#ff5ab4`), big kinetic headline,
  film grain + vignette, slow push-in, hard cuts. Default 1280×720 @ 24fps.
* **Blender 3D path** (optional): upload a `.blend` file and it renders
  headless, then encodes to mp4. Needs a `blender` binary on the server;
  if it is missing the job fails with a clear message instead of
  crashing the service.

Target runtime: **Python 3.12** (the Nodaroo container runs Python 3.12.13).
All dependencies in `requirements.txt` are Python 3.12 compatible.
Only free / open-source tools are used.

---

## 1. Upload to Nodaroo — PRIMARY install path (works while offline)

> **Important:** the Pterodactyl console only accepts typed input while
> the server is **RUNNING**. When the server is offline (for example
> crashed on the missing `app.py`) the console is just a log viewer —
> `git clone` typed there does nothing. That is a chicken-and-egg:
> the server needs `app.py` to run, and the terminal needs a running
> server to clone it. **So the first install must go through the
> File access tab, which works while the server is offline.**
> GitHub (section 6) is for *updates* after the server is already running.

1. In the Nodaroo panel, open the server **"Kang Muse"** → **File access** tab.
2. Upload these two files into `/home/container/` (the server root):
   - `app.py`
   - `requirements.txt`
3. (Optional) also upload `example-2d-job.json` — it is only a test job,
   the server does not need it to run.
4. Go back to the console and click **Start — ONCE — and wait.**
   The egg's startup script installs `requirements.txt` automatically
   (`pip install -U --prefix .local -r requirements.txt`), then runs
   `python /home/container/app.py`. First start takes 1–3 minutes
   while pip installs Flask/Pillow/numpy/imageio.

> **Do NOT click Start repeatedly.** The panel rejects duplicate power
> actions with *"another power action is currently being processed for
> this server"*. One click, then wait for the console to show
> `[ready] listening on 0.0.0.0:8080`.

You should see diagnostics like this in the console:

```
Nodaroo Cinematic Render Service — starting
  Python   : 3.12.13 (...)
  CPU      : 2 cores
  RAM      : 3.84 GiB total
  Disk     : 48.21 GiB free / 50.00 GiB total
  ffmpeg   : /usr/local/bin/ffmpeg (or the imageio-ffmpeg bundled binary)
  blender  : NOT FOUND (3D path disabled)
  node     : NOT FOUND (not required)
  port     : 8080 (bind 0.0.0.0)
[ready] listening on 0.0.0.0:8080 — GET / for status, POST /render to queue a job
```

If `ffmpeg` shows NOT FOUND, the 2D renderer cannot encode — add
`imageio[ffmpeg]` (already in `requirements.txt`) and restart; it
brings a bundled ffmpeg binary.

---

## 2. Check it is alive

The server listens on the port Nodaroo assigned to it. For this server
that is **25571** (`au1.nodaroo.fun:25571`). If the panel shows a
different port, use that one instead.

```bash
curl http://au1.nodaroo.fun:25571/
```

Expected (abridged):

```json
{
  "service": "nodaroo-cinematic-render",
  "alive": true,
  "specs": {"cpu_count": 2, "ram_gb": 3.84, "disk_free_gb": 48.21},
  "capabilities": {"ffmpeg": true, "blender": false, "paths": ["2d"]},
  "jobs": {}
}
```

---

## 3. API

### Submit a 2D job

```bash
curl -X POST http://au1.nodaroo.fun:25571/render \
  -H "Content-Type: application/json" \
  -d @example-2d-job.json
# {"job_id": "a1b2c3d4", "status": "queued", "type": "2d"}
```

Job JSON fields:

| Field | Default | Notes |
|---|---|---|
| `type` | `"2d"` | `"2d"` only for JSON jobs |
| `width` / `height` | `1280` / `720` | 160×90 … 3840×2160; **720p is the sane default on a small CPU** |
| `fps` | `24` | 1 … 60 |
| `letterbox` | `false` | cinematic black bars top/bottom |
| `grain` | `7` | film grain strength 0–20 (`0` = off) |
| `fade_frames` | `12` | crossfade length when a scene uses `"transition": "fade"` |
| `output_name` | `render-<id>.mp4` | output file name |
| `scenes` | — | **required**, at least 1; total duration ≤ 120s |

Scene fields:

| Field | Default | Notes |
|---|---|---|
| `headline` | `""` | big bold centred headline |
| `accent_word` | `""` | one word of the headline drawn in pink |
| `subtext` | `""` | smaller grey line under the headline |
| `label` | `""` | small pink caps label, top-left (e.g. `"MESSY VIRGO"`) |
| `bg_color` / `bg_color2` | `#0b0b16` | vertical gradient (top / bottom) |
| `bg_image_b64` | — | base64 JPEG/PNG used as background (darkened for text) |
| `duration` | `3.0` | seconds (max 30 per scene) |
| `effect` | `"push-in"` | `"push-in"` / `"zoom-out"` / `"static"` |
| `transition` | `"cut"` | `"cut"` (hard cut) or `"fade"` (crossfade into this scene) |
| `headline_size` | `0.085` | headline size as a fraction of width |

### Check a job

```bash
curl http://au1.nodaroo.fun:25571/jobs/a1b2c3d4
# {"id": "a1b2c3d4", "type": "2d", "status": "done", "progress": 1.0,
#  "output": "the-red-screen-test.mp4", "log": [...]}

curl http://au1.nodaroo.fun:25571/jobs
# list all jobs with status + progress
```

### Download the finished mp4

```bash
curl -O http://au1.nodaroo.fun:25571/download/a1b2c3d4
```

### Submit a Blender job (3D, optional)

Requires a `blender` binary on the server. Upload the `.blend` file as
multipart form data:

```bash
curl -X POST http://au1.nodaroo.fun:25571/render \
  -F "blend=@myscene.blend" \
  -F "frame_start=1" -F "frame_end=48" \
  -F "fps=24" -F "resolution_x=1280" -F "resolution_y=720" \
  -F "engine=BLENDER_EEVEE_NEXT" \
  -F "output_name=myscene.mp4"
```

`engine` is `BLENDER_EEVEE` (fast, default) or `CYCLES` (slow —
avoid on a small CPU-only server). The service normalises the EEVEE
name for the installed Blender version (`BLENDER_EEVEE_NEXT` is
accepted too and mapped automatically). Max 600 frames per job. The
service never downloads or installs Blender by itself.

---

## 4. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `can't open file '/home/container/app.py'` then offline | `app.py` was never uploaded. Upload `app.py` + `requirements.txt` via **File access**, then Start. |
| *"another power action is currently being processed for this server"* | You clicked Start/Stop/Restart twice. **Click once and wait.** The panel queues one power action at a time; a second click is rejected until the first finishes. |
| Console shows the installer / "Running Installer" | The egg is (re)installing. Wait a few minutes, then Start. |
| `ffmpeg: NOT FOUND` in diagnostics | System ffmpeg is missing and imageio-ffmpeg did not install. Check `requirements.txt` was uploaded before Start (the egg only installs it at startup). Restart the server. |
| `Blender is NOT installed on this server` on a 3D job | Expected — the 3D path needs a `blender` binary. Use the 2D path, or install Blender on the server first. |
| Blender EEVEE fails with `Couldn't open libEGL.so.1` | EEVEE needs GPU/EGL libraries, which a bare CPU-only server may not have. Use `engine=CYCLES` instead (slower, but pure CPU) — or install the EGL libraries on the server first. |
| Job `failed` with `total duration ... exceeds the 120s limit` | Split the video into shorter jobs (≤ 120s each) and join them with ffmpeg afterwards. The limit protects a small CPU-only server. |
| `GET /` does not answer / connection refused | The service is not running (check the console for `[ready]`) or you are using the wrong port — use the port shown in the panel (here `25571`). |
| Server keeps dying right after Start | Read the terminal: the last lines before `Server marked as offline...` name the missing file or the bad setting. `app.py` missing is the most common cause. |

---

## 5. Files

| File | What it is |
|---|---|
| `app.py` | the render service (Flask + worker + 2D/Blender renderers) |
| `requirements.txt` | Flask, Pillow, numpy, imageio[ffmpeg] (Python 3.12) |
| `example-2d-job.json` | 3-scene test job ("THE RED SCREEN" vibe) — submit as-is |
| `README.md` | this file |

Jobs are processed **one at a time** in the background. Outputs live in
`jobs/<id>/` on the server (download them via `/download/<id>` — the
panel's 50 GiB disk fills up if old outputs are never deleted).

---

## 6. GitHub — SECONDARY path (updates only, after the server runs)

The code also lives at **https://github.com/oxizbinance-aox/nodaroo-render**.

Do **not** use this for the first install — the terminal gives no
response while the server is offline (see section 1). Use it for
*updates* once the server is already running:

```bash
# inside /home/container, in the Pterodactyl console (server RUNNING):
git clone https://github.com/oxizbinance-aox/nodaroo-render.git .
```

Or, to pull the latest code into an existing install:

```bash
cd /home/container && git pull
```

For automatic updates on every restart, set these **Startup variables**
in the panel (server Settings → Startup):

| Variable | Value | Why |
|---|---|---|
| `PY_FILE` | `app.py` | the file the egg runs (default is already `app.py`) |
| `REQUIREMENTS_FILE` | `requirements.txt` | auto-installed by the egg at startup |
| `AUTO_UPDATE` | `1` | the egg runs `git pull` on restart when a `.git` dir exists |

With `AUTO_UPDATE=1`, restarting the server after a `git push` pulls
the newest code automatically.
