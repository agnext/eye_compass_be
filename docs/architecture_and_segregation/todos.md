# TODOs — open items to revisit

This is the master list of outstanding work. Items 1–7 below are in priority
order; each links to a detailed section if one exists further down this file
or elsewhere in this doc series. See also `7 - deployment_and_qa_checklist.md`
for the hardware-QA-specific subset of this list.

## Open work items, in priority order

1. **Decide what happens after Submit in batch mode.** Submit Batch is
   currently a two-step flow matching legacy exactly (compute-and-review,
   then a separate confirm that actually saves/syncs) — including legacy's
   own real fragility, where a batch is never saved at all if the operator
   doesn't reach the second step. This needs a team decision, not a unilateral
   fix. See "Submit Batch is a two-step flow" below for the full detail and
   the options.
2. **Finish the results display page.** `ResultsViewer.jsx` now handles both
   the not-yet-confirmed and already-saved cases (see item 1), but unlike
   Login, Home, New Batch, Data Collection, and the live-scan page, it has not
   yet had the same rigorous side-by-side comparison against a real
   production screenshot that those screens got — see
   `9 - post_remediation_session_log.md` §3 for what that process looked like
   for the other screens. Its layout, field set, and behavior should be
   verified the same way before considering it done.
3. **Test the data migration script for real.** `migrate_sqlite_to_postgres.py`
   has been verified to correctly *parse* all 79 legacy records and pick the
   right dedup key (see `8 - remediation_log.md` §9), but has not actually
   been run as a real cutover against a production device's live SQLite
   database into a real Postgres instance.
4. **Figure out how an existing production device gets migrated onto this
   stack.** There is currently no documented or tested procedure for taking a
   device that's running the legacy PyQt5 app today and replacing it with
   `eye_compass_be` + `eye_compass_fe` — stopping the legacy service, running
   the migration script, standing up the new backend/frontend/database,
   verifying nothing regresses, and a rollback plan if something goes wrong.
5. **Test sending data to Qualix for real.** The backend builds and sends the
   full Qualix datagram correctly (verified structurally — all 21
   `scan_data` fields, the `analysis` array, etc.), but an actual successful
   round-trip against the real Qualix service, and a look at what a real
   rejection (`sync_status='2'`) looks like in practice, has not been
   confirmed in this project.
6. **Do thorough testing, likely per-commodity.** Inference correctness
   (`resolve_model`, per-commodity confidence thresholds, the suppression
   rules) was verified structurally against legacy's own logic
   (`8 - remediation_log.md` §4), but real detection quality against real
   samples has only been spot-checked, not exercised across the commodity
   list. Recall that four `.optimized` model files (`stem_rice`, `toor`,
   `masoor_dal`, `chitra_rajma`) are missing from this device's `models/`
   folder already — `stem_rice` specifically is the fallback model, so this
   blocks testing an unmapped commodity until restored.
7. **Test running this as an installed PWA, and set it to start on device
   boot.** Boot-time launch is now wired up on this dev device (backend
   service already verified working; kiosk browser launch not yet verified
   end-to-end — see the sub-items below):
   - `~/.local/bin/eye-compass-kiosk.sh` — waits for both the backend
     (`:8000/`) and frontend (`:5173`) to actually answer before launching
     the browser (see below) in kiosk mode, instead of firing it blindly at
     login and risking a blank/error page.
   - **Real finding, not in the original plan: neither Chromium nor Firefox
     can run as a snap on this device at all.** This Jetson's kernel has
     `CONFIG_SECURITY_APPARMOR` unset, which `snap-confine` hard-requires —
     confirmed via `/proc/config.gz` and `aa-status`, and this is what
     Ubuntu 22.04 arm64's `chromium-browser`/`firefox` apt packages both
     actually install (both are transitional dummy packages, not real
     browsers). Worked around by downloading Mozilla's official standalone
     Firefox linux-aarch64 tarball to `~/.local/opt/firefox` (a real ELF
     binary, no snap involved) and launching it with a dedicated kiosk
     profile (`~/.local/opt/firefox-kiosk-profile`) via `--kiosk
     --no-remote`. See `10 - pwa_and_deployment_rollout.md` for the full
     detail.
   - `~/.config/autostart/eye-compass-kiosk.desktop` — XDG autostart entry
     that runs the script above at graphical login. GDM auto-login for user
     `nvidia` was already enabled (`/etc/gdm3/custom.conf`), so no manual
     login step is needed at power-on.
   - `docker-compose.yml`'s `db`/`frontend` services already have
     `restart: always`, and `docker.service` is already enabled at boot
     (confirmed via `systemctl is-enabled docker`) — so both containers
     should come back on their own after a reboot.
   - The backend systemd service was set up AND verified working this
     session: enabled, manually started once (after stopping the
     dev-terminal `uvicorn` process), `journalctl -u eye-compass-backend`
     showed a clean startup identical to the manual run, and both
     `curl http://localhost:8000/` and `:5173` responded correctly
     afterward. That verification did **not** require the sudo commands
     below (they were run interactively by the user, not this session):
     ```
     sudo cp /home/nvidia/eye_compass_new/eye_compass_be/eye-compass-backend.service /etc/systemd/system/
     sudo systemctl daemon-reload
     sudo systemctl enable eye-compass-backend.service
     ```
     Enabled AND started (see the verification note above) — the
     systemd-managed instance bound port 8000 and served requests cleanly,
     with no conflict from the previously-manual terminal run. It will keep
     running across restarts/reboots on its own from here.
   - **Sub-todo: switch the kiosk to a production frontend build.** The
     kiosk script currently points at the Vite **dev** server (`:5173`, via
     Docker) on purpose, per an explicit decision to keep matching what's
     already been tested all session rather than switch to an untested
     production/Nginx target right now — see
     `10 - pwa_and_deployment_rollout.md`'s "Prerequisite: serve a real
     production build" section for what that switch actually involves.
   - **Sub-todo: test a real reboot cycle.** None of the above has been
     verified against an actual power-cycle yet (explicit decision this
     session to configure it now, reboot-test later) — need to confirm
     service startup ordering, the kiosk script's wait-and-retry actually
     works in practice, and recovery after a crash.
   - **The kiosk browser now locks down what's reachable from inside the
     Firefox window** (password manager, `about:` pages, devtools, extension
     installs — see `10 - pwa_and_deployment_rollout.md`'s Kiosk browser
     section) and relaunches itself if closed or crashed. **Sub-todo: the
     launch script's own process still isn't supervised** — it currently
     only starts via XDG autostart at graphical login, so if the script
     process itself dies (not just Firefox), nothing brings it back until
     the next login/reboot. Converting it to a systemd **user** service with
     `Restart=always` would close that gap; not done yet, flagged rather
     than done speculatively.

## Submit Batch is a two-step flow — a batch can be silently never saved

(Detail for item 1 above.) To match legacy exactly (explicitly requested),
Submit Batch is split into two separate steps, same as legacy:

1. **`POST /api/scan/submit`** (Submit Batch button) — stops the belt, computes
   the result/looker_data/datagram, writes `result.json`, shows the results
   page. Does **not** save to the database and does **not** sync to Qualix/
   Sheets. Matches `submit_create_result` (`main.py:1618-1661`).
2. **`POST /api/scan/confirm`** (results page's "Confirm & Finish" button) —
   the step that actually saves the row and queues the Qualix/Sheets sync.
   Matches `backtohome -> save_result` (`main.py:1717-1812`).

**The risk**: if the operator never reaches step 2 — navigates away, closes
the tab, the device loses power, gets distracted and walks off — the batch is
**never saved to the database or synced anywhere**, despite `result.json`
already sitting on disk and the operator having seen a completed results
screen. This is exactly how legacy behaves too, not a bug introduced here, but
it's a real operational risk worth the team deciding on deliberately rather
than inheriting silently:

- Leave it exactly as legacy (current state) — accept the risk to match
  behavior exactly.
- Add a safety net without changing the two-click UX: e.g. auto-confirm after
  a timeout on the results page, or a periodic sweep that finds `result.json`
  files with no matching DB row and saves/syncs them anyway.
- Go back to the single-step version (Submit both computes and persists,
  no separate confirm) — this was the behavior before this change and
  doesn't have the risk, at the cost of no longer matching legacy exactly.

## Other tracked findings (not yet in the priority list above)

### Confirm whether PO Number should really accept alphabets

New Batch's PO Number field currently takes plain free text, same as legacy —
checked `lineEdit_po_number` in both `.ui` files (`eye_compass_updated.ui:6196`,
`eye_compass_ui.ui:6275`) and grepped `main.py` for any `setValidator` call:
there is none anywhere in the file, and the widget has no input mask either,
so legacy genuinely lets the operator type letters (or anything else) into
PO Number, unlike Sorting Quantity which this port now restricts to digits
only — no decimal point either, since it was further tightened on request to
a whole positive number (`[1-9]\d*`, rejecting `0` and decimals; see
`enhancements.md`). This looks like it might just be an oversight in legacy
rather than an intentional design choice — a PO number is usually numeric in
practice — so **confirm with the team whether PO Number should be restricted
the same way Sorting Quantity was**, or left as free text matching legacy's
real (unvalidated) behavior.

### S3 upload paths (found while auditing "at what other points does it upload to S3?")

Legacy has three upload-related code paths, of which only two are real:

1. **`upload_videos_pool_id.py`** — a standalone CLI script meant to be
   triggered by the OS's actual crontab, not part of the running Qt app.
   Walks the output folders, waits for inference to be idle, retries
   transient failures, deletes local files after a successful upload by
   default.
2. **In-app background thread** (`main.py:3148`, `s3_upload.py`'s
   `s3Uploading` QThread) — started once at app boot and **does not loop**:
   `run()` walks the whole `output/` tree once and the thread ends, confirmed
   against a real startup log (the "All images of ... uploaded Successfully"
   line appears exactly once, never again for the rest of the session). This
   is the one already ported to `s3_worker.py`'s `S3UploaderTask` — which
   *does* loop, on `S3_UPLOAD_INTERVAL_SECONDS`, a deliberate improvement over
   legacy's one-shot-at-boot behavior; see `enhancements.md`.
3. **Login-triggered upload — does NOT actually exist.** `main.py:588, 612,
   638` print log lines like `"Starting the s3 upload thread in login."`,
   but there is no code near them that starts or restarts the thread.
   Misleading dead logging, not real behavior — nothing to port here.

**To look into later:** the cron script (#1) is external to the app and was
not ported into the backend. If it's still needed, it has to keep running as
its own separate scheduled process, and would need to be pointed at the new
`OUTPUT_DIR` (see the `.env` `OUTPUT_DIR` change in
`9 - post_remediation_session_log.md` §5) instead of the legacy
`/home/nvidia/eye_compass/` tree, or it will keep uploading from — and only
from — the old location.

### `S3UploaderTask`'s cycle cost grows without bound

`s3_worker.py`'s `_sync_directory` (see `S3UploaderTask._run_cycle`) has no
memory of what it already confirmed uploaded. Every cycle — by default every
`S3_UPLOAD_INTERVAL_SECONDS` (60s) — it walks the **entire** `output/` and
`output_frame/` trees again, and for **every file that has ever existed there**
makes an S3 request asking for its remote size, to decide whether to (re-)
upload it.

This means the per-cycle cost is proportional to the device's total scan
history, not to what changed since the last cycle. A device running for
months will eventually spend real time and S3 requests every 60 seconds just
re-confirming thousands of already-uploaded files are still there, before it
ever gets to anything new.

**Two independent things worth doing, not a single fix:**
1. **Widen `S3_UPLOAD_INTERVAL_SECONDS`** (e.g. to a few minutes) — a scan
   image is not urgent to back up the instant it is written, so this buys
   time without changing anything else. On its own this only slows down how
   often the growing cost is paid, it does not stop the cost from growing.
2. **Give the worker some memory of what it already uploaded** (a local flag
   per file, or at least skip the S3 size lookup once a file is known to have
   been handled), so a cycle only does real work on genuinely new files
   instead of re-checking the whole history every time. This is the one that
   actually fixes the unbounded growth; #1 alone does not.

### XAI View is broken in legacy — missing model file

Clicking "XAI View" on the live-scan screen silently does nothing in legacy.
Traced it:

- `show_xai_image` (`main.py:1006`) first tries the optimized/TensorRT path
  (`generate_xai_heatmap_optimized`), using the same model already loaded for
  live detection via `cam_thread.processing_thread.model_infer`. In practice
  this was `None` during testing even though detection was clearly running —
  not yet understood why.
- It then falls back to `test_xai.py`'s `image_preprocessing`, which loads a
  PyTorch YOLOv7 checkpoint from a hardcoded path,
  `/home/nvidia/eye_compass/xai_models/v6_best.pt` — at module import time,
  unconditionally, so this always fails the same way regardless of commodity.
- That file does not exist. Searched every local copy of the `eye_compass`
  folder on this dev unit (`eye_compass`, `eye_compass_legacy`,
  `eye_compass_new/eye_compass`, `eye_compass1`, `eye_compass_cop`,
  `eye_compass-prod-review`) and on a prod device — not present anywhere
  reachable so far.
- The failure is caught and only logged (`except Exception as e:
  logging_fxn(...)`), never shown to the operator — so from the UI it just
  looks like the button does nothing.

**To look into later**, in order of preference:
1. Find out why `cam_thread.processing_thread.model_infer` is `None` in this
   flow and fix that — this sidesteps the missing file entirely, since the
   optimized path never needs `v6_best.pt` at all.
2. Failing that, source the actual `v6_best.pt` weights from wherever the
   model was originally trained/stored (S3, an internal model registry, or
   whoever owns the ML side of this project) and place it at
   `xai_models/v6_best.pt` under the app's root.

Our own frontend's XAI View button is already wired to the backend's
`/api/xai/generate` endpoint (see `9 - post_remediation_session_log.md` §3),
so once either fix above lands in legacy's own model-loading path, the same
fix applies here without further frontend work.
