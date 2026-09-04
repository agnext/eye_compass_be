"""
XAI heatmap endpoint.

Accepts JSON with a base64 frame and returns JSON with a base64 heatmap, which
is what the frontend client actually speaks. The previous version expected
multipart/form-data with an UploadFile and returned raw image/jpeg bytes, so
every request 422'd and nothing would have rendered even if it had not.

It also reuses the already-loaded inference model and its per-commodity
confidence threshold instead of building a fresh TensorRT engine per request
and never releasing it.
"""

import base64
import logging

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import xai_service as xai
from app.services.inference_service import apply_suppression_rules

logger = logging.getLogger(__name__)
router = APIRouter()


class XAIRequest(BaseModel):
    # Base64 JPEG/PNG, with or without a data: URI prefix.
    frame_base64: str
    commodity: str = ""
    variety: str = ""
    # If the caller already has detections, reuse them rather than re-inferring.
    detections: list = []


def _decode(frame_base64: str) -> np.ndarray:
    payload = frame_base64.split(",", 1)[-1] if "," in frame_base64 else frame_base64
    try:
        raw = base64.b64decode(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="frame_base64 is not valid base64")
    # np.frombuffer, not np.fromstring — the latter was removed in NumPy 2.x.
    array = np.frombuffer(raw, np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="Could not decode the image")
    return image


@router.post("/generate")
def generate_heatmap(req: XAIRequest):
    image = _decode(req.frame_base64)

    # Frames from the camera are RGB (COLOR_BAYER_RG2RGB); the heatmap pipeline
    # and cv2 both expect BGR (xai_optimized.py:51-53).
    img_bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

    detections = req.detections or []
    used = "supplied-detections"

    if not detections:
        from app.api.camera import _hardware_lock, _inference

        if _inference is not None and _inference.is_loaded:
            with _hardware_lock:
                detections, _ = _inference.predict(img_bgr)
            detections, _flag = apply_suppression_rules(
                detections, req.commodity, req.variety
            )
            used = "live-model"

    if detections:
        overlay = xai.build_confidence_heatmap(img_bgr, detections)
    else:
        # Legacy falls back to the ForwardCAM activation map when no optimized
        # model is available (main.py:1020-1023).
        overlay = xai.build_activation_heatmap(img_bgr)
        used = "forward-cam"
        if overlay is None:
            overlay = img_bgr
            used = "none"

    ok, buffer = cv2.imencode(".jpg", overlay, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encode the heatmap")

    return {
        "status": "success",
        "method": used,
        "detection_count": len(detections),
        "heatmap_base64": base64.b64encode(buffer).decode("ascii"),
    }
