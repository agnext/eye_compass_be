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
   boot.** The PWA manifest/service worker exist in the build
   (`vite-plugin-pwa`) but have never been tested as an actually-installed
   app, and nothing yet configures the device to launch it automatically at
   power-on. See `10 - pwa_and_deployment_rollout.md` for the intended
   mechanism (production build, kiosk-mode Chromium, autostart) — none of
   which has been tried on real hardware yet.

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
(plus one decimal point) on request. This looks like it might just be an
oversight in legacy rather than an intentional design choice — a PO number is
usually numeric in practice — so **confirm with the team whether PO Number
should be restricted the same way Sorting Quantity was**, or left as free
text matching legacy's real (unvalidated) behavior.

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
