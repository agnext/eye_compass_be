"""
Inference Service — model selection, execution and post-processing.

This is a direct port of the legacy inference path, which was split across
GrabImage.py's ProcessingThread:

    model_paths        (GrabImage.py:221-247)  -> MODEL_REGISTRY
    get_model_infer    (GrabImage.py:384-443)  -> resolve_model
    process_results    (GrabImage.py:445-536)  -> apply_suppression_rules
    enlarge_bbox       (GrabImage.py:547-569)  -> enlarge_bbox

Two things the earlier version of this file got wrong and that are corrected here:

  * It pointed at ml_m/*.engine files. Legacy loads models/*.optimized via
    ModelInfer (which is an alias of TensorRTInference, run_inference.py:603).
  * It resolved a per-commodity confidence threshold and then never passed it
    to predict(), so everything inferred at a hardcoded 0.2.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Class ids, from the mapping documented at GrabImage.py:453-459
# ---------------------------------------------------------------------------

CLASS_NAMES = {0: "OT", 1: "MFM", 2: "IFM", 3: "AO", 4: "OFG", 5: "OFM"}


# ---------------------------------------------------------------------------
# Model registry — filenames exactly as legacy model_paths (GrabImage.py:221-247)
# ---------------------------------------------------------------------------

MODEL_REGISTRY = {
    "stem_rice": "steam_rice_v1_5_0.optimized",
    "sella_rice": "sella_rice_v_1_0.optimized",
    "toor": "Toor_dal_v1_9_0.optimized",
    "masoor_mlka": "masoor_malka_v1_0.optimized",
    "moong_whole": "model_moong_whole_v1_0.optimized",
    "urad_dal": "urad_dhuli_v1.optimized",
    "kabuli_chana": "Kabuli_chana_v1_2.optimized",
    "rice": "Tibar_v7_new.optimized",
    "masoor_dal": "Masoor_v1.4.0.optimized",
    "lobia": "lobia_v5.optimized",
    "chitra_rajma": "Chitra_rajma_v4.optimized",
    "Dawat_rice": "Dawat_sona_chandi_v3.optimized",
    "Black_chana": "Black_chana_v4.optimized",
    "Rosted_chana": "Rosted_chana_v2.optimized",
    "chanadal": "Toor_v6.optimized",
    # 15 June 2026
    "white_peas": "White_peav1.optimized",
    "moth_dal": "Moth_dal_v2.optimized",
    "sabudana": "sabudana_v2.optimized",
    "brown_rice": "brown_rice_v3.optimized",
    "sona_masoori_rice": "smrice_v2.optimized",
    "green_moong_dal": "green_moong_v4.optimized",
    # 23 July 2026
    "masoor_red_whole": "masoor_red_whole_v1.4.0.optimized",
}


def normalize_commodity(value: str) -> str:
    """Legacy normalised commodity/variety with .replace(' ','_').lower()
    before model lookup (main.py:799, 818-819). Without this, every
    multi-word commodity from the Qualix config misses the table."""
    return (value or "").strip().replace(" ", "_").lower()


def resolve_model(commodity: str, variety: str) -> Tuple[Optional[str], float]:
    """Return (model_key, conf_threshold) for a commodity/variety.

    Branch-for-branch port of get_model_infer (GrabImage.py:384-443), including
    the final fallback to stem_rice @ 0.4.
    """
    c = normalize_commodity(commodity)
    v = normalize_commodity(variety)

    if c == "rice":
        if v == "dawat":
            return "Dawat_rice", 0.10
        if v == "tibar":
            return "rice", 0.10
        if v in ("gr1-ri01-st2", "gr1-ri01-rw1"):
            return "stem_rice", 0.20
        if v == "gr1-ri01-sl3":
            return "brown_rice", 0.10
        if v == "brown_rice":
            return "brown_rice", 0.10
        # Legacy falls through the rice branch to the bottom default.
        return "stem_rice", 0.40

    table = {
        "chana_dal": ("chanadal", 0.10),
        "toor_dal": ("chanadal", 0.10),
        "moong_dal": ("chanadal", 0.10),
        "urad_white": ("urad_dal", 0.10),
        "masoor_malka": ("masoor_mlka", 0.20),
        "kabuli_chana": ("kabuli_chana", 0.30),
        "moong_whole": ("green_moong_dal", 0.10),
        "urad_whole_black": ("urad_dal", 0.30),
        "toor": ("toor", 0.10),
        "masoor_red_whole": ("masoor_red_whole", 0.10),
        "lobia": ("lobia", 0.10),
        "chitra_rajma": ("chitra_rajma", 0.30),
        "black_chana": ("Black_chana", 0.10),
        "dalia": ("Rosted_chana", 0.40),
        # 15 June 2026 additions
        "white_peas": ("white_peas", 0.10),
        "moth_dal": ("moth_dal", 0.10),
        "sabudana": ("sabudana", 0.10),
        "green_moong_dal": ("green_moong_dal", 0.10),
        "sonamasoori_rice": ("sona_masoori_rice", 0.25),
    }

    if c in table:
        return table[c]

    # Legacy's final `return self._get_or_load_model('stem_rice'), 0.4`.
    # Legacy was silent about it; we are not, because loading a rice model for
    # an unmapped commodity produces confident, wrong results.
    logger.warning(
        "No model mapped for commodity=%r variety=%r — falling back to stem_rice @ 0.40 "
        "(legacy behaviour). Detections for this commodity will not be trustworthy.",
        commodity,
        variety,
    )
    return "stem_rice", 0.40


# ---------------------------------------------------------------------------
# Post-processing — ports of process_results and enlarge_bbox
# ---------------------------------------------------------------------------

def apply_suppression_rules(detections: List, commodity: str, variety: str) -> Tuple[List, bool]:
    """Commodity-specific false-positive suppression.

    Port of process_results (GrabImage.py:445-536). Returns (detections, fm_flag).

    Legacy inspects only detections[0] — the first detection in the frame — and
    suppresses the whole frame based on it. That is preserved deliberately:
    changing it to a per-detection filter would alter counts relative to the
    legacy machine, which is what this migration is supposed to keep constant.
    Commented-out legacy rules are left out, as they were inactive.
    """
    if not detections:
        return [], True

    c = normalize_commodity(commodity)
    v = normalize_commodity(variety)

    first = detections[0]
    conf = first[4]
    cls = first[5]

    def drop():
        return [], False

    if c == "rice" and v == "tibar" and cls in (2, 4, 5) and conf < 0.20:
        return drop()
    if c == "toor_dal" and cls == 4 and conf < 0.25:
        return drop()
    if c == "chana_dal" and (
        (cls == 2 and conf < 0.45) or (cls == 3 and conf < 0.17) or (cls == 4 and conf < 0.50)
    ):
        return drop()
    if c == "black_chana" and cls == 3 and conf < 0.30:
        return drop()
    if c == "dalia" and ((cls == 3 and conf < 0.80) or (cls == 4 and conf < 0.40)):
        return drop()
    if c == "white_peas" and cls == 4 and conf < 0.25:
        return drop()
    if c == "lobia" and cls == 3 and conf < 0.20:
        return drop()
    if c == "rice" and v == "brown_rice" and cls == 3 and conf < 0.20:
        return drop()
    if c == "rice" and v == "dawat" and cls in (1, 3, 4) and conf < 0.25:
        return drop()
    if c == "moong_whole" and (
        (cls == 4 and conf < 0.50) or (cls in (0, 1, 2, 5) and conf < 0.30)
    ):
        return drop()
    if c == "sonamasoori_rice" and ((cls == 3 and conf < 0.51) or (cls == 5 and conf < 0.30)):
        return drop()
    if c == "masoor_red_whole" and cls in (3, 5) and conf < 0.20:
        return drop()

    return detections, True


def enlarge_bbox(bbox, pad: int = 10, img_w: int = None, img_h: int = None):
    """Pad a detection box by `pad` px on every side (GrabImage.py:547-569).

    Legacy applies this to every emitted box in emit_results, so the crops the
    operator sees and the saved images include a margin around the object.
    """
    if len(bbox) == 6:
        x1, y1, x2, y2, confidence, class_id = bbox
    else:
        x1, y1, x2, y2 = bbox[:4]
        confidence = class_id = None

    nx1, ny1, nx2, ny2 = x1 - pad, y1 - pad, x2 + pad, y2 + pad

    if img_w and img_h:
        nx1 = max(0, nx1)
        ny1 = max(0, ny1)
        nx2 = min(img_w - 1, nx2)
        ny2 = min(img_h - 1, ny2)

    if class_id is not None:
        return [nx1, ny1, nx2, ny2, confidence, class_id]
    return [nx1, ny1, nx2, ny2]


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class BaseInferenceService(ABC):
    @abstractmethod
    def load_model(self, commodity: str, variety: str) -> bool: ...

    @abstractmethod
    def predict(self, frame: np.ndarray, iou_threshold: float = 0.45) -> Tuple[List, np.ndarray]: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    @property
    @abstractmethod
    def is_loaded(self) -> bool: ...


class MockInferenceService(BaseInferenceService):
    """No model — returns the frame unchanged with no detections."""

    def __init__(self):
        self._commodity = ""
        self._variety = ""
        self._loaded = False

    def load_model(self, commodity: str, variety: str) -> bool:
        key, conf = resolve_model(commodity, variety)
        self._commodity, self._variety = commodity, variety
        self._loaded = True
        logger.info("[MockInference] would load %s (conf=%.2f) for %s/%s", key, conf, commodity, variety)
        return True

    def predict(self, frame: np.ndarray, iou_threshold: float = 0.45) -> Tuple[List, np.ndarray]:
        return [], frame.copy()

    def cleanup(self) -> None:
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded


class TensorRTInferenceService(BaseInferenceService):
    """Wraps ModelInfer / TensorRTInference from the legacy run_inference module."""

    def __init__(self):
        import sys

        src = settings.EYE_COMPASS_SRC
        if src not in sys.path:
            sys.path.insert(0, src)
        from run_inference import ModelInfer  # type: ignore

        self._ModelInfer = ModelInfer
        self._cache = {}          # model_key -> instance, mirrors legacy model_cache
        self._model = None
        self._model_key = None
        self._commodity = ""
        self._variety = ""
        self._conf_threshold = 0.2

    def load_model(self, commodity: str, variety: str) -> bool:
        model_key, conf = resolve_model(commodity, variety)
        if model_key is None:
            return False

        filename = MODEL_REGISTRY.get(model_key)
        if filename is None:
            logger.error("Model key %r is not in MODEL_REGISTRY", model_key)
            return False

        path = os.path.join(settings.MODEL_DIR, filename)
        if not os.path.exists(path):
            logger.error(
                "Model file missing: %s (commodity=%r variety=%r key=%r)",
                path, commodity, variety, model_key,
            )
            return False

        # Legacy caches instances by key and switches between them rather than
        # rebuilding the engine on every commodity change (GrabImage.py:375-382).
        if model_key not in self._cache:
            try:
                logger.info("Loading model %s from %s", model_key, path)
                self._cache[model_key] = self._ModelInfer(path, img_size=(640, 640))
            except Exception as exc:
                logger.error("Failed to load model %s: %s", model_key, exc)
                return False

        self._model = self._cache[model_key]
        self._model_key = model_key
        self._commodity = commodity
        self._variety = variety
        self._conf_threshold = conf
        logger.info(
            "Active model: %s (%s) conf=%.2f for %s/%s",
            model_key, filename, conf, commodity, variety,
        )
        return True

    def predict(self, frame: np.ndarray, iou_threshold: float = 0.45) -> Tuple[List, np.ndarray]:
        """Run inference at the model's own confidence threshold.

        The threshold is taken from self._conf_threshold, never from a default
        argument — that was the bug that pinned every commodity to 0.2.
        Legacy call: predict(frame, conf_threshold=self.conf_threshold, iou_threshold=0.45)
        """
        if self._model is None:
            return [], frame.copy()
        try:
            detections, annotated = self._model.predict(
                frame,
                conf_threshold=self._conf_threshold,
                iou_threshold=iou_threshold,
            )
            return detections, annotated
        except Exception as exc:
            logger.error("Inference error: %s", exc)
            return [], frame.copy()

    def cleanup(self) -> None:
        for key, instance in list(self._cache.items()):
            try:
                if hasattr(instance, "cleanup"):
                    instance.cleanup()
            except Exception as exc:
                logger.warning("Error cleaning up model %s: %s", key, exc)
        self._cache.clear()
        self._model = None
        self._model_key = None
        logger.info("Inference service cleaned up")

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def conf_threshold(self) -> float:
        return self._conf_threshold


def get_inference_service() -> BaseInferenceService:
    if settings.USE_MOCK_CAMERA:
        logger.info("USE_MOCK_CAMERA=true -> MockInferenceService")
        return MockInferenceService()
    logger.info("USE_MOCK_CAMERA=false -> TensorRTInferenceService")
    return TensorRTInferenceService()
