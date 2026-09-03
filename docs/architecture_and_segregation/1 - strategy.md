# Eye Compass: Architecture & Segregation Strategy

This document outlines the high-level strategy for segregating the Eye Compass monolithic application into independent frontend, backend, and database components.

## Current Monolithic State
- **Frontend & Backend:** Bundled together in Python using PyQt5 (`main.py`, `eye_compass_ui.py`).
- **Hardware:** Runs on NVIDIA Jetson (aarch64) with a physical industrial camera using the Hikvision MVS SDK (`GrabImage.py`).
- **Machine Learning:** YOLOv7 inference model (`agnext_opti`) running locally for tracking.
- **Database:** SQLite (`database.py`) bundled in the same application.
- **Data Sync:** Syncs data to Qualix via REST APIs (`api_handle.py`).

## Target Microservices Architecture

### 1. Frontend: Progressive Web App (PWA)
- **Tech Stack:** React (or Next.js/Vite) converted to a PWA.
- **Role:** Replaces PyQt. It will render the UI and run in a Kiosk mode browser on the Jetson device.
- **Hardware Integration:** Connects to the local Python backend via WebSockets to receive the real-time MJPEG camera stream and tracking results.
- **Location:** `eye_compass_fe` repository.

### 2. Backend: Python API (FastAPI) & AI Engine
- **Tech Stack:** FastAPI (async Python web framework).
- **Role:** Handles all hardware interaction, ML inference, database operations, and data syncing.
- **Responsibilities:**
  - Initialize and stream the Hikvision camera feed.
  - Run the YOLOv7 ML models on the edge.
  - Serve REST API endpoints for the frontend (login, start/stop tracking, results).
  - Manage the existing Qualix data sync (via background workers/tasks).
  - **AWS S3 Integration:** Handle the background uploading of output images/data to the `agnext-cognito` S3 bucket using Cognito Identity credentials (previously done via `QThread` in `s3_upload.py`).
- **Data Routing to New Apps:** Implement an asynchronous publisher (e.g., webhooks, Redis Pub/Sub, or RabbitMQ) to dispatch data to two additional external applications concurrently without blocking the main event loop.
- **Location:** `eye_compass` repository (refactored).

### 3. Database: PostgreSQL (Local Edge DB)
- **Tech Stack:** PostgreSQL + SQLAlchemy ORM.
- **Role:** Replaces SQLite for better concurrency and reliability.
- **Why PostgreSQL over SQLite?**
  - **Concurrency:** FastAPI's `BackgroundTasks` will asynchronously sync data to Google Sheets, S3, and Qualix. PostgreSQL handles concurrent reads/writes natively, preventing the "database is locked" exceptions common in SQLite under multi-threaded load.
  - **Power-Loss Robustness:** PostgreSQL's Write-Ahead Logging (WAL) heavily mitigates the risk of data corruption if the physical Jetson device experiences a hard power-off during operation.
  - **JSONB Support:** Native `JSONB` data types allow for clean storage and direct querying of the complex ML `analysis` payload objects, avoiding stringified-JSON hacks.
- **Deployment:** Runs locally on the NVIDIA Jetson device (ARM64 compatible).

### 4. Authentication & Identity: Keycloak
- **Role:** Unified Single Sign-On (SSO) login.
- **Integration:** The PWA will authenticate users against Keycloak, and the FastAPI backend will validate the OIDC tokens.
- **Session Expiry:** Configured in Keycloak realm settings to increase the refresh token / SSO session max lifespan to **1.5 months (45 days)**.

---
*Note: This strategy ensures that all edge-dependent operations (camera, ML) remain local to the hardware to avoid latency, while modernizing the stack for better maintainability and scalability.*
