"""
Inference Service — wraps TensorRT model inference for FM detection.

When USE_MOCK_CAMERA=true, this returns raw frames without running any
inference (no tensorrt / pycuda imports needed).

When USE_MOCK_CAMERA is NOT set, it imports the TensorRTInference class
from run_inference.py and loads the .engine models for the selected commodity.
"""

import logging
import os
import numpy as np
from abc import ABC, abstractmethod
from typing import List, Tuple, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class BaseInferenceService(ABC):
    """Interface for inference backends."""

    @abstractmethod
    def load_model(self, commodity: str, variety: str) -> bool:
        """Load the correct model for the given commodity/variety."""
        ...

    @abstractmethod
    def predict(
        self, frame: np.ndarray, conf_threshold: float = 0.2, iou_threshold: float = 0.45
    ) -> Tuple[List, np.ndarray]:
        """
        Run inference on a single frame.
        Returns (detections, annotated_frame).
        detections: list of [x1, y1, x2, y2, confidence, class_id]
        """
        ...

    @abstractmethod
    def cleanup(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Model-to-engine mapping (mirrors GrabImage.py get_model_infer)
# ---------------------------------------------------------------------------

# Maps (commodity, variety) → (engine_filename, conf_threshold)
# When variety is None, any variety for that commodity uses the given engine.
MODEL_MAP: dict = {
    ("rice", "tibar"):           ("tiber_model_1.4.4.engine", 0.10),
    ("rice", "dawat"):           ("Dawat_sona_chandi_v3.engine", 0.20),
    ("rice", "gr1-ri01-st2"):    ("steam_rice_v1_5_0.engine", 0.20),
    ("rice", "gr1-ri01-rw1"):    ("steam_rice_v1_5_0.engine", 0.20),
    ("rice", "gr1-ri01-sl3"):    ("sella_rice_v_1_0.engine", 0.40),
    ("toor", None):              ("toor_syn_v1.8_0.engine", 0.50),
    ("chana_dal", None):         ("Toor_syn_v1_7_0.engine", 0.10),
    ("toor_dal", None):          ("Toor_syn_v1_7_0.engine", 0.10),
    ("moong_dal", None):         ("Toor_syn_v1_7_0.engine", 0.10),
    ("urad_white", None):        ("Toor_syn_v1_7_0.engine", 0.10),
    ("masoor_malka", None):      ("masoor_malka_v1_0.engine", 0.20),
    ("kabuli_chana", None):      ("Kabuli_chana_v1_2.engine", 0.30),
    ("moong_whole", None):       ("model_moong_whole_v1_0.engine", 0.20),
    ("urad_whole_black", None):  ("urad_chilka_v1_3.engine", 0.30),
    ("masoor_red_whole", None):  ("masoor_red_whole_v1.4.0.engine", 0.10),
    ("lobia", None):             ("lobia_v5.engine", 0.10),
    ("chitra_rajma", None):      ("Rajm_new_4.engine", 0.30),
    ("black_chana", None):       ("black_chana1.4.0.engine", 0.10),
    ("dalia", None):             ("Rosted_chana_v2.engine", 0.40),
}


def _resolve_model(commodity: str, variety: str) -> Tuple[str, float]:
    """Look up the engine file and confidence threshold for a commodity/variety."""
    commodity = commodity.lower().strip()
    variety = variety.lower().strip() if variety else ""

    # Try exact (commodity, variety) first, then (commodity, None)
    key = (commodity, variety) if (commodity, variety) in MODEL_MAP else (commodity, None)
    if key in MODEL_MAP:
        return MODEL_MAP[key]

    # Fallback
    logger.warning(f"No model mapped for ({commodity}, {variety}); using default")
    return ("steam_rice_v1_5_0.engine", 0.40)


# ---------------------------------------------------------------------------
# Mock implementation
# ---------------------------------------------------------------------------

class MockInferenceService(BaseInferenceService):
    """Returns frames unchanged — no real inference."""

    def __init__(self):
        self._commodity = ""
        self._variety = ""

    def load_model(self, commodity: str, variety: str) -> bool:
        self._commodity = commodity
        self._variety = variety
        logger.info(f"[MockInference] Model set to {commodity}/{variety} (no-op)")
        return True

    def predict(
        self, frame: np.ndarray, conf_threshold: float = 0.2, iou_threshold: float = 0.45
    ) -> Tuple[List, np.ndarray]:
        # Return no detections + the original frame
        return [], frame.copy()

    def cleanup(self) -> None:
        logger.info("[MockInference] Cleaned up (no-op)")


# ---------------------------------------------------------------------------
# Real TensorRT implementation
# ---------------------------------------------------------------------------

class TensorRTInferenceService(BaseInferenceService):
    """
    Wraps the TensorRTInference class from run_inference.py.
    Only imported when USE_MOCK_CAMERA is False.
    """

    def __init__(self):
        # Lazy import so Windows never touches tensorrt/pycuda
        import sys
        sys.path.insert(0, os.getenv("EYE_COMPASS_SRC", "/home/nvidia/eye_compass"))
        from run_inference import TensorRTInference  # type: ignore

        self._TensorRTInference = TensorRTInference
        self._model: Optional["TensorRTInference"] = None
        self._commodity = ""
        self._variety = ""
        self._conf_threshold = 0.2

    def load_model(self, commodity: str, variety: str) -> bool:
        engine_file, conf = _resolve_model(commodity, variety)
        engine_path = os.path.join(settings.MODEL_DIR, engine_file)

        if not os.path.exists(engine_path):
            logger.error(f"Engine file not found: {engine_path}")
            return False

        # Cleanup previous model if switching
        if self._model is not None:
            try:
                self._model.cleanup()
            except Exception:
                pass

        try:
            self._model = self._TensorRTInference(engine_path, img_size=(640, 640))
            self._commodity = commodity
            self._variety = variety
            self._conf_threshold = conf
            logger.info(f"Loaded TensorRT model: {engine_file} (conf={conf})")
            return True
        except Exception as e:
            logger.error(f"Failed to load TensorRT model {engine_file}: {e}")
            return False

    def predict(
        self, frame: np.ndarray, conf_threshold: float = 0.2, iou_threshold: float = 0.45
    ) -> Tuple[List, np.ndarray]:
        if self._model is None:
            return [], frame.copy()

        try:
            detections, annotated = self._model.predict(
                frame,
                conf_threshold=conf_threshold or self._conf_threshold,
                iou_threshold=iou_threshold,
            )
            return detections, annotated
        except Exception as e:
            logger.error(f"Inference error: {e}")
            return [], frame.copy()

    def cleanup(self) -> None:
        if self._model is not None:
            try:
                self._model.cleanup()
            except Exception:
                pass
            self._model = None
        logger.info("TensorRT inference service cleaned up")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_inference_service() -> BaseInferenceService:
    """Return a MockInferenceService or TensorRTInferenceService based on env."""
    if settings.USE_MOCK_CAMERA:
        logger.info("USE_MOCK_CAMERA=true → using MockInferenceService")
        return MockInferenceService()
    else:
        logger.info("USE_MOCK_CAMERA not set → using TensorRTInferenceService")
        return TensorRTInferenceService()
