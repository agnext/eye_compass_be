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

  Replaced with a 2-character `DEVICE_ID` (`.env`, required before any batch
  can be created) followed by 10-digit epoch seconds — e.g. `T11790153845` —
  so two devices can never collide regardless of what either has scanned
  before. The
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

  Two settings are involved and they are **not** interchangeable, which has
  already caused one outage:

  | Setting | Purpose | Shape |
  | --- | --- | --- |
  | `DEVICE_ID` | prefix on every batch number | exactly 2 chars, `A-Z`/`0-9` |
  | `DEVICE_CODE` | sent as `device_serial_no` on the Qualix scan datagram | free-form, whatever Qualix has registered |

  Neither is derived from the other and both are per physical device. They
  were originally assigned the opposite way round; the swap is recorded below
  under *Batch-id prefix and Qualix device serial swapped over*.

  `_device_code()` validates the prefix at the point of use rather than at
  import, so a misconfigured device still serves history and retries syncs
  instead of refusing to boot — but note the failure mode this produced in
  practice: the New Batch form showed "Generating…" forever, because the
  frontend had no handling for `/batch/next-number` failing and simply never
  replaced its loading text. Both halves were fixed (see *Batch-id prefix and
  Qualix device serial swapped over*).
- **Saving a scan is now idempotent, keyed on a client-generated UUID**, so a
  retry over a dropping link cannot produce a second scan record.
  `POST /api/scan/submit` mints a UUID and returns it; the frontend holds it
  until the batch is saved or discarded and sends it back on
  `POST /api/scan/confirm` as `client_request_id`. A request whose key is
  already stored is answered with the original `result_id` instead of saving
  the scan again.

  The problem this solves was not hypothetical duplicate rows — `/confirm`
  consumes a single in-memory pending slot, so a retry never actually
  inserted twice. It was the *response*: the retry found the slot already
  consumed and got `409 Nothing to confirm — submit a result first`, which
  the operator reads as "the save failed" for a scan that had in fact been
  saved and synced. The obvious next move — re-run the whole batch — is how
  genuine duplicates got created, one screen further back. The key turns that
  409 into the success it always was.

  Three layers again, deliberately, matching the batch-number approach:
  `confirm_scan` checks for the key before touching the pending slot; the
  `client_request_id` column carries a `UNIQUE` index so two retries racing
  past that check cannot both insert; and the resulting `IntegrityError` is
  caught and resolved by returning the row that won. The column is nullable
  and rows written before this feature (and any non-client writer) stay
  `NULL`, which a unique index permits any number of — that is what lets the
  index apply retroactively to a device with existing history.

  A replay deliberately does **not** re-queue the Qualix/Sheets sync: the
  first attempt already queued it and the 30-minute retry worker picks up
  anything still pending, so posting again on a replay would risk a duplicate
  reaching Qualix — the exact problem this is meant to prevent, one system
  downstream.

  Not to be confused with the `uuid` already inside the Qualix payload, which
  is a different key with a different job — that one is legacy-inherited and
  protects *Qualix* from recording a scan twice across delivery retries, and
  never touches this device's own database. `../external_apis.md` has a table
  comparing the two.

  **Where the key is minted, and where it is kept, are both load-bearing.**
  The first version generated it in the browser, on the results page, in a
  React ref. That covered the common case — press Save, press Save again on
  the same screen — but left a hole: the ref dies when the page unmounts, so
  an operator who pressed Save, saw it time out, wandered back to Home and
  returned would generate a *fresh* key, and the retry looked like a brand new
  save and got the same misleading 409 all over again.

  It is now minted by the backend at `/submit`, where it is tied to the
  submission rather than to a screen, and held in `sessionStorage`
  (`src/pendingSaveKey.js`) so it survives the results page unmounting and a
  page reload. It is cleared on a successful save, on discard, and when a
  batch is abandoned via the browser's Back button; a new `/submit` overwrites
  it. `sessionStorage` and not `localStorage` deliberately — a key surviving
  into another day could only ever cause a later save to be misread as a
  replay of something long since finished.

  Every `sessionStorage` access is wrapped in `try`/`catch`, because it throws
  rather than returning `null` when site data is blocked. Losing replay
  protection is acceptable; taking the Save button down with an exception is
  not.

  Generating it server-side also sidestepped a browser trap the first version
  had to work around: `crypto.randomUUID()` is only exposed in a secure
  context, and this app is routinely opened over plain `http` at the Jetson's
  LAN address (see `CORS_ORIGINS`), where it is `undefined` and calling it
  would have thrown inside the Save handler.
- **Batch-id prefix and Qualix device serial swapped over.** `DEVICE_ID` had
  been the value sent as `device_serial_no`, and `DEVICE_CODE` the batch-number
  prefix; they now hold the opposite roles (table above). The swap was
  requested to match how the fields are named and assigned on the Qualix side.

  Worth recording because the transition surfaced two separate defects:

  1. `DEVICE_CODE` had been relaxed from 2 characters to 2–6 to accommodate a
     4-character value (`CGI2`) that was really a device *serial*, not a batch
     prefix. That relaxation then exposed a latent bug: `_last_issued_ts`
     extracted the timestamp with a hardcoded `newest[2:]` slice, which for
     any prefix longer than 2 produced a non-numeric string — so it returned
     `None` every time, the monotonic clock guard had nothing to compare
     against, and a second batch created within the same second regenerated
     an id that already existed, hitting the `UNIQUE` constraint. Now sliced
     by `len(device_code)`. With the swap the prefix is back to a strict 2
     characters, but the slice fix stands on its own.
  2. A `DEVICE_CODE` that failed validation returned HTTP 500 from
     `/batch/next-number`, and the New Batch form showed **"Generating…"
     indefinitely** with no error and a still-clickable Start Batch button.
     The form now renders the backend's actual message in place of the id and
     disables Start Batch while there is no valid id.

  Note for a device with existing batches: changing the prefix does not
  invalidate anything. `_last_issued_ts` scopes its `MAX()` to the current
  prefix via a length-anchored `LIKE`, so old rows under the previous prefix
  are simply not consulted, and cannot collide with new ones precisely
  because the prefix differs.
- **Qualix is now told the device, operator and warehouse explicitly, instead
  of having to infer them from whichever account authenticated the post.**
  Every scan datagram carries a trio of new fields: `device_serial_no`
  (`DEVICE_CODE`), `warehouse_name` (`WAREHOUSE_NAME`) and `operator_id`.

  This replaces an approach that was started and then abandoned. Qualix maps a
  scan's location from the email of the account that posted it, so the obvious
  reading was that each sync should authenticate *as the operator who ran the
  batch*. That cannot work here, and the reason is structural rather than
  incidental: an operator who signed in offline (tier 2, cached password hash)
  or via the device fallback (tier 3) has no Keycloak token at all, and never
  will until they next log in online — so a sync that depended on their
  identity could never run for them. Syncing therefore stays on the fixed
  service account, and the identity that *matters* travels in the payload
  where it is always available. Qualix already accepts these fields.

  `operator_id` is Qualix's own `user.user_id`, read from its
  `/user/keycloak-profile` response at login. It is refreshed **only on a
  fresh online login** and otherwise carried forward unchanged from the
  `creds` row, so an offline login keeps posting the correct operator rather
  than blanking the field. A value that differs from the cached one is logged
  at WARNING before being stored, so a reassignment is visible rather than
  silent. Stored on both `sessions` and `creds`.

  `current_operator_id()` (`core/security.py`) reads it for the scan
  endpoints and is deliberately *soft* — it returns `""` rather than raising
  when there is no session, because `scan.py` enforces no authentication
  today and reading this must not become a new way for a scan to be rejected.

  Naming trap, documented because it has already misled once: the datagram
  also has a long-standing `device_id` field, which is the machine's own
  `/etc/machine-id` fingerprint via `get_device_id()` — unrelated to both the
  `DEVICE_ID` and `DEVICE_CODE` settings.
- **A rejected Qualix post no longer gets written to Google Sheets.** When
  Qualix rejects a payload outright (HTTP 400 — e.g. `{"error-code":"12092",
  "error-message":"Device does not exist"}`), that record is terminal and is
  not retried; sending it to Sheets anyway put a row there for a scan Qualix
  had explicitly refused, so the two systems disagreed about what existed.

  The cause was one call site out of three: `sync_result_to_cloud` in
  `scan.py` posted to Sheets unconditionally, while the retry worker and the
  manual resync path both already gated on success. Fixing it closed a second
  bug nobody had reported — pending (`'0'`) records were reaching Sheets on
  the first attempt *and* again on every subsequent retry, duplicating rows.
- **A rejection reason is now stored and shown, instead of living only in the
  backend log.** A `'2'` (rejected) record was previously a dead end on
  screen: the operator saw "Rejected" with no way to find out why. Qualix's
  reason is now parsed out of the response body (`_readable_qualix_error`
  handles its `error-code`/`error-message` shape, falling back to raw
  truncated text), stored on `result.sync_error`, and surfaced both in the
  History list and on the record detail page. `post_analysis_data` returns a
  3-tuple `(status, error_code, error_detail)`; all three call sites pass the
  detail through to `set_sync_status`, which clears it on a successful sync so
  a stale reason can never outlive the failure it described.
- **The sync identity and the emergency device login are now two separate
  accounts.** `QUALIX_USERNAME`/`QUALIX_PASSWORD` was doing both jobs at once:
  authenticating every outbound sync, *and* serving as the tier-3 credentials
  that unlock the device when Keycloak is unreachable and no cached password
  exists.

  That is wrong in both directions. The emergency credentials have to be
  shareable with whoever might need to recover a device in the field — so
  making them the same secret as the sync account means everyone holding the
  door key also holds the identity that posts every scan. And in the other
  direction it caused a real outage: the shared account existed in Qualix but
  not in Keycloak, so every sync failed with "Invalid user credentials" and
  nothing explained why.

  Now:

  | Setting | Job | Checked where |
  | --- | --- | --- |
  | `SYNC_SERVICE_USERNAME` / `_PASSWORD` | authenticates the scan POST and config fetch, under **both** providers | Keycloak, or Qualix on the legacy path |
  | `EMERGENCY_LOGIN_USERNAME` / `_PASSWORD` | tier-3 unlock only | entirely on-device, no network call |

  The emergency account need not exist in Keycloak or Qualix at all — it
  decides who gets *in*, never what anything is sent *as*.

  Two details worth knowing. **Syncing now uses `SYNC_SERVICE_*` on the legacy
  path too** (`sync_worker.py`, `history.py`, `sync_service.py` previously read
  `QUALIX_*` there); one setting means one account, whichever provider is
  selected. And **`SYNC_SERVICE_*` has no fallback on purpose** — quietly
  borrowing another account is precisely how the outage above went unnoticed,
  so a blank value now fails loudly and logs what to set.
  `QUALIX_USERNAME`/`QUALIX_PASSWORD` were then **removed as settings
  entirely** — nothing reads them as such. They survive only as deprecated
  *aliases* when resolving `EMERGENCY_LOGIN_*`, alongside legacy's
  `config.INI`, which is the step that actually matters: on a real device the
  credentials live in `config.INI` (`[CONFIG_SETTINGS] username`/`password`),
  not necessarily in `.env` at all. Dropping those reads would have silently
  taken away the emergency login on upgrade — the one path whose entire
  purpose is to work when everything else has failed. A startup warning names
  whichever old source is still supplying the value, so it is visible when a
  device has been migrated and the old entries can go.

- **The same scan can no longer be delivered twice at once.** Three things
  deliver to Qualix — the post right after a batch is saved, the retry worker,
  and History's manual re-sync — and nothing stopped two of them working on
  the same record concurrently.

  This needed no operator action to happen. A record stays at
  `sync_status='0'` for the *whole* duration of its POST, and that POST is
  slow (the endpoint averages ~30s). So a batch saved shortly before the retry
  worker's tick is still listed as unsent when the worker asks what needs
  sending, and both post it. A manual re-sync click during that window is
  simply a third way in, as is double-tapping the button.

  **What actually broke was Google Sheets, not Qualix.** Qualix recognises the
  repeat by `sample_id` and answers `12063`, which is handled. But
  `post_to_sheets` checks `already_in_sheet` and *then* appends — two
  deliveries interleaving between those two steps both see "not present" and
  both append, putting two rows in the sheet for one scan.

  `services/sync_lock.py` holds a per-result-id claim (`claim_result`, a
  context manager yielding whether the claim was granted). All three
  deliverers take it: the background post returns early if it cannot get it,
  the worker skips the record and looks again next cycle, and the manual
  re-sync answers **409** with a message telling the operator it is already
  being sent and will update on its own. Claims are per record — two
  *different* records syncing at once is normal — and are released even when a
  delivery raises. In-process only, which is all that is needed: one backend
  process owns this device, the same assumption behind the single-slot pending
  submission in `api/scan.py`.

  Worth knowing for whoever re-adds a Sync button: **there is currently no UI
  caller for re-sync at all.** It was removed from History on request, and
  `History.jsx`'s comment claiming `ResultsViewer.jsx` still uses it is stale —
  nothing does. The endpoint and its RTK Query mutation remain, so this guard
  is in place ahead of the button coming back.

- **`Sample ID already exists` from Qualix is now recorded as delivered, not
  rejected, and the scan POST waits 90s instead of 30.** These are one finding:
  a real scan was filed as failed when Qualix had actually stored it.

  The POST had a 30s read timeout. A live measurement against the dev gateway
  answered in **28.7s** — so the limit sat right on top of the endpoint's real
  response time, and requests that crossed it were recorded as undelivered
  even though Qualix had received and stored them. The retry then came back
  `400 {"error-code":"12063","error-message":"Sample ID already exists"}`,
  which was filed as `'2'` (Rejected) — terminal, so it would have sat on the
  History screen looking like lost data forever, while the scan was safe in
  Qualix the whole time.

  Both halves are fixed: `12063` maps to `'1'` (delivered) with the stored
  error cleared, and `_POST_TIMEOUT_SECONDS` is 90. Nothing waits on this call
  — the post after a batch is a background task and the worker has no user
  attached — so a longer wait costs nothing, while giving up early costs a
  scan that looks lost. The config `GET` stays at 30s: no evidence it is slow,
  and it *is* on the login path.

  Treating a duplicate as success is only safe because **batch numbers are
  globally unique** (device code + epoch, `UNIQUE` constraint), so a
  `sample_id` can only already exist at Qualix if this same scan reached them
  before. Two devices sharing a `DEVICE_ID` would break that assumption — the
  reason the acceptance is logged at WARNING rather than silently.

  The check is in `post_analysis_data`, so the immediate post, the retry
  worker and the manual re-sync all inherit it. Raising the timeout makes the
  case rarer but cannot remove it: no timeout distinguishes "still working"
  from "never going to answer".
- **`resync_result` no longer falls back to a direct Qualix login when
  `AUTH_PROVIDER=keycloak`.** Found while auditing the above for similar
  cases. The fallback was pointless in that mode — `_auth_headers()` ignores
  the resulting `access_token` entirely and uses the gateway's own token — so
  a failure surfaced as a confusing Qualix login error rather than the real
  one. It now raises `502` directly, matching the guard the sync worker
  already had.
- **Config-fetch failures now log the URL and the response body**, not just a
  bare status code. A `Config fetch failed` line reporting only HTTP 500/503
  gave nothing to act on; both the non-200 branch and the exception branch now
  include where the call went and what came back (truncated).
- **`POST /api/config/sync` reports real completion.** It previously handed
  the work to a `BackgroundTasks` job and returned success immediately —
  meaning "accepted", not "done" — so a caller could not tell a finished sync
  from a failed one, and the frontend had no moment at which to refresh. It
  now runs inline and returns `{"status": ..., "synced": bool}`.

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
- **Fixed a silently-ignored RTK Query option that made freshly synced config
  invisible.** Reported as: a surveyor synced from Qualix (the backend log
  confirming `1 surveyors`, and the row present in Postgres) never appeared in
  the New Batch dropdown — not on revisiting the page, not until a full logout
  and login.

  The cause was a genuine API misuse rather than a caching-policy choice:
  `refetchOnMountOrArgChange` was set inside each `builder.query({...})`
  definition in `configApi.js`. It is not a valid endpoint-level option — it
  belongs on the `createApi` root (or on an individual hook call) — and RTK
  Query neither applies nor warns about it there. It was moved to the root,
  so mounting New Batch now genuinely refetches. `syncConfig` additionally
  declares `invalidatesTags: ['Config']`, which (together with the backend's
  `/config/sync` now completing inline rather than returning early) makes a
  sync update open screens immediately, without needing a remount at all.

  Worth flagging for future work in this codebase: an unrecognised key in an
  endpoint definition is accepted in silence, so this class of bug does not
  announce itself. The first diagnosis here was wrong for exactly that reason
  — the behavior looked like a stale-cache timing issue, and was only
  identified correctly after the reported "reopening the page doesn't help"
  ruled that out.
- **Screens re-read the backend on every visit, because the kiosk has no
  reload button.** This is a device constraint the web architecture does not
  otherwise account for: the Jetson runs a single full-screen browser with no
  address bar and no F5, so whatever RTK Query has cached is simply what the
  operator sees. There is no user-accessible way to force a refresh.

  `refetchOnMountOrArgChange: true` now sits on `historyApi` and `scanApi`
  (it was already on `configApi`), so every visit to History, a record, or
  Reclassify Objects reads the database rather than replaying the previous
  visit's response. Without it, re-opening History inside
  `keepUnusedDataFor` — 60s after the last subscriber goes away — showed the
  earlier rows, including sync statuses for batches that had since been
  delivered.

  It has to be set on `createApi`, never inside `builder.query()`: it is a
  CreateApiOptions/hook option and RTK Query **silently ignores it** on an
  endpoint definition. In plain JS nothing flags that, which is exactly how
  the same mistake went unnoticed in `configApi.js` (symptom: a synced
  surveyor that never appeared in a dropdown). An audit script now confirms no
  slice has it misplaced.

  The remaining slices are deliberate, not oversights: `authApi`'s `me` and
  `batchApi`'s `next-number` are passed the same option **hook-side** by
  `Home.jsx` and `NewBatch.jsx`, which is equally valid; `batchApi.getBatch`,
  `cameraApi.getCameraStatus` and `conveyorApi.getConveyorStatus` have no
  callers at all.

- **History and an unsettled record poll, so a sync status changes on screen
  rather than only on re-entry.** Refetching on mount fixes opening a screen;
  it does nothing for one already open. A row's Sync Status is *not* settled
  when it first appears — the Qualix post runs as a background task against an
  endpoint averaging ~30s, and the retry worker changes statuses long
  afterwards with nothing on screen to trigger a re-read. On a desktop the
  operator would press F5; here a batch shown as "Pending" would stay
  "Pending" until they navigated away and back, which looks exactly like a
  sync that never completed.

  The History list polls every 15s while open. A record page polls every 10s
  **only while its own `sync_status` is `'0'`** — an accepted or rejected
  record cannot change again on its own, and this device leaves screens open
  for long stretches, so there is no reason to keep asking about a settled
  one.

  Implementation note, since the obvious version does not compile: the polling
  interval cannot be derived inline from the query's own result, because that
  result is declared by the very call being configured (a temporal dead zone
  error). It is held in state and updated with React's documented "adjusting
  state during render" pattern rather than an effect — one guarded assignment,
  which settles immediately instead of painting once with the stale value.

- **Sorting Quantity accepts positive whole numbers only.** It is a count, not
  a weight, but previously took decimals and negatives. The field now strips
  everything but digits as the operator types (`value.replace(/\D/g, '')`, so
  `.` and `-` cannot be entered at all), and `BatchCreate` enforces the same
  rule server-side with `re.fullmatch(r"[1-9]\d*", ...)` — deliberately a
  regex and not a bare `int()` call, which would also accept `-5`, `1e10` and
  `0`. Both halves are needed: the frontend filter is the usable one, the
  backend check is the one that holds for a request that never went through
  the form.
- **The sync outcome is visible where the records are.** The History list
  shows the rejection reason beneath the status pill (clamped to two lines,
  full text on hover), and the record detail page shows it as a banner —
  red for rejected, amber for still pending. Both read `sync_error` from the
  API described in the backend section above.
- **History lists the last 30 days, not everything ever scanned.** Nothing
  prunes the `result` table, so a device that has been in the field for a
  season has thousands of rows behind that screen, and an operator on a touch
  screen is realistically only ever looking for something from the last few
  days. `GET /api/history/` now filters to `HISTORY_WINDOW_DAYS` (default 30,
  `0` disables the window), and the screen says which window it is showing —
  otherwise a missing two-month-old batch reads as lost data rather than as a
  list that stops.

  Nothing is deleted and nothing stops syncing. The window is on the *list*
  only: `GET /api/history/{id}` still serves a record of any age, so an
  existing link or a support request for an old batch still works, and the
  per-request `?days=` override (`0` for everything) exists so support can
  pull one without editing a device's `.env` and restarting it.

  The window is on the scan date — when the device did the work — not on the
  operator-entered `receiving_date`, which is free to be older and would make
  the window mean something other than what it says.

  Two details worth recording. The filter is a plain text comparison against
  the indexed `result.date` column, which is sound *because* that column is
  written zero-padded `"%Y-%m-%d"`: for that format, and only for it,
  lexicographic order is chronological order. And the endpoint now filters,
  orders and pages in SQL; it previously did all three in Python over
  `db.query(Result).all()`, reading every scan the device had ever taken —
  full JSONB datagrams included — in order to return twenty of them.

  Unrelated despite the similar name: `GALLERY_LIMIT` in `ResultsViewer.jsx`
  caps how many FO crops *one* record's gallery fetches in a single call. It
  has nothing to do with how far back the History list reaches.

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
