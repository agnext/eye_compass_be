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
- **Captured-object crops now have a 20px margin around the detection box,
  not legacy's 10px.** Every saved crop (`scan_session.label_detection`/
  `save_unselected`) is cut directly from the box in `self.pending`, which is
  built by padding the model's raw detection box via `enlarge_bbox` before
  it's ever stored — one enlarge, then a single slice, not crop-then-pad.
  Legacy pads by exactly the same mechanism at the same call site
  (`GrabImage.py:574-616`, `pad=10`), and the port matched that value
  exactly until this was first raised: crops were reported as too tightly
  cropped around the object to read clearly, especially on this device's
  touchscreen where a technician is judging a small thumbnail, and it went
  to 30. That overshot — measured on a real batch, a 30px pad left the
  object filling only about a third of its own crop (a ~30px object in a
  ~90px image, the rest bare belt), so it rendered small in the preview.
  Settled at 20 on request. `enlarge_bbox(b, pad=20, ...)` in
  `scan_session.py`'s `process_frame` — a deliberate deviation from legacy's
  value, not a bug fix, and it also sets the tap-to-classify overlay box
  shown live on the frozen frame (both draw from the same padded list),
  which is an intended side effect, not a separate change.

  Worth recording for the next time crop legibility comes up: **padding is
  the only software lever on it.** The objects are ~30-55 real sensor pixels
  in a 1920x1200 frame, so less padding makes an object render *bigger*, never
  sharper. Upscaling the crop at save time was tried and measured against a
  browser's own stretch of the same file — the two are visually
  indistinguishable, so it was not adopted (it would have inflated every
  stored crop ~9x for no visible gain). Anything beyond this needs more
  optical resolution, not code.
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
- **Data Collection's page shows a live camera preview immediately, not only
  once recording starts.** Legacy only ever emits a preview frame from inside
  `CollectionCameraThread.run()` (`GrabImage.py:733-826`), i.e. between
  `start_dc`/`capture_image_dc` and `stop_dc` — the port matched that
  exactly at first, so arriving at the page showed a grey placeholder until
  Start was pressed. Reported as looking like a broken/frozen camera, not
  "recording hasn't started yet" (worth noting: it wasn't actually a bug at
  the time — this doc's own earlier text said so — just an unhelpful piece
  of legacy fidelity). `data_collection_stream` (`app/api/camera.py`) now
  grabs and sends a frame on every loop iteration regardless of
  `_dc_recording`; only the disk write (the actual "data collection" part)
  stays gated on it, matching Dashboard's own live-scan page
  (`/ws/camera/stream`), which never had this restriction. A deliberate
  deviation from legacy, not a bug fix.
- **Data Collection's finish step cleans up unconditionally.** Legacy's
  `submit_dc`/`back_from_dc` just navigate away — if the operator forgot to
  press Stop first, the frame-recording thread and the belt both keep running
  in the background indefinitely. `POST /api/camera/data_collection/finish`
  stops both before the frontend navigates away, closing that leak.
- **The Qualix bearer token is real.** Legacy issued the literal string
  `"dummy_offline_token"` for an offline login, which nothing downstream could
  actually authenticate with. The backend issues a real session token
  (`SessionStore`, with no time limit — a session ends only when Keycloak says
  the account is no longer good) for both the online and offline login paths.
- **The live `total_fo_detected` no longer counts detections that were
  suppressed from operator review.** Legacy's `handle_detection`
  (`main.py:2618-2622`) adds every new tracker id to `existing_track_ids`
  whether or not `has_similar_x_axis` decided it was the same physical object
  already awaiting a label — so a re-identified duplicate inflates the count
  as if it were a second object, and one that is never photographed, since
  crops are only written for items that reach `self.pending`. `scan_session`
  now keeps a second set, `counted_track_ids`, holding only ids actually
  queued for review, and the live count reports that instead.
  `existing_track_ids` is still accumulated unchanged, purely to keep a
  suppressed duplicate from being re-evaluated on every subsequent frame.
  Requested directly after the mismatch was noticed on screen. Note this only
  moves the **live, in-progress** number shown during a scan: the saved
  result's `total_fo_detected` is computed by `create_results()` counting crop
  *files* on disk, which never included suppressed detections in the first
  place. An earlier attempt at this was reverted (see
  `9 - post_remediation_session_log.md` §7b) to preserve legacy parity for the
  Qualix datagram — that reasoning still applies to the saved figure, which is
  unchanged here.
- **The reclassify endpoint is serialized against itself and against Save.**
  `POST /api/scan/pending-crops/relabel` renames one crop, then re-counts the
  entire batch folder and overwrites the module-level `_pending_submission`
  with what it measured. Only the rename was locked. FastAPI runs plain `def`
  endpoints on a threadpool, and the reclassify screen fires one request per
  staged change (≈20 within a few seconds in a real batch), so a request that
  measured the folder *before* a sibling's rename landed could finish last and
  write its stale totals over the fresher ones — and whatever sits in that
  slot when the operator presses Save is exactly what is persisted and synced
  to Qualix. Reproduced with a threaded harness against the unlocked code (4
  of 12 runs persisted counts that disagreed with the folder, e.g. a phantom
  `FM: 1` alongside `Husk: 7` where disk held `Husk: 8`); 12 of 12 clean once
  locked. Note this is a *latent* bug — it is NOT what corrupted batch
  `milind4550` (see the `create_results` double-count in
  `9 - post_remediation_session_log.md`), since a pure rename cannot change
  the file total the way that record's did. A module-level
  `_pending_lock` now spans the whole rename → recount → publish cycle, and
  `/submit`, `/confirm`, `/discard` and `/pending-crops` take it too, so Save
  waits for an in-flight reclassify instead of persisting a half-applied one.
  Not a legacy concern at all — legacy has no reclassify path and is
  single-threaded Qt.
- **Operator login can be pointed at Keycloak instead of Qualix**
  (`AUTH_PROVIDER=keycloak`), reversing the "not moved to an external
  identity provider" decision in `1 - strategy.md`. Keycloak already fronts
  Qualix org-wide through the **Assurance** gateway and shares its userbase,
  so this adopts existing infrastructure rather than introducing a new
  dependency. The operator still types into the same login form — Keycloak is
  reached by a direct grant (ROPC), not a hosted redirect page, specifically
  so the password still reaches this backend and can be hashed for the
  offline tier. A hosted-page flow would break that, and is only needed if
  MFA is ever enforced. Defaults to `legacy`, so an un-switched device is
  byte-for-byte unchanged. New `app/services/keycloak_service.py`; the login
  cascade in `auth.py` splits into `_login_keycloak` / `_login_legacy` over a
  shared `_offline_tiers`, so tiers 2 and 3 cannot drift apart between
  providers.
- **Operator login moved to Keycloak** (behind `AUTH_PROVIDER=legacy|keycloak`,
  default `legacy`), with syncing moved off the operator's own identity onto a
  fixed account routed through the Assurance gateway, sessions persisted to
  Postgres instead of a process-local dict, and a daily worker that
  re-confirms each session against Keycloak without ever cutting an operator
  off mid-batch. Full detail, including why each of these was necessary and
  how they were verified, is in
  `docs/keycloak_integration/` (start at `1 - overview.md`) rather than
  repeated here.
- **Batch number auto-generation was reworked from a per-device row id to a
  device-namespaced timestamp**, to fix a real cross-device collision, not a
  hypothetical one: the first version built the id from commodity + variety +
  date + the batch row's own primary key
  (`{COMMODITY}-{VARIETY}-{YYYYMMDD}-{row_id:06d}`), and since every device's
  primary key restarts at 1, two Jetsons reliably produced the identical id
  once their scans reached the same place (Qualix / the shared spreadsheet).

  Replaced with a 2-character `DEVICE_CODE` (new required setting, `.env`)
  followed by 10-digit epoch seconds — e.g. `D11790014601` — so two devices
  can never collide regardless of what either has scanned before. The
  timestamp alone is not a safe uniqueness guarantee on this hardware: these
  Jetsons have no battery-backed RTC, so the clock can come up in the past
  after a reboot and re-issue a second it already used, and two batches saved
  within the same second is a realistic double-submit at one-second
  resolution, not just a theoretical one. `_generate_batch_number` in
  `api/batch.py` guards against both — it reads the newest id this device has
  already issued (`_last_issued_ts`, a `MAX()` scoped to this device's own
  prefix) and clamps to `last + 1` whenever the clock is not strictly ahead,
  logging a warning so a misbehaving clock is visible rather than silent. The
  `batch_number` column itself also carries a `UNIQUE` constraint as the final
  backstop (`_add_missing_constraints` in `main.py` applies it retroactively
  via `CREATE UNIQUE INDEX IF NOT EXISTS`, since `create_all` never alters an
  existing table), with the `/batch/new` endpoint retrying on the resulting
  `IntegrityError` — three independent layers, not just the timestamp.

  The id is now allocated *before* the batch is saved, not after: `GET
  /batch/next-number` lets the New Batch form show the real id the moment it
  opens (previously it showed a `NNNNNN` placeholder for the row-id suffix,
  since that didn't exist until the row was inserted), and the id shown is
  sent back on save and honored as-is if it's still well-formed and free —
  falling back to a fresh one otherwise — so what the operator sees on screen
  is what actually gets stored, not a preview that could drift from it.

### Frontend
- **The Home screen checks whether the backend has flagged the session for
  re-login** (`needs_relogin` from `/auth/me`) and, if so, signs the operator
  out with an explanatory notice on the login screen. Deliberately checked on
  Home and nowhere else: the backend leaves a flagged session fully working
  precisely so an operator part-way through a batch scan or data-collection
  run is not cut off and does not lose it. Home is the only screen reachable
  between tasks, which is what makes it the safe enforcement point. This also
  put `useMeQuery` to use for the first time — it had been defined and
  exported in `authApi.js` since the port but never called by anything.
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
- **Sorting Quantity is a whole positive number, not a decimal weight.** It
  briefly allowed one decimal point (`40.5`) on the assumption it recorded a
  weight; on request it was tightened to a plain count instead. `handleNumericChange`
  in `NewBatch.jsx` now strips everything but digits — `.` and `-` can no
  longer be typed at all — and `BatchCreate.sorting_quantity_must_be_a_positive_whole_number`
  in `api/batch.py` enforces `[1-9]\d*` server-side, rejecting `0` and a
  leading-zero value like `007` as well as decimals, since a request that
  skips the form entirely still has to pass the same rule.
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
- **XAI View is disabled on the live-scan page.** The button and its handler
  (`Dashboard.jsx`'s `handleXaiToggle`/`xaiImage` state) are left in place
  but commented out of the rendered header, on request, "until further
  notice." Not a removal of the feature, just hidden.
- **"Item" renamed to "FO Category"** in the breakdown table headers on both
  the Save/Results-review screen and the History → record-view screen
  (`ResultsViewer.jsx`), on request — purely a label change, the underlying
  `item`/`itemRows` field names are untouched.
- **A note on Blower FO / Magnetic FO** was added explaining they are typed
  totals from other machine stages (the air blower, the magnetic separator),
  not camera detections, and can't be reclassified like a captured crop —
  shown only on the pending (not-yet-saved) results screen
  (`showFoInfoNote`, gated to `isPending`), not on a saved History record.
- **The reclassify screen's own staged-changes log is ordered by object
  number, not by the order each change was staged.** Object 1's change (if
  any) always appears above Object 5's, matching `objectNumberById`'s own
  numbering, rather than reshuffling every time the operator reclassifies
  something out of order.
- **The live tap-to-classify FM overlay (Dashboard.jsx) has no Cancel
  option.** Once the operator taps a detected box, they must pick an FM
  type from the popup — there is no way to dismiss the picker without
  labeling it, on request. (This also matches legacy, which never had a
  Cancel/dismiss affordance on this exact overlay either — main.py's own
  detection-review flow has no equivalent button.)
- **FM type lists are alphabetized app-wide** (the Reclassify "Change to"
  dropdown, Dashboard's tap-to-classify overlay, DetailsEntry's pre-scan FM
  select, and the reclassify gallery's own type filter) — `NON-FM` (and, in
  the FO Category table specifically, `Blower FO`/`Magnetic FO`) stay pinned
  to a fixed position rather than sorting in alphabetically, since they
  aren't real "found" FM types the way the rest are.
- **`CustomSelect` (`src/components/CustomSelect.jsx`) and `ScrollFrame`
  (`src/components/ScrollFrame.jsx`)** are new reusable components with no
  legacy equivalent. A native `<select>`'s open dropdown draws its own
  scrollbar at the OS/browser level, which can't be made to behave like the
  rest of the app's always-visible ScrollFrame scrollbars (it flashes and
  auto-hides), so `CustomSelect` renders its own option list (via
  `ScrollFrame`) in a `document.body` portal instead — used for
  ReclassifyObjects.jsx's "Change to" dropdown and both pages' "Filter by
  type" gallery filter; not yet swapped in for every native `<select>` in
  the app (kept scoped on request). `ScrollFrame` itself pairs a real,
  always-visible native scrollbar with up/down nudge buttons, and is only
  rendered when there is actual overflow to scroll.
- **Tap-to-preview modal for captured-object crops**, on both
  ReclassifyObjects.jsx and ResultsViewer.jsx — tapping a gallery thumbnail
  opens an enlarged view with Previous/Next navigation (and, on the
  reclassify page, a jump-to-object-number control); not a legacy feature.
  Sized at `min(760px, 96vw)` / up to 70vh image height (enlarged from an
  initial, smaller size on request — crop legibility is bounded by the
  underlying ~30-55px of real sensor detail, see the crop-padding entry
  above, so this is a genuine size increase, not a workaround for blur).
- **History's Sync Status is a pill only, with no manual retry button at
  all**, for every status. Legacy has no manual resync concept in the first
  place (see the very next bullet); this port initially added a button for
  both a pending ('0') and a failed/rejected ('2') record, then removed both
  on request: `sync_worker.py`'s own periodic retry already covers '0'
  automatically, and a '2' record was rejected by Qualix outright (HTTP
  400) — resending the exact same payload changes nothing, so a retry button
  there never actually helped. `'2'`'s label was changed from "Sync Failed"
  to "Rejected" to stop implying a retry could fix it. Applied identically
  on the History table and the ResultsViewer record-view header.
- **History is sorted latest-first (by scan date/start_time), not by
  legacy's commodity-name grouping.** Legacy's own `populate_history_table`
  (`main.py:2100`) sorts rows by commodity name descending, then receiving
  date descending within each commodity — reproduced exactly at first, then
  changed to latest-first on request once it was confirmed this diverges
  from legacy on purpose (see `history.py`'s own `get_history` docstring for
  the full reasoning).
- **The record-view gallery fetches every crop for the record in a single
  request**, not paged 12/24 at a time. The gallery is now a single
  scrollable grid (ScrollFrame provides the scrolling), not legacy's paged
  previous/next viewer, so paging server-side only capped the grid at its
  first page with nothing to reach the rest once the paged viewer's own
  prev/next controls were removed. `get_result_images`'s `limit` cap was
  raised from 200 to 2000 to match.
- **Data Collection's live camera preview and the reclassify/record-view
  crop previews had their own render/UX bugs found and fixed along the
  way** — see `9 - post_remediation_session_log.md` §7m (preview closing
  itself on a double-tap) and §7n (a portal-rendered dropdown opening
  visually behind a modal).
- **The saved/History-detail record-view screen (`ResultsViewer.jsx`,
  `!isPending`) no longer has a docked, narrow enlarged-crop column.** It
  originally mirrored the pending-review screen's own frame/breakdown
  layout; on request this became first a full-width thumbnail gallery with
  a "Filter by type" dropdown (tapping a thumbnail opens the same
  tap-to-preview modal used elsewhere instead of an inline enlarged view),
  then restructured again to match ReclassifyObjects.jsx's own layout
  exactly — a gallery grid on the left, a fixed-width panel on the right
  (the FO Category/Metric tables, in place of reclassify's staged-changes
  log). The `isPending` (pre-save) screen's own layout from §7g is
  untouched by any of this.
- **Sync Status pill and the (now-removed) Sync Now button were sized to
  match the Home button, and made uppercase.** Both were noticeably smaller
  than the header's own Home button on this device's screen; sizing was
  matched explicitly rather than inherited from a shared button class,
  since they sit in the same header row on both History.jsx and
  ResultsViewer.jsx.
- **Login page redesigned to match the organization's Keycloak login
  screen** (`Login.jsx`/`Login.css`) — an "EYE COMPASS" wordmark, a card with
  the app's own green accent (not Keycloak's blue) so it still reads as this
  app, a password show/hide toggle for the touchscreen keyboard, and a
  footer with the AgNext logo and a "Secured by Keycloak" mark. Forgot
  Password/Signup/Register are deliberately absent — nothing on this kiosk
  device can act on them (no email access, no self-registration flow), so
  showing them would just be a dead end for the operator. No "Remember me"
  either: sessions already stay logged in indefinitely by design (see
  `docs/keycloak_integration/`), so there's nothing left for it to do.
- **The email field validates format before any network call, and suggests
  previously-used emails on this device.** `isValidEmail` rejects an
  incomplete address (e.g. missing `@`/domain) immediately, instead of
  waiting on a round trip just to get "Invalid credentials" back for
  something that could never have been valid. Suggestions come from the
  app's own `localStorage` (`config.js`'s `getRememberedEmails`/
  `rememberEmail`/`forgetEmail`, last 5, most recent first, only ever added
  after a successful login), rendered as a touch-sized dropdown in
  `Login.jsx` — not the browser's native autocomplete/`<datalist>`, both of
  which are switched off device-wide (see the kiosk lockdown below) and
  whose rows are too small to tap reliably anyway. Passwords are
  deliberately never remembered or suggested this way, in the app or the
  browser: several operators share this one device, and the backend only
  ever keeps a hash, so it couldn't supply one back even if asked.
- **Kiosk browser hardening.** The Firefox instance this app runs in full
  time on the device now ships a `policies.json` (password manager, other
  autofill, `about:` pages, devtools, extension installs and auto-update all
  disabled) and relaunches itself automatically if closed or crashed,
  instead of leaving the bare desktop exposed. This is a device-hardening
  change with no effect on the app's own behavior — full detail in `10 -
  pwa_and_deployment_rollout.md`'s Kiosk browser section.

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
