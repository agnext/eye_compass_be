# 9. Post-Remediation Session Log — 5–7 September 2026

`8 - remediation_log.md` covered a *static* audit — no running frontend,
no physical hardware, no side-by-side comparison against the real production
device. This document covers everything found and fixed in the much longer,
interactive session that followed: running both stacks for real, exercising
real hardware, and comparing screen-by-screen against actual screenshots from
a production device. Treat this as the most current record of the system's
state, ahead of `8 - remediation_log.md`.

## 1. Getting both stacks running locally

- **Docker networking**: `docker compose up` failed outright
  ("Unable to enable DIRECT ACCESS FILTERING") because this Jetson's kernel
  (`5.15.148-tegra`) has no `iptable_raw.ko` module, which Docker's default
  bridge networking needs. Fixed by running both the `db` and `frontend`
  containers with `network_mode: host` instead — see
  `5 - infrastructure_and_deployment.md`.
- **Remote-dev access**: reaching the frontend from a laptop (not the Jetson's
  own screen) needed port forwarding; several methods were tried (SSH `-L`,
  VS Code's Ports panel). A separately-forwarded second port for the backend
  (8000) turned out to **silently swallow POST requests** while GETs worked
  fine through the same kind of tunnel — this made login hang forever with no
  error and no backend-side log line at all. Fixed at the root: made the
  frontend same-origin with the backend via Vite's dev-server proxy
  (`/api`, `/ws` → `localhost:8000`), so only one port ever needs forwarding,
  in dev or production. `Login.jsx`'s error handling was also rewritten in
  the process — it used to show the literal string "Invalid credentials" for
  *any* failure (network error, CORS, unreachable backend), which had been
  actively misleading while diagnosing this exact bug.
- **Git housekeeping**: both repos had diverged from their remotes; each
  divergence was investigated (not blindly merged) before force-pushing with
  `--force-with-lease`, with explicit confirmation at each step. Commit
  author identity was corrected to the user's own account, scoped locally to
  just these two repos (not global git config).

## 2. Real hardware confirmed present and working

- A genuine Hikvision GigE camera (`MV-CS023-10GC`, at `169.254.143.87`) is
  physically attached to this dev unit. All of `8 - remediation_log.md` §5's
  camera-acquisition fixes (packet size negotiation, calibration file, no
  180° flip, exposure/gain) were confirmed working against it for real.
- The conveyor's serial port (`/dev/ttyTHS1`) sometimes acknowledges commands
  correctly and sometimes doesn't answer at all, depending on whether a real
  belt-controller adapter happens to be connected at that moment — not a
  software defect. Confirmed by checking `fuser`/`lsof` show nothing else
  holding the port, ruling out a resource conflict as the cause.
- A stray `cutecom` process (a GUI serial monitor left open from a previous
  session) was found holding the port at one point — closing it was
  confirmed safe (a passive monitoring tool, no data sent on close).

## 3. UI screens matched against real production screenshots

Each of these was found by comparing the running app directly against a
screenshot from the actual production device, not by re-reading the `.ui`
file in isolation:

- **Login** — legacy's actual branding is "Compass Eye" (confirmed against
  `label_6`/`label_7`/`label_32` in `eye_compass_ui.py`; the window title
  "Eye-Compass" is different and not what's shown on-screen). The two
  top-right icon placeholders turned out to be genuinely dead UI in legacy —
  defined and styled, but with no `.clicked.connect(...)` anywhere in the
  codebase — confirmed and then removed from our version too, rather than
  wiring up functionality legacy itself never had.
- **Home** — branded header (title centered via a 3-column grid, not a
  2-item flex row, so it stays centered regardless of what's on either side).
- **New Batch** — field labels corrected ("Commodity"/"Variety" → "Product
  Name"/"Product Code", matching that this page's fields are
  `comboBox_product_name`/`comboBox_product_code`, a *different* pair of
  legacy widgets from the Data Collection page's `comboBox_commodity`/
  `comboBox_variety`), field grouping/order matched to the real screenshot's
  3-column layout, a genuine bug fixed (the Start Batch button's CSS class
  didn't match any defined rule at all, so it rendered completely unstyled),
  input backgrounds corrected from a stale grey to white, Manufacturing Date
  defaulted to today (matching legacy) alongside Receiving Date.
- **Data Collection** — this entire screen and its backend support **did not
  exist at all** before this session, despite legacy having one
  (`goto_dc_page`/`start_dc`/`stop_dc`/`forward_dc`/`capture_image_dc`/
  `submit_dc`, `main.py:1843-2022`). Built from scratch: a dedicated raw
  frame-dump WebSocket (no inference, matching that legacy's version is a
  plain video-to-disk recorder), Start/Stop/Forward/Capture Image/Submit
  wired to the exact legacy sequences, including a subtle one — legacy's
  `start_dc`/`forward_dc` never check whether the conveyor actually
  acknowledged `machine_start`, so recording proceeds regardless of a failed
  belt ack; the first version built here incorrectly gated on it, which
  silently broke recording on real hardware given how unreliable the belt
  ack already is on this dev unit (see §2). Fixed to match legacy's
  don't-gate-on-it behavior exactly.
- **The live-scan page (`Dashboard.jsx`)** — the biggest set of corrections:
  - Legacy's actual live-scan header (`gridLayout_29`, `eye_compass_ui.py:
    2434-2573`) has *only* Batch Id / FM Detected / XAI / Forward / Submit —
    no Start/Stop, no Blower/Magnetic FO, no sidebar at all. That page only
    appears once a foreign-matter detection has stopped the belt; the normal
    scanning view (Start/Stop, Blower/Magnetic FO sidebar, commodity/variety)
    is a *different* legacy page. `Dashboard.jsx` now switches between these
    two header/sidebar layouts based on the same interlock-locked state that
    already drove the detection overlay, rather than showing one
    permanently-merged layout.
  - The Forward button had been wired to the same simple "start-wait-stop"
    jog used on the Data Collection page (`forward_dc`), but the live-scan
    page's actual legacy Forward is a different function,
    `move_conveyor_forward` (`main.py:851-883`): unlock the interlock, jog for
    only 0.1s (not 2s), then immediately send `FM_detected` (which stops the
    belt again) and re-lock. A dedicated `POST /api/scan/forward` endpoint
    was added to reproduce this exact sequence.
  - **XAI View** was wired up for the first time — the backend endpoint
    (`/api/xai/generate`) and RTK Query hook already existed but nothing on
    this page ever called them.
  - The foreign-matter bounding-box overlay and the camera feed disagreed
    once the feed was changed to fill its container (`object-fit: cover`,
    fixing a bug where the video rendered smaller than its box, leaving
    background showing around it): the SVG overlay used a non-uniform
    stretch (`preserveAspectRatio="none"`) while the image now uses a
    uniform scale-and-crop. Changed the overlay to
    `preserveAspectRatio="xMidYMid slice"` — the SVG equivalent of
    `object-fit: cover` — so detection boxes land back on the right pixels.
  - **Submit Batch was matched to legacy's actual two-step design.** Legacy's
    Submit (`submit_video` → `submit_create_result`, `main.py:1566-1661`)
    only *computes* the result and shows a review page — it does **not**
    save to the database or sync to Qualix/Sheets. That only happens when the
    operator confirms from the results page
    (`backtohome` → `save_result`, `main.py:1717-1812`). The backend's
    `POST /api/scan/submit` was split accordingly into a compute-only step
    plus a new `POST /api/scan/confirm` that actually persists and syncs,
    and `ResultsViewer.jsx` gained a "Confirm & Finish" step for the
    not-yet-saved case. **This intentionally reproduces a real legacy
    fragility — if the operator never reaches the confirm step, the batch is
    never saved or synced at all** — see `todos.md` for the decision this
    needs from the team.
  - A real accounting bug was found and fixed along the way: our port's
    "Manual Stop Count" metric incremented a counter at scan **start**
    (something legacy never does) instead of as part of Submit's implicit
    stop (which legacy's `stop_p()` always does) — the two happened to
    produce the same number in the common case, but diverged the moment the
    belt failed to ack at start, which this hardware has already been shown
    to do.
  - `cancel_result`'s legacy behavior — moving a cancelled batch's saved
    crops into a `rejected/<commodity>/<variety>/<folder>/` archive rather
    than deleting or abandoning them — was missing from the port; added.
  - Blower FO / Magnetic FO inputs now strip non-digit characters as they're
    typed, rather than only hinting a numeric keyboard.
  - Confirmation prompts were added for every way to leave an active scan:
    the in-page Back button, Cancel Batch (already had one), the browser's
    own Back button (via the standard `pushState`/`popstate` workaround,
    since this app uses a plain `BrowserRouter`, not a data router with
    `useBlocker`), and tab close/reload (via `beforeunload` — note browsers
    do not allow custom text on that specific dialog; that's a
    browser-enforced restriction, not something fixable in code).

## 4. A process-crashing threading bug (backend)

`run_inference.py` (legacy code, imported as-is) registers a CUDA context
per-thread the first time it's used on that thread, and only releases it via
an explicit `cleanup()` call — nothing pops it automatically. The camera
WebSocket's per-frame work and the REST `/api/camera/model` endpoint were
running on different thread pools (FastAPI's request pool vs. asyncio's
default executor), so a context could get pushed on a thread that was never
the one `cleanup()` ran on at shutdown. PyCUDA then found a context still on
some abandoned thread's stack and hard-aborted the whole process
(`Aborted (core dumped)`) — not something that happened predictably on every
run, which made it look unrelated to whatever the user happened to be doing
at the time. Fixed by funneling every camera/inference call — REST and
WebSocket alike — through one dedicated single-worker thread, so the context
is always pushed and popped on the same thread.

## 5. S3 upload paths and OUTPUT_DIR

Investigated "at what other points does it upload to S3" thoroughly — see
`todos.md` for the finding (two real upload paths, one dead/misleading log
line) — and separately found and fixed that the backend's `OUTPUT_DIR` wasn't
set in `.env`, which meant new scans were silently writing into the *legacy*
app's own output folder (`/home/nvidia/eye_compass/output/...`) rather than a
folder of the new backend's own.

## 6. Running the legacy app itself, for direct comparison

To compare screens directly, the legacy app was launched on this device. Two
real obstacles came up and were resolved:
- The documented launch scripts point at a `python3.8` virtualenv
  (`m38`) that no longer exists on this device. Verified that the unified
  backend virtualenv (`/home/nvidia/.virtualenvs/eye_compass`, Python 3.10)
  already has every dependency legacy needs (PyQt5, the MVS SDK bindings,
  cv2, torch, boto3) via `include-system-site-packages`, and used that
  instead.
- The live install's `sheet_update.py` was missing a `get_cpu_id` function
  that `main.py` imports unconditionally, so the app crashed at startup. A
  reference copy of the code still had this function; rather than patching
  the live install in place, the user chose to preserve the original install
  (renamed to `eye_compass_legacy/`) and put the reference copy in its place
  at the path legacy's own code hardcodes (`main.py`'s
  `os.chdir("/home/nvidia/eye_compass")`) — that copy's `config.INI` is a
  dev-environment config (different credentials/location than this device's
  real production identity), which is expected and was flagged before the
  swap.

## 7a. The live "FM Detected" label was showing the wrong count

Found while comparing the live-scan header against real device behavior: the
label showed the batch's cumulative FM total (492 and climbing) instead of
matching legacy. Traced legacy's actual chain:

- `GrabImage.py:621` emits `str(len(coo))` — the box count for **this one
  detection instance**, not a running total.
- `main.py:2667/2675` stores that as `frame_fm_count`.
- `main.py:980`, `ui.label_fm_count.setText(str(frame_fm_count))` — that's
  what the label actually shows. Legacy's cumulative count
  (`total_fo_detected`) is a completely different value, only ever used for
  the saved result/`looker_data`, and is never displayed on this label.

The port had this label bound to the cumulative value (`total_fo_detected`)
instead. Fixed by adding `frame_fm_count` (`len(self.pending)`, the count for
whatever detection is currently frozen on screen) as its own field in
`scan_session.py`'s `_snapshot`, and pointing `Dashboard.jsx`'s "FM Detected"
label at that instead of the cumulative count. This is a legacy-matching
correctness fix, not a deviation — see `1 - strategy.md` — so it isn't in
`enhancements.md`.

## 7. Investigated but not (yet) part of this codebase

- **XAI View is broken in legacy itself** — traced to a missing PyTorch model
  file (`v6_best.pt`), confirmed absent on every local copy of the legacy
  tree and on a real prod device. See `todos.md`.
- **What physically controls the camera blower / camera light / belt blower /
  belt / vibrator / hopper door / sensor** hardware visible on the machine —
  confirmed none of these are individually addressable from software; the
  application only ever sends five abstract commands
  (`machine_start`/`all_stop`/`FM_detected`/`camera_on`/`camera_off`) and the
  machine's own controller decides what physical hardware each one touches.
  A separate Modbus-based VFD was found on the device (a different port,
  different protocol) with read-only monitoring scripts pointed at it, but no
  application anywhere on this device actually controls it.
