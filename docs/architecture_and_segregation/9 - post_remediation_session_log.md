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

## 7e. The belt was restarting itself after dismissing an FM detection

Symptom, reported directly against real legacy behavior: pressing the
FM-review header's Submit button should return to the normal Start/Stop
screen with the belt still stopped — the operator has to press Start
themselves to actually resume motion. The port was instead restarting the
belt automatically the moment Submit was pressed.

Traced both halves in legacy:

- **`submit_all_fo_new`** (`main.py:1310-1348`, the header Submit button) only
  clears the pending detection, unlocks `machine_start_locked`, and clears
  `capture_paused` (so the live view returns). Every line that would actually
  restart the conveyor/camera threads or send `machine_start` is commented
  out in the source — it genuinely never resumes the belt.
- **`start_process`** (`main.py:751-839`, the sidebar Start button) is the
  only thing that ever sends `machine_start` to resume motion. It is guarded
  by `self.start_time_flag`: the first Start press for a batch resets
  `conveyor_stop_count` and creates the output folders; every later press for
  the *same* batch — e.g. resuming after Submit — only accumulates stop time
  (`add_time_to_conveyor_stop_count`) and re-sends `machine_start`. It does
  not touch the tracker, `existing_track_ids`, or any already-created folder.

The port had both wrong: `scan_session.resume()` (bound to the header's
Submit) called `conveyor_service.send("machine_start")` directly, and
`scan_session.start()` (bound to the sidebar Start button) unconditionally
ran `self.reset()` on every call — which would have wiped the batch's
accumulated FM counts and tracker state even on a plain resume, not just
skipped starting the belt at the wrong time.

Fixed both: `resume()` no longer sends `machine_start` at all — only
`start()` does, and only actually moves the belt if the operator presses it.
`start()` now checks `self.active` first (this port's equivalent of
`start_time_flag`): if a batch is already in progress, it just accumulates
stop time and resumes capture, exactly like legacy's guarded branch; the full
reset/setup only runs for a genuinely new batch.

**This immediately surfaced a second, deeper bug**, caught on real hardware:
leaving a batch via the in-app Back button and starting a new one continued
the *previous* batch instead (same FM count carried over). `self.active` was
the wrong signal for "is this a genuinely new batch" — legacy's real reset
point is not "was a scan already active," it's **the operator leaving the
batch-details form for the live-scan page**, `on_next_click` (`main.py:658-
661`), which unconditionally clears `start_time_flag` regardless of whether
the previous batch was ever formally finished — legacy's own Back button
(`back_to_batch`, `main.py:934-935`) doesn't cancel anything either, same as
this port's. Added `POST /api/scan/reset` (`scan_session.reset()` under the
session lock), called once when `Dashboard.jsx` mounts, before Start can be
pressed — the direct equivalent of `on_next_click`'s reset, independent of
whatever `self.active` happened to be left at from an abandoned prior batch.

**Two more issues surfaced while testing the fix above:**

- **The browser Back button needed two presses to actually leave, and could
  trigger the browser's own native unload dialog.** This project runs
  `React.StrictMode`, which double-invokes effects in development. The
  pushState-based Back-button guard (`Dashboard.jsx`) had no cleanup to undo
  its `pushState` call, so every mount pushed *two* dummy history entries
  instead of one — a single Back press only popped one of them (looked like
  nothing happened), and enough phantom entries stacking up across repeated
  navigation could plausibly overshoot the SPA's own history into a genuine
  page unload, which is the only thing that can trigger that native dialog.
  Fixed with a ref guard so the push only actually happens once per real
  mount, regardless of StrictMode's double-invoke.
- **The in-app Back button on the live-scan page was never disabled.** Legacy
  has this same button (`pushButton_back_live`) on this page too, and
  disables (not hides) it once scanning starts (`start_process`,
  `main.py:757`), re-enabling only once Submit Batch computes results
  (`submit_create_result`, `main.py:1834`). First ported as a matching
  `backDisabled` state tied to Start/Submit, then on request made simpler and
  stricter than legacy twice over: not just disabled but removed from the
  page entirely for the whole time it shows an in-progress batch — Cancel
  Batch is the only way to leave from here. A deliberate deviation from
  legacy's exact show/enable lifecycle, not a bug fix — see
  `enhancements.md`.
- **Start AND Stop were both disabled after Submit-in-header, when Start
  specifically should have been clickable.** A direct side-effect of the
  `resume()` fix above: `handleResume` (`Dashboard.jsx`) still optimistically
  set `isScanning(true)`, left over from when Submit-in-header used to
  auto-restart the belt. Since Start is disabled by `isScanning || locked`,
  `isScanning` stuck at `true` kept Start disabled even though the belt was
  genuinely stopped and waiting on the operator. Changed to `setIsScanning
  (false)`, matching that the belt does not move again until Start is
  actually pressed.

## 7b. The biggest miss so far: legacy pauses the camera, this port didn't

Symptom, seen on real hardware: with the belt stopped and interlocked, the FM
count kept climbing on its own, and the review screen showed red boxes
floating over an apparently empty belt. Both turned out to be the same root
cause, and it is the largest single behavioral gap found in the port to date.

**Legacy stops grabbing frames entirely while a detection is under review.**
`cam_thread.capture_paused` (`GrabImage.py:82`) is checked at the top of the
capture loop (`GrabImage.py:95`): when set, the loop sleeps instead of
grabbing, so nothing is queued, **no inference runs at all**, and the
displayed pixmap simply stays as it was. It is set via
`stop_camera_with_delay` (`main.py:726-744`), which waits one second for the
conveyor to decelerate first. The full lifecycle:

| Event | Legacy | Ported to |
|---|---|---|
| START | `capture_paused = False` (`main.py:812/844`) | `scan_session.start` |
| FM detected | paused after 1s (`update_fm_image`, `main.py:983`) | `_on_foreign_matter` |
| Manual STOP | paused after 1s (`stop_p`, `main.py:1085`) | `stop_belt_manually` |
| Forward jog | unpaused (`main.py:868`), re-paused after re-lock (`main.py:888`) | `POST /api/scan/forward` |
| Detection resolved | `capture_paused = False` (`main.py:1324`) | `scan_session.resume` |

The port had none of this: its WebSocket loop grabbed and inferred
continuously, so (a) a stationary object under a stopped belt kept minting
fresh tracker ids from ordinary frame-to-frame box jitter — inflating
`total_fo_detected` with the belt physically still — and (b) the SVG box
overlay, computed from the detection frame, was drawn over whatever *newer*
live frame had since arrived, so the boxes lined up with nothing.

Ported as `scan_session.pause_capture()` / `resume_capture()` plus a check at
the top of the stream loop in `app/api/camera.py`. While paused the loop
grabs nothing, runs no inference, never calls `process_frame`, reports
`fps: 0`, and sends the **frozen detection frame** (`pending_frame`, the exact
frame the boxes were computed on) once, then state-only messages — so the
overlay aligns with what's on screen, exactly as legacy's burnt-in boxes did.
`pause_capture` also carries one small addition legacy lacks: a token check,
so a pause scheduled a second earlier cannot land *after* the operator has
already resolved the detection and freeze a scan that was just released.

**Two earlier attempts at this were reverted as part of the fix**, both having
targeted the symptom rather than the cause:
1. Not adding `has_similar_x_axis`-suppressed ids to `existing_track_ids`.
   Reverted: legacy counts them (`main.py:2618-2622`), and
   `total_fo_detected` goes into the Qualix datagram, so it has to stay
   comparable with what legacy reports for the same material. The check only
   governs whether the operator is asked to classify something, not whether
   it was a real object.
2. Refusing new ids whenever `machine_start_locked` was set. Reverted because
   it was actively wrong: legacy re-locks `machine_start` *on purpose* during
   the Forward jog (`main.py:886`) while capture stays live for that window —
   which is precisely when the nudged-forward material must be detected — so
   a lock-based gate silently breaks the Forward button.

## 7c. Ported legacy's periodic resource monitor (was completely missing)

Found by comparing a real legacy startup log line by line against the new
backend's: legacy logs `"Resource monitor started (interval: 300s)"` at boot
(`logger.py`'s `ResourceMonitor`, a `QThread`/`threading.Thread` that calls
`log_system_resources()` every 300s) — process memory/CPU, system-wide
memory/swap, and disk usage, all via `psutil`, escalating to a `WARNING` log
line above 85% system memory. Nothing in `eye_compass_be` did anything like
this at all.

Ported as `app/services/resource_monitor.py` — `get_system_resources()` /
`log_system_resources()` are a direct line-for-line port of legacy's, with the
same fields and the same 85%-memory warning threshold. `resource_monitor_worker()`
replaces legacy's `QThread`/`threading.Thread` with an `asyncio` loop, matching
`sync_worker.py`'s style, and is started from the app lifespan
(`RESOURCE_MONITOR_ENABLED`, default on; `RESOURCE_MONITOR_INTERVAL_SECONDS`,
default 300 to match legacy). `psutil` itself was also missing from this
environment entirely — added to `requirements.txt` and installed into the
deployment venv. Unlike legacy, which still starts the (useless) thread and
loops forever doing nothing if `psutil` is missing, the port logs one warning
and simply does not start the loop — no functional difference an operator
would ever notice, since legacy's version produces no output either way.

## 7d. Config.INI audit: run_env dead, device_id priority inverted, Sheet made env-aware

A full audit of every `config.INI` key against both codebases (prompted by
noticing the live device's `config.INI` actually has `run_env = prod`, not
`dev` as an earlier doc entry assumed) found two real bugs and one deliberate
new capability:

1. **`QUALIX_RUN_ENV` was read but never consulted.** `QUALIX_API_URL`'s ini
   fallback (`config.py`) was hardcoded to always look up `API_ENV.prod`
   regardless of what `run_env` said — so switching `run_env` to `dev`/`qa` in
   `config.INI` had no effect on which Qualix host was actually used. Fixed:
   the ini lookup now uses `key=QUALIX_RUN_ENV` instead of the literal
   `"prod"`, matching legacy's own `API_ENV[self.env]` (`api_handle.py:52-56`).
2. **`device_id` priority was inverted from legacy's real behavior.** Traced
   legacy's actual `get_cpu_id()` (found in an untouched reference copy,
   `eye_compass_new/eye_compass/sheet_update.py` — the preserved
   `eye_compass_legacy` copy is missing this function entirely, a legacy-side
   anomaly independent of this port) — it is purely `/etc/machine-id` ->
   `/var/lib/dbus/machine-id`, with **no config fallback at all**. This port's
   `get_device_id()` (`app/services/datagram.py`) tried `settings.DEVICE_ID`
   (the ini value, `VAR10223043`) first. Fixed: machine-id files are now tried
   first, `settings.DEVICE_ID` only as a last resort if neither file exists.
   Verified the fix resolves to the real machine ID
   (`5dbfb12414a3456d9014d88183e338b1`), matching what actually appeared in
   the datagram in a real legacy log shown during this session.
3. **The Google Sheet was made environment-aware — a deliberate deviation,
   not a fix.** See `enhancements.md`: legacy always writes to one hardcoded
   sheet no matter what `run_env` is; `SHEETS_SPREADSHEET_ID_<ENV>` now
   overrides `SHEETS_SPREADSHEET_ID` per environment, so `dev`/`qa` testing
   doesn't land in the real prod sheet.

The same audit also confirmed a long list of keys are either genuinely
ported and working (region/bucket/pool_id, the OAuth/config/analysis URIs,
`camera_index`, `_PATH_.parent`, Google Sheets' `enabled` flag) or correctly
*not* ported because they're provably dead in legacy itself (`variety`,
`location`, `_PATH_.cwd`, `S3.type`, `history_dashboard`/`history_by_mobile`,
all five `CAMERA.runtime_*` keys, `camera_serial`, `feature_load_enabled`,
`s3_base_path` as a read path) — see that finding for the full per-key
breakdown if it's needed again.

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

## 7f. The camera view was cropping detected boxes out of sight

Reported live: the "FM Detected" count (already fixed correctly, §7a) didn't
match the number of highlighted boxes actually visible on screen — e.g. 7
detected, only 1 or 2 boxes visible. Not a counting bug: `Dashboard.css`'s
`.camera-feed-img` used `object-fit: cover`, which scales the frame
uniformly and crops off whatever doesn't fit the container's aspect ratio;
the SVG box overlay used a matching `preserveAspectRatio="xMidYMid slice"` so
boxes stayed pixel-aligned with what was visible — but any box whose source
coordinates fell entirely in the cropped-off margin was never drawn at all,
correctly counted server-side but invisible to the operator.

Checked legacy's actual display code (`main.py:1128-1152`,
`image_update_slot`): `ui.label_live_image_viewer.setScaledContents(True)`
plus `cv2.resize(image, (label_width, label_height))` — the whole frame is
stretched to the label's exact pixel dimensions with **no aspect-ratio
preservation at all**. Nothing is ever cropped out of view in legacy; the
tradeoff is a stretched/distorted image if the label's aspect ratio doesn't
match the camera's, not missing content. Fixed by switching
`.camera-feed-img` to `object-fit: fill` (non-uniform stretch, matching
legacy's own behavior including the distortion) and the SVG overlay to
`preserveAspectRatio="none"` to match. A legacy-matching correctness fix, not
a deviation — the previous crop-instead-of-stretch choice had no legacy basis
and was actively hiding real detections from the operator.

A second, unrelated cause of the same symptom turned up later: two `pending`
entries can have byte-identical coordinates (confirmed live via a coordinate
log added to `_on_foreign_matter`), so two overlapping boxes render as one
visible rectangle while the count still includes both. Traced to
`inference_service.py:270-278`, which imports and runs `run_inference.py`
directly from the shared, unmodified legacy source tree — its NMS
(`run_inference.py:658`, `torchvision.ops.nms`, `agnostic=False`) only
suppresses overlapping boxes of the *same* class, so the same physical object
classified as two different FM types produces two undeduplicated detections.
Legacy's own `fm_control`/`bounding_boxes` (`main.py:2604-2650`) takes the raw
`coo` list the same way, no coordinate-based dedup either — so this appears
to be a genuine characteristic of the shared detection model/NMS, not a
porting bug, though not yet confirmed against real legacy hardware for this
exact instance. Left as-is pending that confirmation, rather than adding
dedup logic legacy itself doesn't have.

## 7g. The results-review screen was missing legacy's Cancel button and showing extra header stats

Reported live, screenshot-compared directly against legacy's own results-
review screen (main.py's stackedWidget index 4, reached from
submit_create_result, main.py:1689). Two things didn't match:

- Legacy's header shows only Batch Id/Commodity/Variety. This port's
  `ResultsViewer.jsx` also showed a "Total FO" stat, a "Not saved yet" pill,
  and XAI View/History buttons — none of which exist on this legacy screen.
  (Those elements now condition on `!isPending`, since they're meaningful for
  viewing an already-saved record from History — a screen this port
  deliberately consolidated with this one — but not for the fresh-from-Submit
  case this screen equals in legacy.)
- Legacy has **two** buttons here: `pushButton_save_res` → `save_result`
  (`main.py:1829-1843`, persists + starts the Qualix/Sheets sync thread —
  already matched by this port's `confirmScan`) and `pushButton_cancel_res`
  → `cancel_result` (`main.py:2064-2081`, archives the batch's crops to
  `rejected/` and abandons it — the same function Cancel Batch uses
  elsewhere). This port only ever had the Save-equivalent button
  ("Confirm & Finish", renamed to "Save" to match legacy's own label for this
  case); Cancel was missing entirely, with no way to discard a computed-but-
  unsaved result from this screen.

Fixed: added `POST /api/scan/discard` (`app/api/scan.py`), reusing
`scan_session.cancel()` — the same archive-to-`rejected/` logic Cancel Batch
already uses — plus clearing the in-memory `_pending_submission` so a stale
Save can't resurrect a discarded result. `ResultsViewer.jsx` now shows a
"Cancel" button next to Save when `isPending`, calling the new endpoint. A
legacy-matching correctness fix, not a deviation.

A third mismatch found the same way: this port also showed a large camera-
frame panel on the left of the breakdown table. `submit_create_result`
(`main.py:1618-1661`) never sets any image widget on this screen — it only
calls `populate_result_table(data)` and switches pages — so legacy shows no
frame here at all. The `isPending` case now omits `.image-viewer` entirely.
The frame area is kept for the saved/History-detail case (`!isPending`),
where the XAI View button still exists and needs somewhere to render the
heatmap toggle.

A fourth and fifth mismatch, same comparison: legacy has no "Analysis"
heading above the table at all (removed, both cases — it was a port
addition, not tied to any legacy widget either way), and Save/Cancel live in
their own side panel next to the table (`pushButton_save_res`/
`pushButton_cancel_res`, `main.py:478/496`), not up in the header bar. Moved
both buttons out of `header-actions` into a new `.results-actions-sidebar`
(`isPending` only — same idea as Dashboard's Start/Stop sidebar, stacked
vertically) that fills the grid's second column now that `.image-viewer` is
gone for that case — so the earlier `.results-breakdown-full` full-span
override is no longer needed and was removed; the existing 2-column grid
(`minmax(0,2fr) minmax(240px,1fr)`) places `breakdown|sidebar` correctly on
its own, the same way it already placed `image-viewer|breakdown` for
`!isPending`.

A visual pass followed, screenshot-compared directly against real legacy:
the header became a plain 3-column bar (Batch Id value NOT bold / Commodity
bold centered / Variety bold right — a separate `results-header-legacy`
layout, since almost nothing is shared with the saved/History-detail header
once XAI/History/Total FO are stripped), the table got legacy's grey-blue
QTableView look (`results-breakdown-legacy`: grey background filling the
full column height including the empty space below the last populated row,
visible grid lines, centered column headers) instead of a white bordered
card, and Save became a light-blue/thin-blue-border native-style button
(`btn-save-legacy`) instead of the green pill used for primary actions
elsewhere in this port. Approximated, not pixel-matched — real Qt widget
chrome (native scrollbars, focus-rect artifacts) isn't meaningfully
reproducible in CSS; the recognizable grey-table/native-button look is what
was targeted. All `isPending`-only, via the same conditional-class pattern
as the rest of §7g.

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

## 7h. History's header used a stale, unstyled layout component

Reported live: the History page's top bar looked nothing like every other
page's header (Home, New Batch, Login, the results-review screens) — small
default-styled "Eye Compass"/Logout buttons crammed in the top-left corner
instead of the shared "← Back | Compass Eye | Logout" bar, and a "Scan
History" heading that was barely visible.

Root cause: History.jsx was the only page still using `layouts/MainLayout.jsx`
(every other page implements its own header inline instead). MainLayout's
JSX used classes `main-layout`/`layout-bar`, but `MainLayout.css` only ever
defined unrelated `layout-container`/`layout-header` rules — apparently
stale from an earlier refactor, with no CSS for the classes actually
rendered. So its header fell back to bare global `.btn-nav`/`.btn-logout`
styling with no bar, no centering, no background. Separately,
`.history-wrapper h2` was `color: #fff`, seemingly written for a dark page
background MainLayout never actually provided (the real page background is
`#f0f0f0`, from `variables.css`'s `--bg-primary`) — white-on-light-grey,
functionally invisible.

Fixed: `History.jsx` now implements the same `app-header` (Back/title/Logout)
pattern as Home.jsx/NewBatch.jsx/Login.jsx directly, matching their exact
grid layout and colors (`History.css`). `.history-wrapper h2` recolored to
`#1a202c`, matching every other page's heading color. `layouts/MainLayout.jsx`
and its CSS were deleted — nothing else referenced them, and they were
actively broken.

## 7i. The saved/History-detail breakdown table was wrongly excluding legacy's own rows

`app/api/history.py`'s `get_result_detail` filtered its `breakdown` array
down to just the FM/NON-FM/Blower FO/Magnetic FO item counts, on the
assumption (stated in its own docstring) that legacy's results table never
shows the `looker_data` rollups (Frame Count/FM Stop Count/Manual Stop
Count/FM Stop Time/Manual Stop Time/Total Stop Time) or `total_fo_detected`.

That assumption was only checked against the FRESH-submit results screen
(`populate_result_table` called with just `create_results()`'s output,
main.py:1689/2141-2152) — never against the separate History-detail screen
(`set_history_options_assessment`, main.py:2181-2205), which is built from
the saved record's full `analysis` array instead (result + looker_data +
total_fo_detected, unfiltered). A real prod device screenshot confirmed the
History-detail table genuinely shows all of it — Frame Count, FM Stop Count,
Manual Stop Count, FM Stop Time, Manual Stop Time, Total Stop Time, and
`total_fo_detected` all appear as their own rows there.

Fixed: `get_result_detail`'s `breakdown` no longer filters anything out — it
returns every entry in the stored `analysis` array as-is, same order legacy
builds it in (result items, then looker_data, then total_fo_detected last).
The now-unused `LOOKER_DATA_KEYS`/`DISPLAY_EXCLUDE_KEYS` constants were
removed from `app/services/datagram.py` along with the dead import. The
FRESH-submit screen (`isPending` in `ResultsViewer.jsx`) is unaffected — it
never reads `detail.breakdown` at all, only `pendingResult.result`, which
correctly stays just the item counts.

## 7j. XAI View was drawing nothing, then the wrong colors, then crashing the process at shutdown

Reported live: clicking "XAI View" during an active FM detection showed only
a background-color change — no red detection boxes, no confidence labels,
nothing resembling legacy's heatmap. Four separate bugs, found and fixed one
at a time as each was uncovered:

1. **No boxes drawn at all.** `Dashboard.jsx`'s `handleXaiToggle` sent
   `detections: pending.map((p) => p.box)` — bare `[x1, y1, x2, y2]` arrays.
   `xai_service.py`'s `build_confidence_heatmap` requires at least 6 values
   per detection (`det[4]` = confidence, `det[5]` = class_id) or it silently
   skips that detection (`if len(det) < 6: continue`) — so every detection
   was always skipped, the mask stayed all-zero, and the function returned
   the frame completely unmodified.

2. **Wrong background color.** `app/api/xai.py` applied
   `cv2.cvtColor(image, cv2.COLOR_RGB2BGR)` on the frame decoded from the
   client's `frame_base64`, on the assumption (copied from legacy's
   `xai_optimized.py:51-53`, which applies to a different input — a raw
   in-memory frame straight off the camera, never JPEG-encoded) that the
   input was RGB. But `cv2.imdecode` always yields correct BGR for a
   standard JPEG regardless of the camera's original colorspace, and
   `frame_base64` here is the very same JPEG already displayed correctly in
   the live view — so this swap re-flipped already-correct colors. Confirmed
   live: a genuinely blue conveyor belt rendered brown/orange in XAI View
   only. Removed the extra conversion.

3. **Investigating fix 1 further: after sending real
   confidence/class_id, detections still came back empty when re-inferred.**
   Root cause: legacy's `show_xai_image` (main.py:1015-1055) re-runs
   inference on `image` — the same in-memory frame object frozen at
   `fm_control`'s lock time (main.py:2661), at its original resolution. This
   port's XAI request instead re-inferred on the client's `frame_base64`,
   which is the WebSocket **preview** copy — JPEG-compressed and possibly
   downscaled to `STREAM_MAX_WIDTH` (`camera.py`'s `encode_display`) for
   bandwidth. Re-running inference on that lossy/downscaled copy found
   nothing, even though the same frame's full-resolution original had just
   found 4 detections live. Fixed: when no `detections` are supplied,
   `generate_heatmap` now prefers `scan_session.pending_frame` (the exact
   full-resolution frame frozen at lock time, `scan_session.py:387`) over
   the client-supplied preview, matching legacy's own frame object exactly.
   `pending_frame` is RGB (same as what the live loop already feeds
   `_inference.predict()` directly, `camera.py:267`), so it's re-run
   unconverted and only turned to BGR afterward for the drawing/colormap
   step. This also meant a related design decision: `Dashboard.jsx` no
   longer sends any `detections` at all for XAI (previously sent the stored
   `pending` boxes) — legacy re-infers fresh rather than reusing the boxes
   that triggered the lock, so this port now does the same.

4. **Process aborts at shutdown: `PyCUDA ERROR: The context stack was not
   empty upon module cleanup... Aborted (core dumped)`.** Every other
   camera/inference call site in `camera.py` is carefully routed through one
   dedicated worker thread (`_hw_executor`/`_run_on_hw_thread`), because
   TensorRT/pycuda pushes a CUDA context onto whichever OS thread first
   calls `predict()` and only pops it via an explicit `cleanup()` call made
   on that SAME thread at shutdown (see the note above `_hw_executor`'s
   definition). `api/xai.py`'s `generate_heatmap`, a plain sync FastAPI
   handler, called `_inference.predict(...)` directly — Starlette runs sync
   handlers on its own internal thread pool, a different (and
   non-deterministic) thread every request, so each XAI call could push a
   context that shutdown's `cleanup()` never sees, and the process
   hard-aborts once one is left dangling. This bug predates this session's
   XAI changes; it was already present in the original endpoint. Fixed:
   `generate_heatmap` now submits inference to `_run_on_hw_thread` (via a
   small `_run_inference` helper) exactly like every other hardware-touching
   call in `camera.py`.

The confidence-score label drawn on each box (`0.59`-style text) and the
JET-colormap-tinted background are both intentional, matching legacy exactly
— see `xai_service.py`'s `build_confidence_heatmap` (same math as
`xai_optimized.py`): a strongly-colored blob at a detection means high
confidence; the red-dominant background is JET's "low activation" end,
correctly recolored across the whole frame, not a residual color bug.

## 7k. `create_results` counted every FM crop twice (wrong totals saved AND synced)

Found from the History → record-view screen: batch `milind4550` showed
`Total FO: 247` beside a gallery captioned `Captured objects (245)`. Both
numbers are derived, and they disagreed by exactly 2.

The stored record claims `FM: 4`; the batch folder holds exactly 2
`FM_*.png` crops. Every other one of the 12 categories matched its file
count exactly.

`create_results` (`scan_session.py`) builds its match list as:

```python
params = list(self.analysis_parameters) + ["FM", "NON-FM"]
```

and then, for each file, increments `counter[key]` for every `key` in
`params` the filename's stem starts with. **Most commodities already carry
`"FM"` in their own analysis vocabulary** — Urad White's is `['Others',
'Blower FO', 'Magnetic FO', 'Husk', 'FM', 'Metal Fragments', ...]` — so the
hardcoded `+ ["FM", ...]` puts `"FM"` in the list **twice**, and with no
`break` in the inner loop each FM crop is counted once per occurrence.
2 crops × 2 = the stored `FM: 4`, and since `total_fo_detected` is summed
straight from these values, 245 real crops became the stored 247. Replaying
the exact pre-fix logic over that folder reproduces `FM: 4`/`total: 247`
deterministically; the fixed version yields `FM: 2`/`total: 245`.

**This is a porting bug, not legacy behavior.** Legacy does not iterate a
list — it builds a dict first (`main.py:1443`):

```python
filename_mapping = {param: param.replace(" ", "_") for param in analysis_parameters}
...
for key, mapped_value in filename_mapping.items():
    if file_name.startswith(key):
        counter[key] += 1
```

A dict comprehension silently collapses the duplicate `"FM"` to one key, so
legacy matches it once. The port swapped that dict for a plain list and
reintroduced the duplicate. Fixed by deduplicating while preserving order
(`dict.fromkeys(...)`), which restores legacy's own semantics exactly rather
than changing them. The missing `break` is deliberately left as-is: legacy
has no break either, so a genuine prefix-collision pair would double-count
there too — no commodity's vocabulary currently contains such a pair.

**Scope of the damage.** This affected the saved `result`/`total_fo_detected`
for every batch that captured at least one crop labelled with the generic
`FM` type, on any commodity whose vocabulary also lists `FM` — and those
inflated numbers went into the Qualix datagram, so they were *synced*, not
just displayed. Already-stored rows are not retroactively corrected by this
fix; they keep the inflated values until someone decides whether to
recompute and re-deliver them.

## 7l. Every saved crop and raw frame had red and blue transposed

Reported from the History → record-view gallery: the captured-object crops
looked like XAI heatmaps rather than photographs — a red/brown field with
blue blobs, which reads exactly like a JET colormap. Nothing from the XAI
path was actually leaking into the gallery; the crops were simply being
written with their red and blue channels swapped. The belt is a blue
food-grade belt and the objects are cream/tan, so transposing R and B turns
the belt brown and the grains blue, which is what made it look like a
heatmap.

Both codebases debayer identically — `cv2.COLOR_BAYER_RG2RGB`, legacy at
`GrabImage.py:45`, this port at `camera_service.py:230`. Call that array
`B`. Legacy then swaps the channels a **second** time before anything
downstream sees the frame (`GrabImage.py:308`,
`image_rgb = cv2.cvtColor(pic, cv2.COLOR_BGR2RGB)`), and it is that `img`
copy which `emit_results` hands to the crop/save path and which the global
`image` used by the XAI view points at. So:

| | legacy | port (before) | port (after) |
|---|---|---|---|
| array reaching the save path | `S(B)` | `B` | `B` |
| conversion at write time | `BGR2RGB` | `BGR2RGB` | none |
| net | identity → `B` as BGR ✓ | one swap ✗ | identity → `B` as BGR ✓ |

The port has no equivalent of that `GrabImage.py:308` conversion, so copying
legacy's write-time conversion literally (`main.py:1361` for crops,
`main.py:2464` for raw frames) left exactly one uncancelled swap. Dropping
it at the three save sites in `scan_session.py` (`label_detection`,
`save_unselected`, `save_raw_frame`) restores legacy's **net** behavior
rather than changing it — see `_COLOR_ORDER_NOTE` at the top of that file.

Verified three ways: legacy's own `output/` crops show a blue belt with
cream rice grains; R/B-swapping one of this port's crops reproduces exactly
that; and re-running the fixed save path over a recovered frame produces the
blue belt/tan object directly.

The live view was never affected and needed no change — `camera.py`'s
`encode_display` hands the frame straight to `cv2.imencode` with no
conversion, which is already the correct `B`-as-BGR reading. That is also
why the discrepancy went unnoticed: the operator's screen was right while
the files on disk were not.

**The XAI view had the same defect, from the same misconception.**
`xai.py` was converting `pending_frame` with `COLOR_RGB2BGR` before building
the heatmap. Legacy hands its own array to Qt, whose `QImage` reads it as
RGB; this port hands it to `cv2.imencode`, which reads it as BGR — so the
photograph *underneath* the heatmap was swapped, while the JET colormap
itself (produced by `applyColorMap` in OpenCV's own BGR order) came out
correct. That combination is why §7j's earlier review concluded the
red-dominant background was legitimate JET output: the colormap was right,
but the belt beneath it was brown instead of blue and reinforced the
impression. Now blended onto an unconverted `.copy()` — the copy also
matters on its own, since `build_confidence_heatmap` blends into the array
it is given, and passing `pending_frame` itself would burn the heatmap into
the very frame every later crop is cut from.

Already-saved crops are not rewritten by this fix; existing batches keep
their swapped colors on disk and in S3.

## 7m. The captured-object preview modal appeared to "blink" and Preview seemed dead

Reported directly: double-tapping a thumbnail on ReclassifyObjects.jsx made
the enlarged preview flash open and immediately close again, and the
"Preview" button (jump-to-object-number) seemed to do nothing at all.

The preview *was* opening correctly both times — it was being closed again
instantly. The modal's backdrop (`reclassify-preview-overlay`/
`results-preview-overlay`) covers the whole screen the moment it renders
and closes the preview on any click. On this touchscreen, the **second tap**
of a double-tap lands on that just-rendered backdrop and dismisses it before
it can be seen, which reads as a blink. The same mechanism explains the
Preview button: pressed twice in quick succession (as an operator retrying
what looked like a dead button naturally would), the first tap opens the
preview and the second immediately closes it.

Fixed by timestamping when the preview last opened
(`previewOpenedAtRef`/`handlePreviewBackdropClick` in both
ReclassifyObjects.jsx and ResultsViewer.jsx) and ignoring a backdrop click
within 400ms of that — comfortably inside a double-tap's timing, comfortably
below any deliberate "tap to dismiss" gap. The modal's own ✕ close button is
unaffected and still closes unconditionally on any single tap.

## 7n. A "Change to" dropdown opened visually behind the preview modal it lives in

Reported directly, with a screenshot: opening the "Change to" dropdown from
inside the reclassify preview modal showed the option list rendering
*underneath* the modal, so taps on any option landed on the modal instead
and the operator couldn't select anything.

`CustomSelect`'s option panel is portal-rendered onto `document.body`
specifically so it can escape its trigger's own clipping ancestors (see
`CustomSelect.jsx`'s own docstring), but that also means its stacking order
is no longer determined by where it sits in the DOM tree — it needs its own
`z-index` high enough to clear anything else on the page, including a modal
the trigger happens to be inside. It was left at `z-index: 40`
(`CustomSelect.css`) while both pages' preview-modal overlays are
`z-index: 50` — so the panel was opening correctly, just one layer beneath
the modal. Raised to `z-index: 60`, above every modal in the app.
