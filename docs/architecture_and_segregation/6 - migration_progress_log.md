# Migration Progress Log

This document serves as a comprehensive summary of all the modernization, segregation, and development work completed to migrate the legacy monolithic `Eye Compass` application into a decoupled Full-Stack Web Application.

## 1. Architectural Overhaul & Containerization
- **Decoupling the Monolith:** Successfully transitioned the monolithic `PyQt5` + `SQLite` legacy application (`eye_compass_ui.py`, `main.py`) into a modern client-server architecture using **React** (Frontend) and **FastAPI** (Backend).
- **Docker Orchestration:** Implemented a robust `docker-compose.yml` to orchestrate the entire stack:
  - `frontend`: Node.js Alpine container serving the Vite React App.
  - `backend`: Python FastAPI container handling the business logic.
  - `db`: A PostgreSQL container serving as the primary relational database.
- **Development Ergonomics:** Configured Docker bind mounts and explicitly enabled filesystem polling (`usePolling: true` in `vite.config.js`) to allow for seamless hot-module-replacement (HMR) development directly on the Windows host machine without needing to rebuild the Docker images.

## 2. Database Migration & ORM
- **PostgreSQL Transition:** Replaced the local `sqlite.db` with a robust **PostgreSQL** instance to better support concurrent operations and web-based scalability.
- **Data Migration Scripts:** Developed a secure migration script (`migrate_sqlite_to_postgres.py`) to systematically extract legacy data (Users, Configurations, Results) from the SQLite file and inject it into the new Postgres schema.
- **SQLAlchemy ORM:** Defined structured Python data models in `app/models/schema.py` mapping exactly to the legacy database tables (`Users`, `Configuration`, `Results`, `BatchDetails`).

## 3. Backend API Development (FastAPI)
- **API Routing:** Structured the backend modularly by defining a central FastAPI router in `app/main.py`.
- **Authentication (`/api/auth`):** Created secure login endpoints validating user credentials securely against the Postgres `users` table.
- **Batch Management (`/api/batch`):** Created endpoints for receiving and persisting detailed 12-field batch metadata.
- **Services:** Abstracted business logic into dedicated service files (e.g., `app/services/sync_service.py` to synchronize configuration states).

## 4. Frontend Modernization (React + Vite)
- **State Management (Redux Toolkit):** Replaced legacy desktop state with a robust global state store (`src/store/index.js`), using **RTK Query** for efficient API data fetching and caching (`authApi.js`, `batchApi.js`, `configApi.js`).
- **UI & UX:** Built responsive React components that faithfully replicate the functionality and look-and-feel of the legacy PyQt kiosk application:
  - **Login (`Login.jsx`):** Secure authentication screen.
  - **Home Hub (`Home.jsx`):** Clean navigation dashboard with "New Batch", "History", and "Data Collection" entry points. Removed legacy utility buttons (WiFi/Shutdown) that are no longer relevant in the web context.
  - **New Batch Details (`NewBatch.jsx`):** Meticulously recreated the legacy 12-field input form, matching the exact 3-column grid layout, light-grey paneling, and custom button coloring (Red Cancel, Green Start).
  - **Data Collection (`DetailsEntry.jsx`):** Reconstructed the metadata entry screen and conveyor control panel.
  - **Dashboard/Camera View (`Dashboard.jsx`):** Scaffolded the main real-time visual inspection interface.
- **Branding:** Replaced placeholder framework icons (Vite/React) with the official Eye Compass logo (`cp2.png`) for the PWA manifest and browser tab favicon.

## 5. Final Legacy Features (Hardware, Inference, & History)
- **Computer Vision & Inference:** Ported `GrabImage.py` (camera capture) and `run_inference.py` (YOLO/ONNX) into the FastAPI background services (`app/api/camera.py`). Real-time WebSockets stream the annotated frames directly to the React Dashboard.
- **Hardware Integration:** Migrated the serial port controls (`serial_port.py`) to an API (`app/api/conveyor.py`), enabling the web interface to physically start and stop the conveyor via `/dev/ttyUSB0`.
- **History & Results:** Built the `GET /api/history` endpoint to dynamically query PostgreSQL `Results` and populated the React `History.jsx` table. Added seamless navigation to view the detailed heatmaps (`ResultsViewer.jsx`) for past scans.

## Status correction (4 September 2026)

An earlier revision of this document stated that "100% of the legacy
application's features are now successfully migrated". That was not accurate.
A file-level audit against the legacy tree found that the backend could not be
imported at all, several endpoints were unreachable at the URLs the frontend
used, and the core inspection loop had no implementation.

The specifics, and what was done about them, are in
`8 - remediation_log.md`. Treat that document as the current state of the
migration; the sections above describe intent, not completed behaviour.

## What's Next? (QA & Physical Deployment)
The remaining work is genuine hardware QA, which cannot be done off-device:

1. **Physical Hardware QA:** Run the backend on the Jetson with
   `USE_MOCK_CAMERA=false` and confirm the MVS SDK binds, the calibration file
   loads, and the conveyor acknowledges commands over `/dev/ttyUSB0`.
2. **End-to-End Testing:** Put a physical sample through the machine and verify
   the whole pipeline: Login -> New Batch -> Start -> detection stops the belt ->
   operator classifies the object -> Resume -> Submit -> Qualix sync.
3. **Kiosk Mode Setup:** Configure the device to boot into a full-screen browser
   pointing at the frontend.
