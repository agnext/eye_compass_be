"""
Camera WebSocket API — streams live camera frames (with optional inference
overlay) to the React frontend.

Endpoints:
    WS  /ws/camera/stream   — continuous JPEG frame stream
    GET /api/camera/status   — camera health check
    POST /api/camera/model   — switch the active inference model
"""

import asyncio
import base64
import logging
import time

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.config import settings
from app.services.camera_service import get_camera_service, BaseCameraService
from app.services.inference_service import get_inference_service, BaseInferenceService
from app.services.sort import ObjectTracker

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Singleton-ish service holders (created once at startup)
# ---------------------------------------------------------------------------

_camera: BaseCameraService | None = None
_inference: BaseInferenceService | None = None
_camera_initialized = False


def _ensure_services():
    """Lazy-init camera + inference services on first use."""
    global _camera, _inference, _camera_initialized

    if _camera is None:
        _camera = get_camera_service()
    if _inference is None:
        _inference = get_inference_service()

    if not _camera_initialized:
        if _camera.initialize():
            _camera.start_grabbing()
            _camera_initialized = True
            logger.info("Camera services initialized and grabbing")
        else:
            logger.error("Camera initialization FAILED")


# ---------------------------------------------------------------------------
# WebSocket — /ws/camera/stream
# ---------------------------------------------------------------------------

@router.websocket("/ws/camera/stream")
async def camera_stream(websocket: WebSocket):
    """
    Streams JPEG-encoded frames over the WebSocket as base64 strings.

    The frontend receives each message as:
        { "frame": "<base64 jpeg>", "detections": [...], "fps": 20.1 }
    """
    await websocket.accept()
    logger.info("WebSocket client connected to /ws/camera/stream")

    _ensure_services()

    if not _camera_initialized or _camera is None:
        await websocket.send_json({"error": "Camera not available"})
        await websocket.close()
        return

    frame_interval = 1.0 / settings.STREAM_FPS
    fps_counter = 0
    fps_timer = time.time()
    current_fps = 0.0
    
    # Initialize tracker for the session
    tracker = ObjectTracker(x_tolerance=10)
    frame_idx = 0
    cumulative_counts = {}

    try:
        while True:
            loop_start = time.time()

            # Grab frame (blocking call — run in threadpool to keep async)
            frame = await asyncio.get_event_loop().run_in_executor(
                None, _camera.grab_frame
            )

            if frame is None:
                await asyncio.sleep(0.05)
                continue

            # Run inference (also blocking — offload)
            detections, annotated_frame = await asyncio.get_event_loop().run_in_executor(
                None, _inference.predict, frame
            )

            # Encode to JPEG
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.STREAM_JPEG_QUALITY]
            _, buffer = cv2.imencode(".jpg", annotated_frame, encode_params)
            b64_frame = base64.b64encode(buffer).decode("utf-8")

            # FPS calculation
            fps_counter += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 1.0:
                current_fps = fps_counter / elapsed
                fps_counter = 0
                fps_timer = time.time()

            # Update Object Tracker
            frame_idx += 1
            if detections:
                # detections is list of [x1, y1, x2, y2, conf, class_id]
                # sort expects same format
                tracker.update(detections, frame_idx, frame.shape[:2])
                tracked = tracker.get_tracked_objects()
                # Recompute cumulative counts based on tracked objects
                cumulative_counts = {}
                for obj_id, obj_data in tracked.items():
                    cls_id = int(obj_data['bbox'][5])
                    cls_name = f"Class_{cls_id}"
                    # Common legacy mappings (fallback if needed)
                    if cls_id == 0: cls_name = "OT"
                    elif cls_id == 1: cls_name = "MFM"
                    elif cls_id == 2: cls_name = "IFM"
                    elif cls_id == 3: cls_name = "AO"
                    elif cls_id == 4: cls_name = "OFG"
                    elif cls_id == 5: cls_name = "OFM"
                    
                    cumulative_counts[cls_name] = cumulative_counts.get(cls_name, 0) + 1

            # Send to client
            await websocket.send_json({
                "frame": b64_frame,
                "detections": detections,
                "cumulative_counts": cumulative_counts,
                "fps": round(current_fps, 1),
            })

            # Throttle to target FPS
            processing_time = time.time() - loop_start
            sleep_time = frame_interval - processing_time
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        logger.info("WebSocket stream ended")


# ---------------------------------------------------------------------------
# REST — /api/camera/status
# ---------------------------------------------------------------------------

@router.get("/api/camera/status")
def camera_status():
    """Returns the current camera and inference service status."""
    return {
        "camera_initialized": _camera_initialized,
        "mock_mode": settings.USE_MOCK_CAMERA,
        "stream_fps_target": settings.STREAM_FPS,
        "jpeg_quality": settings.STREAM_JPEG_QUALITY,
    }


# ---------------------------------------------------------------------------
# REST — /api/camera/model
# ---------------------------------------------------------------------------

class ModelSwitchRequest(BaseModel):
    commodity: str
    variety: str = ""


@router.post("/api/camera/model")
def switch_model(req: ModelSwitchRequest):
    """Switch the active inference model to match a different commodity."""
    _ensure_services()

    if _inference is None:
        return {"success": False, "error": "Inference service not available"}

    ok = _inference.load_model(req.commodity, req.variety)
    return {
        "success": ok,
        "commodity": req.commodity,
        "variety": req.variety,
    }
