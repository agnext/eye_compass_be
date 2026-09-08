# 6. Migration Progress Log

A narrative summary of the modernization work, kept in roughly the order it
happened. For the authoritative, currently-accurate state of the system,
prefer `9 - post_remediation_session_log.md` (most recent) and
`8 - remediation_log.md` over the prose below where they disagree — this log
is kept for history, and an earlier version of it made a claim ("100%
migrated") that turned out to be false; see the correction below.

## 1. Architectural Overhaul & Containerization
- Transitioned the monolithic PyQt5 + SQLite legacy application into a
  client-server architecture: React (frontend) talking to FastAPI (backend).
- `docker-compose.yml` orchestrates the **frontend** (Node/Vite dev container)
  and **db** (PostgreSQL) — the backend deliberately does **not** run in
  Docker; see `5 - infrastructure_and_deployment.md` for exactly why.
- Development ergonomics: bind mounts plus `usePolling: true` in
  `vite.config.js` so hot-module-replacement works reliably even when the
  filesystem event notifications a container would normally rely on aren't
  available.

## 2. Database Migration & ORM
- Replaced the legacy SQLite file with PostgreSQL for better concurrency and
  crash-safety (see `5 - infrastructure_and_deployment.md` for the reasoning).
- `scripts/migrate_sqlite_to_postgres.py` migrates all seven legacy tables —
  not just `Result` — after significant bugs were found in an earlier version
  of the script (see `8 - remediation_log.md` §9). It has been verified to
  parse and migrate correctly against the live legacy database's actual data,
  but has **not** yet been run as a real cutover on a production device — see
  `todos.md`.
- `app/models/schema.py` defines SQLAlchemy models mapping to the legacy
  tables: `Creds`, `ClientInfo`, `SurveyorDetails`, `BrandDetails`,
  `VendorDetails`, `CommodityDetails`, `Result`, plus a new `BatchDetails`.

## 3. Backend API Development (FastAPI)
- Central FastAPI app in `app/main.py`, routers mounted per domain.
- `/api/auth` — login, validated against Qualix online-first with a legacy
  offline fallback.
- `/api/batch` — the 12-field batch-details form's persistence.
- Business logic lives in `app/services/` (camera, conveyor, inference, scan
  session, sync, S3), not in the route handlers themselves.

## 4. Frontend Modernization (React + Vite)
- Redux Toolkit + RTK Query for state and API data fetching/caching, split by
  domain (`authApi.js`, `batchApi.js`, `configApi.js`, `scanApi.js`, etc.).
- Pages rebuilt to match legacy's actual look and behavior, verified against
  real screenshots from a production device rather than from reading the Qt
  `.ui` file alone — see `9 - post_remediation_session_log.md` for the long
  list of concrete corrections this produced (branding text, field styling,
  button placement, header layout that changes depending on scan state, and
  more).
- Home's navigation removed the legacy WiFi/shutdown utility buttons that
  don't apply in a browser context — a deliberate, not accidental, omission.

## 5. Hardware, Inference & History
- `GrabImage.py` (camera capture) and `run_inference.py` (TensorRT inference)
  ported into `app/services/camera_service.py` / `inference_service.py`,
  streamed to the frontend over a WebSocket.
- Conveyor control ported to `app/api/conveyor.py` / `conveyor_service.py`,
  talking to the physical belt controller over `/dev/ttyTHS1` with the same
  plain-ASCII protocol, retry policy, and fail-safe as legacy.
- `GET /api/history` + `History.jsx` for past-result browsing;
  `ResultsViewer.jsx` for the per-scan breakdown and crop gallery.

## Status correction (4 September 2026)

An earlier revision of this document stated that "100% of the legacy
application's features are now successfully migrated". That was not accurate.
A file-level audit against the legacy tree found that the backend could not be
imported at all, several endpoints were unreachable at the URLs the frontend
used, and the core inspection loop had no implementation.

The specifics, and what was done about them, are in `8 - remediation_log.md`.

## Status correction (7 September 2026)

A second, much longer pass — conducted interactively against real hardware and
against real screenshots from a production device — found and fixed a further
substantial list of behavioral mismatches (see `9 -
post_remediation_session_log.md`): a completely unbuilt Data Collection
feature, several UI screens that didn't match legacy's actual layout, an
incorrect Forward-button mapping, a process-crashing threading bug in the
camera/inference pipeline, and the Submit flow not matching legacy's two-step
compute-then-confirm design, among others. Treat `9 -
post_remediation_session_log.md` and `todos.md` as the current state of the
migration; everything above describes the shape of the work, not a guarantee
of what's currently correct.
