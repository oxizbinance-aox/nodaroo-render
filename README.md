# Nodaroo Cinematic Render Service

A small Flask service that renders cinematic 2D/3D videos on a Nodaroo
(Pterodactyl) Python server. Built for AOX's **"Kang Muse"** server.

* **2D cinematic renderer** (default, works with only Python + Pillow +
  ffmpeg; numpy is an optional speed-up): JSON scene spec in, mp4 out.
  Tetsuoai style — dark
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

## Slim install — why `requirements.txt` only has Flask + Pillow

On 23 Nov 2026 the Nodaroo container's `pip install -r requirements.txt`
failed with **`OSError(28, 'No space left on device')`** while downloading
the `imageio-ffmpeg` wheel (68.0 MB — it got to 40.4/68.0 MB, then died).
The server's own 50 GiB quota was fine (only 92 KiB used), so the
failure was in pip's temp/download area, which fits ~40 MB but not a
68 MB wheel. `numpy` (15.7 MB) had the same problem in the earlier
attempt. So `requirements.txt` is now **slim on purpose**:

* **In it:** `flask` + `pillow` only — **7.5 MB total** to download,
  far under the ~40 MB the container's temp area fits.
* **Out of it (optional enhancements):** `numpy` and `imageio[ffmpeg]`.
  The service runs fully without them: with no numpy it uses a
  pure-Pillow 2D renderer (same look — gradient, glow, grain,
  vignette — just slower: ~28s vs ~18s for the 9s example job);
  without imageio it uses the system `ffmpeg` binary.
* **To add them back** (only if the container has room): put them in
  the panel's **`PY_PACKAGES`** field (space separated, Startup page):
  `numpy imageio[ffmpeg]`. The egg installs `PY_PACKAGES` at startup
  before running `app.py`. With numpy installed the service
  automatically uses the faster numpy render path (it prints
  `numpy: yes (fast 2D path)` in the startup diagnostics); with
  imageio installed it can use the bundled ffmpeg binary when the
  system has no ffmpeg.
* **The service never downloads anything at runtime.** `app.py` only
  *uses* imageio-ffmpeg when it is already installed; if neither a
  system ffmpeg nor imageio exists, a 2D job fails with a clear
  "No ffmpeg available" message — but the API itself still starts
  and `GET /` still answers.

The container itself is decent hardware (2 CPU cores, ~7 GB RAM) —
only the pip download size was the problem.

---

## 1. Upload to Nodaroo — PRIMARY install path (works while offline)

> **Important:** the Pterodactyl console only accepts typed input while
> the server is **RUNNING**. When the server is offline (for example
> crashed on the missing `app.py`) the console is just a log viewer —
> `git clone` typed there does nothing. That is a chicken-and-egg:
> the server needs `app.py` to run, and the terminal needs a running
> server to clone it. **So the first install must go through the
> File access tab, which works while the server is offline.**
> GitHub (section 6) is for installing from a repo *and* for updates —
> but only once the clone works (see the Startup variables there).
> If the clone fails, this File access upload is the fallback.

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
   while pip installs Flask/Pillow (slim — only ~7.5 MB to download).

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
  numpy    : no (slim install — pure-Pillow 2D path)
  blender  : NOT FOUND (3D path disabled)
  node     : NOT FOUND (not required)
  port     : 8080 (bind 0.0.0.0)
[ready] listening on 0.0.0.0:8080 — GET / for status, POST /render to queue a job
```

If `ffmpeg` shows NOT FOUND, the 2D renderer cannot encode — add
`imageio[ffmpeg]` via the panel's `PY_PACKAGES` field (see the Slim
install note above) and restart; it brings a bundled ffmpeg binary.
If `numpy` shows `no (slim install — pure-Pillow 2D path)` that is
normal: the 2D renderer still works, just slower. Add `numpy` to
`PY_PACKAGES` for the fast path.

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
| `ffmpeg: NOT FOUND` in diagnostics | System ffmpeg is missing and imageio-ffmpeg is not installed. Add `imageio[ffmpeg]` to the panel's `PY_PACKAGES` field (see the Slim install note) and restart. |
| Job stalls mid-render / status endpoint stops answering while a job runs | On a 2-CPU box, do not hammer `GET /jobs` while a render runs. One test stalled mid-render while `/jobs` was polled every 8s; a clean run with no concurrent polling finished in 29s. Poll at most every 30s, or just wait and check when the job should be done. |
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
| `requirements.txt` | Flask + Pillow only — slim (Python 3.12). numpy and imageio[ffmpeg] are optional enhancements via `PY_PACKAGES` (see Slim install note) |
| `example-2d-job.json` | 3-scene test job ("THE RED SCREEN" vibe) — submit as-is |
| `README.md` | this file |

Jobs are processed **one at a time** in the background. Outputs live in
`jobs/<id>/` on the server (download them via `/download/<id>` — the
panel's 50 GiB disk fills up if old outputs are never deleted).

---

## 6. GitHub — install from the repo (Startup variables must be right)

The code also lives on GitHub:

- **Repo:** https://github.com/oxizbinance-aox/nodaroo-render
- **Clone URL:** https://github.com/oxizbinance-aox/nodaroo-render.git

> **The repo must exist before the clone can work.** It was created
> and pushed on 23 Nov 2026, so the clone works now — but if you
> ever see a "repository not found" error, check the repo link first.

### 6a. Startup variables — set these EXACTLY (from the real Startup page)

The server's Docker image is **Python 3.12**. Its Startup page has
9 variables: `GIT_ADDRESS`, `BRANCH`, `USER_UPLOAD`, `AUTO_UPDATE`,
`PY_FILE`, `PY_PACKAGES`, `USERNAME`, `ACCESS_TOKEN`,
`REQUIREMENTS_FILE`. Two are already correct and must stay as they are:

| Variable | Value | Notes |
|---|---|---|
| `PY_FILE` | `app.py` | already set — do not change |
| `REQUIREMENTS_FILE` | `requirements.txt` | already set — do not change |

Set these three for the GitHub path:

| Variable | Set to | Why |
|---|---|---|
| `GIT_ADDRESS` | `https://github.com/oxizbinance-aox/nodaroo-render` | **bare URL ONLY — do NOT type `git clone` in front of it.** The field takes just the URL; writing `git clone https://…` in this field breaks the clone. |
| `USER_UPLOAD` | **OFF** | When it is ON the installer **skips the git clone** entirely (it assumes you uploaded the files yourself). It must be OFF for the GitHub path. |
| `AUTO_UPDATE` | **ON** | When ON, the startup script runs `git pull` on every restart, so the server always runs the latest pushed code. |

The other variables do not need changing: `BRANCH` (the repo's
default branch is `main`), `PY_PACKAGES` (empty — `requirements.txt`
covers it), `USERNAME` / `ACCESS_TOKEN` (only needed for *private*
repos; this repo is public).

### 6b. Install steps (GitHub path)

1. Open the server → **Settings → Startup** and set the three
   variables above (`GIT_ADDRESS` bare URL, `USER_UPLOAD` OFF,
   `AUTO_UPDATE` ON).
2. Click **Reinstall Server** — this is the step that performs the
   clone (the installer clones `GIT_ADDRESS` into `/home/container/`
   and installs `requirements.txt`).
3. Click **Start — ONCE — and wait** (1–3 minutes on first start).
4. Check the console for `[ready] listening on 0.0.0.0:8080`,
   then open `http://au1.nodaroo.fun:25571/` in a browser.

**If the clone fails** (wrong URL, repo not found, network error),
fall back to section 1: upload `app.py` + `requirements.txt` from
the Drive zip via File access — that path works while the server
is offline.

### 6c. Updates AFTER the server is running (GitHub path)

Once the server is running, the Pterodactyl console accepts input
again, so updates are easy:

```bash
# inside /home/container, while the server is RUNNING:
git pull
```

Or simply **restart the server** — with `AUTO_UPDATE=ON` the egg's
startup script runs `git pull` for you before starting `app.py`.
