"""
Camera streaming API.

Routes:
    WS   /ws/camera/stream    (mounted app-level via ws_router)
    GET  /api/camera/status
    POST /api/camera/model
    POST /api/camera/start    begin acquisition
    POST /api/camera/stop     end acquisition

Route paths on `router` are relative because app/main.py mounts it under
/api/camera. The WebSocket lives on a separate router mounted at the app root
so its public URL stays ws://host/ws/camera/stream.
"""

import asyncio
import base64
import logging
import threading
import time

import cv2
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.config import settings
from app.services.camera_service import BaseCameraService, get_camera_service
from app.services.inference_service import BaseInferenceService, get_inference_service
from app.services.scan_session import scan_session

logger = logging.getLogger(__name__)

router = APIRouter()
ws_router = APIRouter()

# ---------------------------------------------------------------------------
# Services. The camera and the TensorRT context are single physical resources,
# so they are guarded by a lock — several browser tabs must not interleave
# grabs or run inference concurrently on one CUDA context.
# ---------------------------------------------------------------------------

_camera: BaseCameraService | None = None
_inference: BaseInferenceService | None = None
_camera_initialized = False
_hardware_lock = threading.Lock()
_stream_clients = 0
_clients_lock = threading.Lock()


def _ensure_services() -> bool:
    global _camera, _inference, _camera_initialized

    with _hardware_lock:
        if _camera is None:
            _camera = get_camera_service()
        if _inference is None:
            _inference = get_inference_service()

        if not _camera_initialized:
            if _camera.initialize() and _camera.start_grabbing():
                _camera_initialized = True
                logger.info("Camera initialized and grabbing")
            else:
                logger.error("Camera initialization FAILED")
                return False
        return True


def shutdown_camera_services():
    """Called from the app lifespan on shutdown."""
    global _camera, _inference, _camera_initialized
    with _hardware_lock:
        if _camera is not None:
            try:
                _camera.stop_grabbing()
                _camera.close()
            except Exception as exc:
                logger.warning("Camera shutdown error: %s", exc)
        if _inference is not None:
            try:
                _inference.cleanup()
            except Exception as exc:
                logger.warning("Inference shutdown error: %s", exc)
        _camera = None
        _inference = None
        _camera_initialized = False


def camera_health() -> dict:
    return {
        "initialized": _camera_initialized,
        "model_loaded": bool(_inference and _inference.is_loaded),
        "stream_clients": _stream_clients,
    }


# ---------------------------------------------------------------------------
# WebSocket stream
# ---------------------------------------------------------------------------

@ws_router.websocket("/ws/camera/stream")
async def camera_stream(websocket: WebSocket):
    """Stream annotated JPEG frames plus the live scan state.

    Message shape:
        {
          "frame": "<base64 jpeg>",
          "detections": [[x1,y1,x2,y2,conf,cls], ...],
          "fm_detected": bool,          # belt has just been stopped
          "pending": [ {index, box, confidence, class_id}, ... ],
          "awaiting_label": [int, ...],
          "total_fo_detected": int,     # unique objects for the whole run
          "machine_start_locked": bool,
          "fps": float
        }
    """
    global _stream_clients

    await websocket.accept()
    logger.info("WebSocket client connected to /ws/camera/stream")

    if not _ensure_services():
        await websocket.send_json({"error": "Camera not available"})
        await websocket.close()
        return

    if not _inference.is_loaded:
        # Detection silently disabled is worse than a visible warning.
        await websocket.send_json({
            "warning": "No inference model loaded. Call POST /api/camera/model "
                       "with the commodity and variety before starting a scan."
        })

    with _clients_lock:
        _stream_clients += 1

    frame_interval = 1.0 / max(1, settings.STREAM_FPS)
    decimation = max(1, settings.CAMERA_FRAME_DECIMATION)
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.STREAM_JPEG_QUALITY]

    fps_counter = 0
    fps_timer = time.time()
    current_fps = 0.0
    raw_frame_index = 0
    loop = asyncio.get_running_loop()

    try:
        while True:
            loop_start = time.time()

            def grab_and_infer():
                # One lock for the whole grab+infer step: the camera handle and
                # the TensorRT context are both single-owner resources.
                with _hardware_lock:
                    frame = _camera.grab_frame()
                    if frame is None:
                        return None, None
                    dets, annotated = _inference.predict(frame)
                    return (frame, dets, annotated)

            result = await loop.run_in_executor(None, grab_and_infer)
            if result is None or result[0] is None:
                await asyncio.sleep(0.02)
                continue

            frame, detections, annotated = result

            raw_frame_index += 1
            # Legacy processed every 2nd frame (GrabImage.py:117).
            if raw_frame_index % decimation != 0:
                continue

            # Detection state machine — belt stop, interlock, crops, counting.
            state = await loop.run_in_executor(
                None, scan_session.process_frame, frame, detections
            )

            display = annotated if annotated is not None else frame
            if settings.STREAM_MAX_WIDTH and display.shape[1] > settings.STREAM_MAX_WIDTH:
                scale = settings.STREAM_MAX_WIDTH / display.shape[1]
                display = cv2.resize(
                    display, (settings.STREAM_MAX_WIDTH, int(display.shape[0] * scale))
                )

            ok, buffer = cv2.imencode(".jpg", display, encode_params)
            if not ok:
                continue
            b64_frame = base64.b64encode(buffer).decode("utf-8")

            fps_counter += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 1.0:
                current_fps = fps_counter / elapsed
                fps_counter = 0
                fps_timer = time.time()

            await websocket.send_json({
                "frame": b64_frame,
                "frame_width": int(display.shape[1]),
                "frame_height": int(display.shape[0]),
                "source_width": int(frame.shape[1]),
                "source_height": int(frame.shape[0]),
                "detections": [[float(v) for v in d] for d in (detections or [])],
                "fps": round(current_fps, 1),
                **state,
            })

            sleep_time = frame_interval - (time.time() - loop_start)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        with _clients_lock:
            _stream_clients -= 1
        logger.info("WebSocket stream ended")


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@router.get("/status")
def camera_status():
    return {
        "camera_initialized": _camera_initialized,
        "model_loaded": bool(_inference and _inference.is_loaded),
        "mock_mode": settings.USE_MOCK_CAMERA,
        "stream_fps_target": settings.STREAM_FPS,
        "jpeg_quality": settings.STREAM_JPEG_QUALITY,
        "stream_clients": _stream_clients,
    }


class ModelSwitchRequest(BaseModel):
    commodity: str
    variety: str = ""


@router.post("/model")
def switch_model(req: ModelSwitchRequest):
    """Load the inference model for a commodity/variety.

    Returns 503 on failure rather than {"success": false}: a scan running with
    no model produces zero detections and looks like a clean sample.
    """
    if not _ensure_services():
        raise HTTPException(status_code=503, detail="Camera/inference services unavailable")

    with _hardware_lock:
        ok = _inference.load_model(req.commodity, req.variety)

    if not ok:
        raise HTTPException(
            status_code=503,
            detail=f"Could not load a model for commodity={req.commodity!r} "
                   f"variety={req.variety!r}. Check MODEL_DIR and the model files.",
        )
    return {"success": True, "commodity": req.commodity, "variety": req.variety}


@router.post("/start")
def start_camera():
    if not _ensure_services():
        raise HTTPException(status_code=503, detail="Camera initialization failed")
    return {"success": True, **camera_health()}


@router.post("/stop")
def stop_camera():
    """Stop acquisition. Legacy paired this with a 1s conveyor deceleration
    delay (stop_camera_with_delay, main.py:720-739)."""
    global _camera_initialized
    with _hardware_lock:
        if _camera is not None:
            _camera.stop_grabbing()
        _camera_initialized = False
    return {"success": True}
