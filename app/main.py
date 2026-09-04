import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, batch, camera, config, conveyor, history, scan, xai
from app.core.config import settings
from app.core.database import Base, engine
from app.models import schema  # noqa: F401  — registers the tables on Base.metadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle.

    Mirrors what legacy main.py did at boot: create the schema, put the conveyor
    into a known-safe state, and start the background workers
    (start_background_threads, main.py:3112-3124).
    """
    logger.info("Eye Compass API starting up...")

    # 1. Schema. A failure here is fatal — the legacy app could not run without
    #    its database either, and starting anyway just moves the error to every
    #    subsequent request where it is much harder to diagnose.
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables verified.")
    except Exception as exc:
        logger.error(
            "FATAL: could not reach the database at %s — %s",
            engine.url.render_as_string(hide_password=True),
            exc,
        )
        raise

    # 2. Safety: the machine must never come up with the belt running.
    #    Legacy did this at main.py:505.
    if not settings.USE_MOCK_CAMERA:
        try:
            from app.services.conveyor_service import conveyor_service

            conveyor_service.send("all_stop")
            logger.info("Startup all_stop sent to conveyor.")
        except Exception as exc:
            logger.warning("Startup all_stop failed: %s", exc)

    # 3. Background workers.
    tasks = []

    if settings.SYNC_WORKER_ENABLED:
        from app.services.sync_worker import sync_retry_worker

        tasks.append(asyncio.create_task(sync_retry_worker(), name="sync-retry"))
        logger.info(
            "Unsynced-result retry worker started (every %s min).",
            settings.SYNC_RETRY_INTERVAL_MINUTES,
        )

    s3_task = None
    if settings.S3_ENABLED:
        from app.services.s3_worker import S3UploaderTask

        s3_task = S3UploaderTask()
        tasks.append(asyncio.create_task(s3_task.start(), name="s3-upload"))
        logger.info("S3 background uploader started.")

    app.state.background_tasks = tasks

    try:
        yield
    finally:
        logger.info("Eye Compass API shutting down...")

        if s3_task is not None:
            s3_task.stop()

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Stop the belt before we lose the ability to talk to it.
        if not settings.USE_MOCK_CAMERA:
            try:
                from app.services.conveyor_service import conveyor_service

                conveyor_service.send("all_stop")
            except Exception as exc:
                logger.warning("Shutdown all_stop failed: %s", exc)

        from app.api.camera import shutdown_camera_services

        shutdown_camera_services()

        logger.info("Eye Compass API shut down cleanly")


app = FastAPI(title="Eye Compass API", version="1.0.0", lifespan=lifespan)

# This service drives physical hardware, so the origin list is explicit rather
# than "*". Configure extra origins via CORS_ORIGINS in .env.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scan.router, prefix="/api/scan", tags=["Scan"])
app.include_router(xai.router, prefix="/api/xai", tags=["XAI"])
app.include_router(camera.router, prefix="/api/camera", tags=["Camera"])
app.include_router(conveyor.router, prefix="/api/conveyor", tags=["Conveyor"])
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(config.router, prefix="/api/config", tags=["Config"])
app.include_router(batch.router, prefix="/api/batch", tags=["Batch"])
app.include_router(history.router, prefix="/api/history", tags=["History"])

# The camera WebSocket is mounted at the app level, not under /api/camera, so the
# client URL is the plain ws://host/ws/camera/stream.
app.include_router(camera.ws_router, tags=["Camera"])


@app.get("/")
def read_root():
    return {"status": "Eye Compass API is running."}


@app.get("/api/health")
def health():
    """Liveness plus the few facts worth knowing before a shift starts."""
    from app.api.camera import camera_health

    return {
        "status": "ok",
        "mock_mode": settings.USE_MOCK_CAMERA,
        "camera": camera_health(),
    }
