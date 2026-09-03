from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import engine, Base
from app.models import schema
from app.api import scan, xai, camera, conveyor, auth, config, batch, history
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Automatically create all tables defined in schema.py
try:
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified.")
except Exception as e:
    logger.warning(f"Could not connect to the database on startup. Submissions will fail, but the UI/Login can still be tested: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for hardware services."""
    logger.info("Eye Compass API starting up...")
    yield
    # Cleanup camera on shutdown
    from app.api.camera import _camera, _inference
    if _camera is not None:
        _camera.stop_grabbing()
        _camera.close()
    if _inference is not None:
        _inference.cleanup()
    logger.info("Eye Compass API shut down cleanly")


app = FastAPI(title="Eye Compass API", version="1.0.0", lifespan=lifespan)

# Setup CORS to allow the React Frontend to communicate
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict to localhost / specific IPs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
app.include_router(scan.router, prefix="/api/scan", tags=["Scan"])
app.include_router(xai.router, prefix="/api/xai", tags=["XAI"])
app.include_router(camera.router, prefix="/api/camera", tags=["Camera"])
app.include_router(conveyor.router, prefix="/api/conveyor", tags=["Conveyor"])
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(config.router, prefix="/api/config", tags=["Config"])
app.include_router(batch.router, prefix="/api/batch", tags=["Batch"])
app.include_router(history.router, prefix="/api/history", tags=["History"])


@app.get("/")
def read_root():
    return {"status": "Eye Compass API is running."}
