"""
XAI heatmap generation.

Legacy has two paths (main.py:1006-1046):

  * Primary — xai_optimized.generate_xai_heatmap_optimized: runs the live
    optimized model and builds a confidence-weighted gaussian heatmap from the
    detection boxes. This is what the operator normally sees.
  * Fallback — test_xai.image_preprocessing -> xai.py's ForwardCAM, used only
    when the optimized model is not loaded. That one hooks a real target layer
    and produces an activation map.

Both are provided here. Two fidelity details the earlier port missed:

  * The RGB->BGR conversion at xai_optimized.py:51-53. Camera frames come out of
    COLOR_BAYER_RG2RGB, so skipping it channel-swaps both the image and the
    colormap overlay.
  * The confidence threshold. Legacy passes the live model's per-commodity
    threshold (main.py:1015); a hardcoded 0.2 changes which boxes appear.
"""

import logging
from typing import List, Optional

import cv2
import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)


def build_confidence_heatmap(img_bgr: np.ndarray, detections: List) -> np.ndarray:
    """Confidence-weighted gaussian overlay.

    Port of generate_xai_heatmap_optimized (xai_optimized.py:62-165), from the
    point where detections are already available.
    """
    if img_bgr is None:
        return img_bgr
    if not detections:
        return img_bgr

    h, w = img_bgr.shape[:2]
    mask = np.zeros((h, w), dtype=np.float32)

    def clamp_box(det):
        x1, y1, x2, y2 = det[:4]
        x1 = max(0, min(int(x1), w - 1))
        y1 = max(0, min(int(y1), h - 1))
        x2 = max(0, min(int(x2), w - 1))
        y2 = max(0, min(int(y2), h - 1))
        return x1, y1, x2, y2

    for det in detections:
        if len(det) < 6:
            continue
        x1, y1, x2, y2 = clamp_box(det)
        conf = det[4]
        if x2 <= x1 or y2 <= y1:
            continue

        bw, bh = x2 - x1, y2 - y1
        cx, cy = bw // 2, bh // 2
        ys, xs = np.ogrid[:bh, :bw]
        dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        max_dist = np.sqrt(cx ** 2 + cy ** 2)
        norm = dist / max_dist if max_dist > 0 else np.zeros_like(dist)
        box_mask = np.exp(-2 * norm ** 2) * float(conf)
        mask[y1:y2, x1:x2] = np.maximum(mask[y1:y2, x1:x2], box_mask)

    if mask.max() <= 0:
        return img_bgr
    mask = mask / mask.max()
    mask = 1.0 - mask  # high activation -> cool colours, as legacy does

    heatmap = cv2.applyColorMap(np.uint8(255 * mask), cv2.COLORMAP_JET)
    overlay = cv2.addWeighted(img_bgr, 1.0, heatmap, 0.6, 0)

    for det in detections:
        if len(det) < 6:
            continue
        x1, y1, x2, y2 = clamp_box(det)
        if x2 <= x1 or y2 <= y1:
            continue
        conf_text = f"{float(det[4]):.2f}"
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 255), 2)
        size = cv2.getTextSize(conf_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
        ty = max(y1 - 5, size[1])
        cv2.rectangle(overlay, (x1, ty - size[1] - 2), (x1 + size[0] + 2, ty + 2), (0, 0, 255), -1)
        cv2.putText(overlay, conf_text, (x1 + 1, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    return overlay


# ---------------------------------------------------------------------------
# ForwardCAM fallback (xai.py:26-38, 66+)
# ---------------------------------------------------------------------------

_cam_state = {"model": None, "cam": None}


class ForwardCAM:
    """Captures a layer's forward activations and reduces them to a CAM.

    Verbatim port of xai.py:27-41.
    """

    def __init__(self, layer):
        self.activations = None
        layer.register_forward_hook(self._hook)

    def _hook(self, module, inp, out):
        self.activations = out.detach()

    def generate(self):
        cam = self.activations.mean(dim=1).squeeze().cpu().numpy()
        return (cam - cam.min()) / (cam.max() + 1e-8)


def build_activation_heatmap(img_bgr: np.ndarray, img_size: int = 640) -> Optional[np.ndarray]:
    """Real activation CAM, used when no optimized model is loaded.

    Requires torch plus the yolov7 source tree and the checkpoint at
    XAI_MODEL_PATH. Returns None if any of that is unavailable, so the caller
    can fall back to the confidence heatmap rather than failing the request.
    """
    import os

    model_path = settings.XAI_MODEL_PATH
    if not model_path or not os.path.exists(model_path):
        logger.info("ForwardCAM unavailable: no checkpoint at %s", model_path)
        return None

    try:
        import sys

        import torch

        yolo_path = os.path.join(settings.EYE_COMPASS_SRC, "yolov7")
        if yolo_path not in sys.path:
            sys.path.append(yolo_path)

        if _cam_state["model"] is None:
            from models.experimental import attempt_load  # type: ignore

            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = attempt_load(model_path, map_location=device)
            model.eval()
            # Legacy hooks the last layer before the detection head.
            target_layer = list(model.model.children())[-2]
            _cam_state["model"] = model
            _cam_state["cam"] = ForwardCAM(target_layer)
            _cam_state["device"] = device

        model = _cam_state["model"]
        cam_hook = _cam_state["cam"]
        device = _cam_state["device"]

        h, w = img_bgr.shape[:2]
        resized = cv2.resize(img_bgr, (img_size, img_size))
        tensor = torch.from_numpy(resized[:, :, ::-1].copy()).float().div(255.0)
        tensor = tensor.permute(2, 0, 1).unsqueeze(0).to(device)

        with torch.no_grad():
            model(tensor)

        cam = cam_hook.generate()
        cam = cv2.resize(cam, (w, h))
        heatmap = cv2.applyColorMap(np.uint8(255 * (1.0 - cam)), cv2.COLORMAP_JET)
        return cv2.addWeighted(img_bgr, 1.0, heatmap, 0.6, 0)
    except Exception as exc:
        logger.warning("ForwardCAM fallback failed: %s", exc)
        return None


class XAIService:
    """Kept as a thin class for the existing import surface."""

    @staticmethod
    def generate_heatmap(image: np.ndarray, detections: List) -> np.ndarray:
        return build_confidence_heatmap(image, detections)
