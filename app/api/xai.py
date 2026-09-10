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
from app.services.scan_session import scan_session

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


def _run_inference(frame: np.ndarray):
    """Runs on camera.py's single dedicated hardware thread — see the note
    above camera.py's _hw_executor. TensorRT/pycuda pushes a CUDA context
    onto whichever thread first calls predict() and only pops it via an
    explicit cleanup() call made on THAT SAME thread at shutdown; calling
    predict() straight from a FastAPI request handler (a fresh thread from
    Starlette's own pool every time) pushes a context nobody ever pops, and
    the process hard-aborts at shutdown ("PyCUDA ERROR: The context stack
    was not empty... Aborted (core dumped)") — confirmed live.
    """
    from app.api.camera import _hardware_lock, _inference

    if _inference is None or not _inference.is_loaded:
        return [], None
    with _hardware_lock:
        return _inference.predict(frame)


@router.post("/generate")
def generate_heatmap(req: XAIRequest):
    from app.api.camera import _run_on_hw_thread

    detections = req.detections or []
    used = "supplied-detections"

    # Prefer the exact frame the live pipeline is already holding frozen
    # (ScanSession.pending_frame, set at lock time — scan_session.py:387)
    # over the client's frame_base64, when there is no explicit detections
    # list to reuse. This is what legacy's show_xai_image actually re-infers
    # on (main.py:1015-1055: the same in-memory `image` the belt locked
    # with) — matching resolution and color order to what the model was
    # just run against live. The client-supplied frame_base64 is instead the
    # WebSocket preview copy (JPEG-compressed and possibly downscaled to
    # STREAM_MAX_WIDTH for bandwidth, camera.py's encode_display) — close
    # enough to look at, but re-inferring on it found nothing: confirmed
    # live, the same detections that showed on the frozen preview vanished
    # entirely once inference ran on that lossy/downscaled copy instead of
    # the original frame.
    if not detections and scan_session.pending_frame is not None:
        # pending_frame is RGB (camera_service.py's COLOR_BAYER_RG2RGB),
        # same as what the live loop hands _inference.predict() directly
        # (camera.py:267) — so re-run it unconverted for identical results,
        # then convert to BGR only for the drawing/colormap step below.
        raw_frame = scan_session.pending_frame
        img_bgr = cv2.cvtColor(raw_frame, cv2.COLOR_RGB2BGR)
        detections, _ = _run_on_hw_thread(_run_inference, raw_frame)
        detections, _flag = apply_suppression_rules(detections, req.commodity, req.variety)
        used = "pending-frame-live-model"
    else:
        # cv2.imdecode always yields BGR for a standard JPEG, regardless of
        # what colorspace the camera pipeline used before encoding it — no
        # further conversion needed (unlike the pending_frame branch above,
        # this has already been through a real JPEG round-trip).
        img_bgr = _decode(req.frame_base64)

        if not detections:
            detections, _ = _run_on_hw_thread(_run_inference, img_bgr)
            detections, _flag = apply_suppression_rules(detections, req.commodity, req.variety)
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
