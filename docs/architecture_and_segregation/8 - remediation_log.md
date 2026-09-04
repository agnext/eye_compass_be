# 8. Remediation Log — 4 September 2026

A file-level audit compared the legacy `eye_compass` monolith against
`eye_compass_be` + `eye_compass_fe`. This records what was wrong and what was
changed. Legacy line references are to the tree as it stood at the time.

## 1. The backend could not start

| Problem | Fix |
|---|---|
| `app/models/` did not exist, but seven modules imported `app.models.schema` | Package reconstructed as `app/models/schema.py` with all seven legacy tables |
| `.gitignore` had a bare `models/` line, which git matched against `app/models/` — this is why the package was never committed | Changed to `/models/` and `/ml_m/` so it only matches the repo root |
| `app/`, `app/core/`, `app/services/` had no `__init__.py` | Added |
| No interpreter had both the ML stack and the web stack | The web stack was installed into `/home/nvidia/.virtualenvs/eye_compass`, which already had tensorrt/pycuda/torch/cv2. The systemd unit now points at it |
| `requirements.txt` omitted numpy, torch, tensorrt, pycuda | Rewritten, with the JetPack-provided packages documented rather than pip-installed |
| A DB connection failure was swallowed at startup | Now fatal, so it surfaces at boot instead of on every request |
| Stray `eye_compass_backend.db` (SQLite) in the repo | Removed; its DDL was used to reconstruct the schema first |

## 2. Endpoints the frontend called did not exist

Routers were mounted under a prefix while their decorators also carried the
full path, so `/api/conveyor/command` was served at
`/api/conveyor/api/conveyor/command`. The camera WebSocket, the model switch
and every conveyor command were 404. Route paths are now relative, and the
WebSocket is mounted app-level so its URL stays `ws://host/ws/camera/stream`.

The XAI endpoint expected multipart and returned JPEG bytes while the client
sent JSON and read `heatmap_base64`; the endpoint now speaks JSON both ways.

## 3. The inspection loop was missing

This was the largest gap: the legacy detect → stop → classify → count sequence
had no implementation. `app/services/scan_session.py` now holds it, since HTTP
requests cannot carry the state the legacy in-process object did.

- **Cumulative counting.** The stream loop rebuilt its count dictionary on every
  frame from currently-tracked objects, so the figure sent to Qualix was a
  snapshot, not a run total. Unique track ids now accumulate for the whole run
  (`handle_detection`, main.py:2566-2591).
- **Duplicate suppression.** `has_similar_x_axis` (main.py:2516-2563) ported, so
  one physical object is not queued twice.
- **Foreign-matter interlock.** On detection the belt is stopped and
  `machine_start` is locked *before* `FM_detected` is sent, matching the legacy
  ordering that avoids the restart race (main.py:2621-2623). The operator could
  previously restart the belt with unresolved foreign matter under the camera.
- **Operator classification.** The frozen frame's boxes are now tappable in the
  React dashboard; each tap crops and saves `<FM_name>_<timestamp>.png`
  (`crop_and_save` / `submit_fm_type`). `FMSelect.jsx` and `ManualEntry.jsx`
  were static stubs with no handlers and have been removed — the interaction
  belongs on the live frame, as it did in the legacy UI.
- **File-based results.** `create_results` counts saved crops by filename prefix
  (main.py:1372-1452), and `update_fm_count` produces the six `looker_data`
  metrics from real stop-time accounting (main.py:1535-1563).
- **Frame persistence.** `r_frame_N.jpg` at quality 95, which feeds both the
  "Frame Count" metric and the S3 upload.

## 4. Inference correctness

- `MODEL_MAP` pointed at `ml_m/*.engine`; legacy loads `models/*.optimized` via
  `ModelInfer` (an alias of `TensorRTInference`, run_inference.py:603). The
  registry is now a transcription of legacy `model_paths` (GrabImage.py:221-247).
- Five commodities added on 15 June 2026 were missing entirely, and several
  models and thresholds disagreed with `get_model_infer`. `resolve_model` is now
  a branch-for-branch port.
- The per-commodity confidence threshold was resolved and then discarded —
  `predict()` had a default of 0.2 that always won — so every commodity inferred
  at 0.2. It is now passed through.
- Commodity names are normalised with `.replace(" ", "_").lower()` (main.py:818),
  without which any multi-word commodity fell through to the rice fallback.
- `process_results` (GrabImage.py:445-536) and `enlarge_bbox(pad=10)` ported.
- An unmapped commodity still falls back to stem_rice as legacy did, but now
  logs a warning instead of doing it silently.

## 5. Camera acquisition

- **Two `cv2.flip` calls were active** in `camera_service.py` that are commented
  out in `GrabImage.py:63-64`. Together they rotated every frame 180°, reversing
  the belt's direction of travel in image space and breaking the tracker's
  entry/exit assumptions. Removed.
- `MV_CC_FeatureLoad(FeatureFile_new.ini)` restored — without it the camera runs
  on factory white balance and frame rate.
- GigE optimal packet-size negotiation (`GevSCPSPacketSize`) restored.
- Exposure/gain corrected to the legacy 600.0 / 0.0. The `[CAMERA] runtime_*`
  keys in config.INI are read by no legacy code path and are not used.
- BayerRG8 pixel-format guard restored; SDK error codes are now logged; a failed
  `initialize()` releases the handle instead of leaking it.
- Frame decimation (every 2nd frame) restored, and grab+infer now run under one
  lock so concurrent clients cannot interleave on the CUDA context.

## 6. Conveyor safety

`send_control_command` (main.py:2279-2378) is ported in full: the acknowledgment
map, three retries, and the fail-safe `all_stop` when no ACK arrives. The
previous endpoint wrote once, had the ACK read left in as a comment, returned
success regardless, and accepted any string from the request body. Commands are
now whitelisted, a missing serial port is an HTTP error rather than
`200 {"success": false}`, and `all_stop` is sent at startup and shutdown.

## 7. Data pipeline

- The Qualix POST never ran: it required credentials in the request body that
  the frontend never sent. The Qualix session is now held server-side from
  login, so no credential round-trips through the browser.
- The datagram carried 3 of 21 `scan_data` fields. `app/services/datagram.py`
  rebuilds all of them server-side from the persisted batch row plus the
  session, including `uuid`, `variety_id`, `device_id`, `weight_unit`, both
  process times in `dd/MM/yyyy HH:MM:SS`, the `looker_data` block,
  `total_fo_detected`, and `analysisType: "ICOMPASS"`.
- Batch metadata was written to an orphan row whose id was discarded; it is now
  carried into the scan and into the payload.
- `mark_as_synced` fired when *Google Sheets* succeeded, masking Qualix
  failures. Sync status is now driven by the Qualix response alone and keeps all
  three legacy states (`1` accepted, `2` rejected, `0` pending).
- The 15-minute retry worker (main.py:2826-2966) did not exist —
  `get_unsynced_results()` was defined and never called. It now runs from the
  app lifespan, with a manual `POST /api/history/{id}/resync` alongside it.
- Request-scoped DB sessions were handed to background tasks after FastAPI had
  closed them. Every background path now opens its own `SessionLocal()`.
- The Sheets duplicate guard (`check_start_time_exists`) was ported, without
  which the retry worker appends the same row on every attempt.
- The S3 worker was never started, read `config.INI` from the wrong directory,
  and had `"<REDACTED>"` as its Cognito pool id. It now starts from the lifespan
  and reads `settings`.

## 8. Configuration and identity

- The login-time config sync stored only commodities. Surveyors, vendors,
  brands and client info are now stored too, which is what the batch form's
  dropdowns and the S3 key prefix depend on. The sync is transactional — it
  previously deleted the commodity table before knowing the fetch had succeeded.
- Login compared against one hardcoded env credential pair and returned the
  literal string `"dummy_offline_token"`. It is now online-first against Qualix
  with the legacy offline fallback via the `creds` table, and issues a real
  bearer token.
- No endpoint required authentication and no frontend route was guarded. Both
  are fixed; CORS is an explicit origin list rather than `*`.
- `load_dotenv(override=True)` let a stale `USE_MOCK_CAMERA=true` in a
  developer's `.env` beat the systemd setting. Now `override=False`.
- `app/core/config.py` covers the config.INI keys the legacy system reads, and
  falls back to config.INI itself when a value is not in the environment.

## 9. Migration script

Verified against the live legacy database (79 results, 7 tables):

- Blobs are Python reprs, not JSON. `json.loads` failed on **all 79** rows and
  the fallback stored `{"raw_string": ...}`, destroying every historical record.
  Now `ast.literal_eval`, and all 79 parse.
- Deduplication was on `sample_id`, which is not unique — only 10 distinct
  values exist, so 69 rows would have been dropped. Now keyed on the legacy
  4-tuple `(sample_id, date, start_time, stop_time)`.
- Only `result` was migrated; all seven tables now are.
- `sys.path` pointed at `scripts/` rather than the backend root, so the script
  could not import `app.*` at all. Fixed, along with the legacy DB path.
- `sync_status` `'2'` is preserved (11 rows in the live data).
- Added `--dry-run` and a summary table.

## 10. Frontend

- The API base was hardcoded to `http://127.0.0.1:8000` in all eight slices;
  it now comes from `VITE_API_BASE`, defaulting to the serving host.
- Session token stored and sent on every request; a 401 clears it.
- Route guards added; the logout buttons now actually log out.
- Batch form uses server-populated dropdowns for commodity, variety, vendor,
  brand and sorter, with `vendor_code` auto-fill, and validates the batch number
  as legacy did.
- Dashboard implements the FM classification overlay, reconnects the WebSocket
  on drop, and shows errors on screen rather than in the console.
- History shows the legacy column set, pages properly, and offers re-sync.
- Results viewer restores the Item/Count breakdown and the crop gallery.
- `nginx.conf` added so deep links do not 404 on refresh in kiosk mode.

## What is verified, and what is not

Verified on this machine, against a real interpreter and the live legacy
database: module imports, the full route table, login/auth, batch creation and
validation, scan start/label/resume/submit, the FM interlock (including that
`machine_start` is refused while locked), duplicate suppression, suppression
rules, file-based counting, the six looker metrics, the complete 21-field
datagram, the WebSocket stream, the retry worker, and the migration of all 79
legacy records with `sync_status` preserved.

**Not verified — requires the physical machine:** the MVS SDK path, camera
calibration loading, GigE packet negotiation, TensorRT engine loading and
detection quality, and the serial ACK handshake with the real conveyor
controller. The audit ran with `USE_MOCK_CAMERA=true` and a SQLite database,
because Docker was not accessible in that session.

The `.optimized` model files for `stem_rice`, `toor`, `masoor_dal` and
`chitra_rajma` are absent from `eye_compass/models/` on this device. That is a
pre-existing gap in the legacy tree, not something introduced here, but
`stem_rice` is the fallback model, so it should be restored before the device
runs an unmapped commodity.
