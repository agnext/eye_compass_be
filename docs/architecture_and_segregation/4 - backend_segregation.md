# 4. Backend Segregation (FastAPI)

The backend (`eye_compass_be`) is where every piece of legacy `main.py` /
`GrabImage.py` / `run_inference.py` / `sheet_update.py` / `s3_upload.py`
business logic ended up — extracted from one PyQt5 process into a modular
FastAPI application that the frontend talks to over HTTP and one WebSocket.

## Directory Structure
- `app/api/` — REST routers and WebSocket endpoints, grouped by domain
  (`auth`, `batch`, `camera`, `conveyor`, `config`, `history`, `scan`, `xai`).
- `app/core/` — settings (`config.py`, with a fallback chain: real environment
  → `.env` → legacy `config.INI` → hardcoded legacy defaults) and the database
  engine (`database.py`, supports both SQLite for quick local dev and
  PostgreSQL for real use).
- `app/models/` — SQLAlchemy schema (`schema.py`) — the seven legacy tables,
  reconstructed table-for-table.
- `app/services/` — the actual business logic and hardware/external-service
  clients: camera, conveyor, inference, the scan state machine, Qualix/Sheets
  sync, S3 upload, session/auth storage.

## Key Modules

1. **Camera streaming (`app/api/camera.py`)** — wraps the Hikvision MVS SDK
   (`app/services/camera_service.py`) and TensorRT inference
   (`app/services/inference_service.py`) behind a WebSocket at
   `ws://<host>/ws/camera/stream` (mounted at the application root, not under
   `/api/camera`, so its URL is stable regardless of how the REST routes are
   prefixed). A second WebSocket, `/ws/data_collection/stream`, serves the
   separate raw-frame-capture page. All camera/inference calls — REST and
   WebSocket alike — are funneled through one dedicated worker thread; see
   `9 - post_remediation_session_log.md` for why that is load-bearing (a
   process-crashing CUDA context bug otherwise) and not just a style choice.

2. **Conveyor control (`app/api/conveyor.py`, `app/services/conveyor_service.py`)**
   — a small plain-ASCII serial protocol over `/dev/ttyTHS1` (**not** Modbus,
   and not the same port as the separate, unrelated Modbus VFD described in
   `2b - dependencies_and_hardware.md`). Five commands exist:
   `machine_start`, `all_stop`, `FM_detected`, `camera_on`, `camera_off`, each
   with its own expected acknowledgment string, three retries, and a fail-safe
   `all_stop` if nothing answers. Exposed as REST endpoints
   (`POST /api/conveyor/command`, `/unlock`, `/forward`, `GET /status`), plus
   a second, page-specific `/api/scan/forward` for the live-scan page's own
   Forward button, which does something meaningfully different from the
   Data Collection page's Forward — see `9 -
   post_remediation_session_log.md`.

3. **Scan lifecycle & inference (`app/api/scan.py`,
   `app/services/scan_session.py`, `app/services/inference_service.py`)** —
   the detect → stop-belt → lock-interlock → operator-classifies →
   resume/submit state machine that was the single largest piece of legacy
   business logic to port; see `8 - remediation_log.md` §3 for what was
   missing and `9 - post_remediation_session_log.md` for the two-step
   submit/confirm split added afterward to match legacy exactly.

4. **XAI (`app/api/xai.py`, `app/services/xai_service.py`)** — generates an
   explainability heatmap for the current/a past frame. Wired up on the
   frontend during this session; the underlying legacy feature itself is
   broken (missing model file) — see `todos.md`.

5. **Authentication (`app/api/auth.py`, `app/core/security.py`)** — online-first
   against Qualix, offline fallback against a locally cached, hashed
   credentials table, matching legacy exactly. Issues a real bearer token
   (`SessionStore`, 45-day TTL) rather than the legacy code's literal
   `"dummy_offline_token"` string.

6. **Config sync (`app/services/sync_service.py`, `app/api/config.py`)** — on
   login, fetches and caches commodities/varieties/vendors/brands/surveyors/
   client info from Qualix into Postgres, transactionally.

7. **Data Collection (`app/api/camera.py`'s `data_collection/*` routes)** — the
   raw frame-dump tool: start/stop/capture, writing every grabbed frame to
   disk untouched, no inference involved. Built from scratch in this session
   to match a legacy page that had no equivalent anywhere in the original
   segregation — see `9 - post_remediation_session_log.md`.

## Database

- SQLAlchemy ORM models in `app/models/schema.py`, mapping to the legacy
  tables: `Creds`, `ClientInfo`, `SurveyorDetails`, `BrandDetails`,
  `VendorDetails`, `CommodityDetails`, `Result`, plus a new `BatchDetails`
  table (legacy never gave the batch-details form its own table; the row was
  written and its id discarded — see `8 - remediation_log.md` §7).
- Columns that legacy stored as `str(dict)`/`str(list)` (`analysis`, `variety`,
  `result`) use `JSON().with_variant(JSONB(), "postgresql")`, so Postgres can
  query them structurally while SQLite (used for quick local dev) still
  works.
- `scripts/migrate_sqlite_to_postgres.py` migrates the legacy SQLite database
  (all seven tables, all historical rows) into Postgres — see `8 -
  remediation_log.md` §9 for the bugs found and fixed in this script, and
  `todos.md` for why it still needs a real end-to-end test run.
