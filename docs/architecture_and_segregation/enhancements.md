# Enhancements — Beyond a Straight Legacy Port

The guiding rule for this whole project was "segregation only, behavior
identical to legacy" (see `1 - strategy.md`). A handful of places deliberately
depart from that rule anyway, because matching legacy exactly would have meant
reproducing a genuine bug, a silent failure mode, or a gap the web
architecture doesn't share. This document keeps those separate from ordinary
correctness fixes (which just made the port match legacy — see
`8 - remediation_log.md` / `9 - post_remediation_session_log.md`) and lists
them alongside further enhancements worth considering but not yet done.

## Enhancements already made

### Backend
- **Captured-object crops now have a 30px margin around the detection box,
  not legacy's 10px.** Every saved crop (`scan_session.label_detection`/
  `save_unselected`) is cut directly from the box in `self.pending`, which is
  built by padding the model's raw detection box via `enlarge_bbox` before
  it's ever stored — one enlarge, then a single slice, not crop-then-pad.
  Legacy pads by exactly the same mechanism at the same call site
  (`GrabImage.py:574-616`, `pad=10`), and the port matched that value
  exactly until now. Reported live: crops were too tightly cropped around
  the object to read clearly, especially on this device's touchscreen where
  a technician is judging a small thumbnail. `enlarge_bbox(b, pad=30, ...)`
  in `scan_session.py`'s `process_frame` — a deliberate deviation from
  legacy's value, not a bug fix, and it also widens the tap-to-classify
  overlay box shown live on the frozen frame (both draw from the same
  padded list), which is an intended side effect, not a separate change.
- **History's Re-sync has no legacy equivalent at all.** Legacy has no
  operator-facing way to force a resync — the only thing that ever re-sends
  a `sync_status='0'` record is the fully automatic
  `sync_unsynced_data_Thread` (`main.py:2826-2966`), on its own 15-minute
  timer, with no manual trigger anywhere. `POST /api/history/{id}/resync`
  and its button (now inline in each row's own Sync Status cell, see
  `History.jsx`) are a straightforward manual on-demand call into the same
  underlying sync logic the background worker already uses
  (`sync_service.post_analysis_data`/`post_to_sheets`) — for when the
  operator doesn't want to wait for the next scheduled pass. Labeled "Sync"
  for a `'0'` record (never actually delivered yet — this click would be its
  first real send) and "Re-sync" for a `'2'` (already sent once and
  rejected) — no button at all once a row reaches `'1'` (delivered).
- **The Google Sheet can now vary per environment, unlike legacy.** Legacy
  always writes to one hardcoded spreadsheet regardless of `run_env`
  (`sheet_update.py:7`'s `SPREADSHEET_ID` constant) — so testing against
  `dev`/`qa` would still write real rows into the same sheet used in
  production. `SHEETS_SPREADSHEET_ID` now resolves an optional
  `SHEETS_SPREADSHEET_ID_<ENV>` override first (e.g.
  `SHEETS_SPREADSHEET_ID_DEV`), keyed off `QUALIX_RUN_ENV`, before falling
  back to the single `SHEETS_SPREADSHEET_ID` / `config.INI` value — so an
  untouched device still gets legacy's one-sheet-for-everything behavior, but
  a device or developer that sets a per-environment override can test without
  touching the real sheet. Requested alongside fixing `QUALIX_API_URL`'s
  `run_env` selection, which is a different, unrelated fix — that one made the
  Qualix host actually follow `run_env` the way legacy's real (working)
  behavior always did; the Sheet was never environment-aware in legacy at
  all, so this is a genuine deviation, not a legacy-matching fix. See
  `9 - post_remediation_session_log.md`.
- **The S3 upload sweep now runs continuously, not just once at app boot.**
  Legacy's `s3_upload.py` (`s3Uploading` QThread) is started once at startup
  and never again — `run()` walks the whole `output/` tree once, uploads
  anything missing/stale, and the thread simply ends; a file saved mid-session
  doesn't reach S3 until the app is restarted. Confirmed directly from a real
  startup log: the "All images of ... uploaded Successfully" line appears
  exactly once, right after boot. `S3UploaderTask.start()` in `s3_worker.py`
  runs the same sweep immediately at startup (matching legacy's one-shot
  behavior) but then keeps repeating it every `S3_UPLOAD_INTERVAL_SECONDS`
  for the life of the process, so newly-saved files get uploaded without
  needing a restart.
- **Real HTTP error codes instead of silent failure.** Legacy's conveyor
  commands returned `200 {"success": false}` (or nothing at all — a warning
  only in a log file the operator never sees) for both a blocked interlock and
  a genuine hardware failure. The backend now returns `409` (interlock
  blocked, an expected operating state) and `502` (real failure) with a
  message, so the frontend can show the operator what actually happened
  instead of a generic error or nothing.
- **Data Collection's finish step cleans up unconditionally.** Legacy's
  `submit_dc`/`back_from_dc` just navigate away — if the operator forgot to
  press Stop first, the frame-recording thread and the belt both keep running
  in the background indefinitely. `POST /api/camera/data_collection/finish`
  stops both before the frontend navigates away, closing that leak.
- **The Qualix bearer token is real.** Legacy issued the literal string
  `"dummy_offline_token"` for an offline login, which nothing downstream could
  actually authenticate with. The backend issues a real session token
  (`SessionStore`, 45-day TTL) for both the online and offline login paths.

### Frontend
- **The live-scan page's in-app Back button is not shown at all for the
  whole time a batch is in progress**, stricter than legacy. Legacy has this
  same button (`pushButton_back_live`) and only disables (not hides) it once
  scanning has actually started (`start_process`, `main.py:757`), re-enabling
  it once results are computed (`submit_create_result`, `main.py:1834`) — so
  in legacy it's visible and briefly clickable right after arriving at this
  page, before Start is pressed. Here it's removed from the page entirely
  for as long as a batch is in progress, on request — Cancel Batch is the
  only way to leave. See
  `9 - post_remediation_session_log.md` §7e.
- **An available (currently OFF) option: keep the live-scan sidebar visible
  even once an FM detection locks the belt**, instead of disappearing the way
  legacy's equivalent page does. This was added, and briefly enabled, after a
  real operational problem on the dev unit: the FM interlock was tripping on
  an empty belt (a separate detection-quality issue, tracked on its own), and
  legacy's page-switch design has no way to finish/submit the batch from the
  locked screen at all — the operator would be stuck until the false
  detection was dismissed. On reflection this wasn't actually needed: legacy's
  own header Submit button (`handleResume`) already returns to the normal
  Start/Stop screen from the locked view — confirmed directly against real
  hardware — so the sidebar reappears anyway as soon as the operator dismisses
  the detection normally. `Dashboard.jsx`'s `SHOW_SIDEBAR_DURING_FM_REVIEW`
  flag now defaults to `false`, matching legacy's hide-on-lock behavior
  exactly, but the capability (and its safety reasoning around disabling
  Start/Stop while locked) is kept in code in case a real stuck-operator
  scenario comes up again — set the flag back to `true` if so.
- **A "CONNECTED"/"disconnected"/"error" pill shows the live WebSocket
  status** on the normal scan header. Legacy has nothing like this at all — a
  desktop app talking to hardware over USB/serial doesn't have a "dropped
  connection to a stream" failure mode in the same sense a browser does, so
  there was no equivalent UI to match. Added because a silently-dead
  WebSocket with a frozen last frame looked identical to a healthy paused
  stream otherwise; see `Dashboard.jsx`'s stream-connection handling. Note
  this only reflects the *transport* — the socket being open — not whether
  the camera is actually producing frames (see `capture_paused` in
  `9 - post_remediation_session_log.md` §7b for that distinction). Controlled
  by the `SHOW_CONNECTION_STATUS` flag right above the component; set it to
  `false` to hide the pill entirely.
- **An FPS readout plus a "CONVEYOR LOCKED" badge floats above the belt view**
  on the live-scan page. Legacy has no on-screen stream-stats display at all —
  frame rate only ever appears in internal camera-config log lines
  (`GrabImage.py:734`), never shown to the operator — and "locked" is
  communicated purely by the whole page switching to the FM-review layout,
  not a separate badge. Controlled by the `SHOW_STREAM_STATS` flag next to
  `Dashboard`'s other display flags; set it to `false` to remove the whole
  bar.
- **Numeric fields reject non-numeric input as you type**, rather than only
  validating after submission. Legacy (and an early version of this port)
  let you type anything into Blower FO/Magnetic FO/Sorting Quantity and only
  complained once you pressed Submit; these fields now strip non-digit
  characters immediately.
- **Confirmation before leaving an active scan.** Legacy has no protection at
  all against accidentally navigating away mid-scan — the desktop app simply
  doesn't have a "back" gesture in the same sense a browser does. The web
  version added confirmation prompts for every way a browser lets you leave a
  page: an in-app Back button, Cancel Batch, the browser's own Back button,
  and tab close/reload.
- **Loading vs. genuinely-empty dropdowns are visually distinct.** Legacy
  dropdowns look identical whether their options haven't arrived yet or there
  genuinely are none. Server-populated dropdowns (Vendor Name, Brand, Sorter
  Name) now show "Loading…" until their data arrives, so an empty list never
  looks broken.
- **Network/CORS failures are distinguished from a real wrong password** on
  the login screen. Legacy (and an earlier version of this port) showed the
  same "Invalid credentials"-style message regardless of the actual cause,
  which was actively misleading while diagnosing an unrelated networking bug
  during this project — see `9 - post_remediation_session_log.md`.
- **Abandoning a locked batch actually releases the conveyor interlock**,
  instead of leaving it stuck. `machine_start_locked` is one global flag
  shared by every screen that can send `machine_start`, in both legacy and
  here — legacy's `send_control_command` (`main.py:2320-2326`) checks it for
  any caller, including the unrelated Data Collection page's own Start button
  (`start_conveyor`, `main.py:1860-1865`). Legacy only ever clears the flag
  from the batch-scan flow itself, via Submit (`main.py:1321`) or Forward
  (`main.py:864`) — its own Cancel Batch (`cancel_result`, `main.py:2064-2081`)
  doesn't touch it either. So in legacy, walking away from a locked batch by
  any means other than Submit/Forward leaves the interlock engaged
  indefinitely, blocking Start on every other screen until the operator
  happens to revisit that exact batch and resolve it — confirmed live on real
  hardware (`POST /api/conveyor/command` for `machine_start` kept returning
  `409` from the Data Collection page long after the batch that caused the
  lock was gone). Three exits now clear it instead: `Dashboard.jsx`'s browser-
  Back handler calls the same `cancelScan` cleanup Cancel Batch already used
  (unlock, `all_stop`, archive crops) before letting the navigation through —
  it was the one abandon-a-batch path with no in-app click site to hook a
  cleanup into; `DetailsEntry.jsx` (the Data Collection page) unlocks on
  arrival, so a stale lock from an unrelated abandoned batch can't block its
  own hardware-test Start button; and `POST /api/scan/reset` (called once
  every time Dashboard mounts, before Start can be pressed for that batch)
  also unlocks — confirmed live: a fresh batch could arrive at this exact
  screen already locked from a previous session, with Start disabled and no
  pending detections to Submit/Forward against to clear it, a genuine dead
  end with no way out except Cancel Batch. `POST /api/scan/cancel` already
  did this for in-app Cancel Batch before this change; `/api/conveyor/unlock`
  (already existed for the Submit/Forward-adjacent paths) is what the new
  call sites use. A deliberate improvement over legacy's real stuck-forever
  trap, not a bug fix.
- **Submit reliably lands on, and stays on, the Start/Stop screen instead of
  sometimes flipping back into the FM-review overlay on its own.** `resume()`
  (Submit) clears `machine_start_locked` and `capture_paused` so the live view
  returns, matching legacy's `submit_all_fo_new` (`main.py:1310-1348`) exactly
  — but the belt is still physically stationary at that point (Start hasn't
  been pressed), and confirmed live on real hardware: the still-in-frame
  object that just tripped the interlock got redetected as "new" seconds
  later and re-locked the interlock on its own, no operator action, sending
  the UI right back into the FM-review overlay it had just left. The likely
  mechanism is the tracker losing that object's track across the
  capture-pause gap (no frames reach it while paused) and reassigning it a
  fresh id once frames resume — legacy's own tracker has no special handling
  for this gap either, so this may reproduce a genuine legacy bug rather than
  a porting mistake, but it wasn't verified against real legacy hardware and
  the fix was requested regardless. `scan_session.py`'s new
  `detection_suspended` flag (set in `resume()`, cleared in `start()`) skips
  detection — not the live preview — until the operator explicitly presses
  Start again, so Submit can't self-trigger a new lock. A deliberate
  deviation, not a legacy-matching fix, since it has no corresponding flag or
  gate in `main.py`.
- **A New Batch draft survives an involuntary round trip back to this screen**
  (the browser's own Back button carrying the operator back from Dashboard
  mid-batch), instead of silently discarding everything typed. This one exists
  specifically *because* of the web architecture (a React page component is
  destroyed and rebuilt on navigation; legacy's equivalent Qt page object
  never was), not because legacy did anything better here. Scope is
  deliberately narrow, per explicit request: leaving this screen on purpose —
  its own "← Back" button or Cancel, both `NewBatch.jsx`'s own click sites —
  clears the draft; only the round trip that lands back here without the
  operator choosing to return should restore it. A third site clears it too:
  confirmed live — completing a batch all the way through (Confirm & Finish
  on `ResultsViewer.jsx`) still left the just-finished batch's details
  prefilled on the next New Batch form, since that page has no connection to
  `NewBatch.jsx`'s sessionStorage draft at all. `handleFinish` now imports
  and calls `NewBatch.jsx`'s exported `clearDraft()` on a successful
  `confirmScan` — once a batch is genuinely done there's no round trip left
  to preserve the draft for.
- **"Blower FO"/"Magnetic FO" no longer appear as tap-to-classify FM options,
  and are shown in the Metric/Value table instead of the Item/Count table on
  the results screen.** Both are members of some commodities' Qualix-supplied
  `analysis` list (`CommodityDetails.analysis`), so — same as legacy, whose
  tap-to-classify dropdown reads the identical field
  (`main.py:1234`, `self.selected_analysis_dict = self.analysis_dict[com]`)
  — they used to show up as tappable classification choices on the
  detected-box overlay right alongside real FM types like Husk/Metal
  Fragments/Stones, confirmed live on a real prod device. That's misleading:
  they're not something the camera ever classifies at all, just two typed
  totals for material already removed by other machines (an air blower / a
  magnet) before or alongside the camera. Tapping either one was also
  provably pointless even before this change — the crop got saved to disk,
  but `create_results()` unconditionally overwrites
  `counter["Blower FO"]`/`counter["Magnetic FO"]` with the manually-typed
  sidebar values right after the file-count loop, in both legacy
  (`main.py:1468-1469`) and this port (`scan_session.py:531-532`) — so any
  per-object tally from tapping them was always silently discarded.
  `Dashboard.jsx`'s `fmOptions` now filters both names out before they ever
  reach the overlay or the `analysis_parameters` sent to the backend; on
  `ResultsViewer.jsx`, both are added to `METRIC_ITEMS` alongside the
  run-level metrics, so they render in the non-clickable Metric/Value table
  instead of the clickable Item/Count one (there is nothing meaningful for
  clicking them to filter the gallery to, now that they can never be tapped
  during classification).

- **Reclassify a captured object on the pre-Save review screen.** Genuinely
  new — legacy has no equivalent at all: `ImageLabel.mousePressEvent`
  (`main.py:219-246`) no-ops on a box that already has a label, and neither
  `create_results` nor `submit_create_result`/`save_result` offer any
  edit/undo path, even at Submit. Requested directly, not a legacy-parity
  fix.

  Classification is stored as the crop file's own filename prefix
  (`{fm_name}_{epoch_ms}.png`, `scan_session.label_detection`), and
  `create_results()` just counts files by that prefix — so reclassifying is
  a rename, nothing more, and only meaningful before `/confirm` persists the
  batch (after that, the crops may already be archived/synced). New backend
  methods `scan_session.list_pending_crops()`/`relabel_crop()` and endpoints
  `GET /api/scan/pending-crops` / `POST /api/scan/pending-crops/relabel`
  (`app/api/scan.py`), gated on the same `_pending_submission` window
  `/confirm` and `/discard` already use. A relabel re-runs `create_results()`
  and `build_datagram()` so the counts stay correct, rewrites `result.json`,
  and returns the same `{status, result, datagram}` shape `/submit` does, so
  `ResultsViewer.jsx`'s pending screen just swaps in the new result rather
  than re-fetching anything. Surfaced as a small gallery inside the pending
  screen's existing breakdown panel — each captured object has a dropdown to
  re-tap it to a different FM type (or back to `NON-FM`).

  **Known limitation, not fixed here**: the S3 upload worker
  (`s3_worker.py`, see `11 - data_folders_and_s3_upload.md`) sweeps
  `output/`/`output_frame/` every 60s independent of whether a batch is
  still pending, keyed by local filename with no rename-tracking. If a crop
  is uploaded before it's reclassified, the rename leaves a stale orphan
  object in S3 under the old name and a second upload under the new one —
  local and S3 state diverge for that one file. Rare in practice (a
  reclassify happens promptly, within the same short review window a
  60-second sweep may or may not have already caught), but a real gap if it
  does line up.

## Suggested future enhancements

These are not yet built. Some originate from open questions raised during
this project; they're listed here as options for the team to weigh in on
alongside the more clear-cut open work in `todos.md`.

- **Collapse Submit Batch back into one step.** Legacy's real behavior — and
  what this port now deliberately reproduces — is a two-step Submit-then-
  confirm flow where a batch is never saved or synced at all if the operator
  doesn't reach the second click (see `todos.md`). A single-step version
  (compute, save, and sync all in the one Submit click) was actually how this
  port worked for most of this project, before being deliberately changed
  back to match legacy exactly on request. Worth a real decision: match
  legacy's fragility, or keep the safer single-step behavior as an
  intentional improvement.
- **A periodic sweep for orphaned results**, as a safety net *underneath*
  whichever Submit design is chosen: find any `result.json` on disk with no
  matching database row (e.g., because the operator never confirmed) and
  save/sync it anyway on a timer. This would remove the two-step flow's risk
  without changing its UX.
- **Expose real status for the camera blower / camera light / belt blower /
  vibrator / hopper door / sensor hardware visible on the machine.** Neither
  legacy nor this port has any visibility into these at all — the software
  only ever sends five abstract commands and has no idea which physical
  components respond to them (see `9 - post_remediation_session_log.md` §7).
  If the machine's controller can report individual component status, surfacing
  it in the UI would be a genuine capability legacy never had, not just a
  port of one.
- **Finalize the PWA manifest's branding** (icon, theme color) to match the
  app's actual "Eye Compass" identity instead of the current placeholder
  values. `/public/logo.png` is the unrelated "Compass Group" corporate
  logo — legacy's own `app.setWindowIcon` (`main.py:3123`) uses the exact
  same file, so this isn't something this port broke, but it was never
  replaced with real branding either. Tried replacing it with a generated
  icon at one point; reverted on request (a stylized "Eye Compass" text
  wordmark was wanted instead, not a graphical mark — see `.brand-wordmark`
  in `global.css`, used in every header) — so the file itself is back to
  the original placeholder and still needs real artwork if the icon/favicon/
  PWA install icon should ever show something other than it. See
  `3 - frontend_setup_walkthrough.md` and `10 - pwa_and_deployment_rollout.md`.
- **Fix XAI View for real**, rather than just wiring the frontend button to
  the existing (currently broken) backend endpoint. See `todos.md` for the
  two candidate approaches already identified.
