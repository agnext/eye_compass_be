# 2. Backend Segregation (FastAPI)

The backend (`eye_compass_be`) was extracted from the monolithic `main.py` and modularized into a clean FastAPI application.

## Directory Structure
- `app/api/`: Contains the REST routers for different domains.
- `app/core/`: Database connections and configuration loading.
- `app/models/`: SQLAlchemy database schemas.
- `app/services/`: Background processing and external API communications.

## Key Modules
1. **Camera Stream (`app/api/camera.py`)**: 
   - Converted the legacy `GrabImage.py` threads into a WebSocket endpoint `/api/camera/ws`. 
   - This allows the React frontend to receive real-time, low-latency JPEG frames directly from the hardware camera.
2. **Conveyor Control (`app/api/conveyor.py`)**: 
   - Modbus TCP commands (Forward, Reverse, Stop) were exposed via REST POST endpoints.
3. **Inference / XAI (`app/api/scan.py` & `app/api/xai.py`)**: 
   - Extracted the TensorRT model execution logic. 
   - The UI now triggers an analysis via API, and the backend handles image cropping, model execution, and heatmap generation in the background.
4. **Authentication (`app/api/auth.py`)**: 
   - Built to support "Offline-First" login. Uses `.env` credentials (`cgi.op3`) to allow operators to log in even when the Jetson loses internet connectivity.
5. **Config Syncing (`app/services/sync_service.py` & `app/api/config.py`)**: 
   - A background task that mimics the legacy system: upon successful login, it silently connects to the Qualix API in the background, fetches the latest crops, varieties, and Foreign Matter configurations, and caches them in PostgreSQL.

## Database Migration
- Converted SQLite `appDB` queries to SQLAlchemy ORM models (`app/models/schema.py`).
- Updated columns like `analysis` and `variety` to `JSONB` to properly leverage PostgreSQL's structured data types instead of stringified dictionaries.
- Created `migrate_sqlite_to_postgres.py` to allow historical data retention from the old system.
