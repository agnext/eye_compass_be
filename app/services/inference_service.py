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
    "stem_rice": "punjab_kesari_v1_0_02.optimized",
    "sella_rice": "sella_rice_v_1_0.optimized",
    "toor": "Toor_v6.optimized",
    "masoor_mlka": "masoor_malka_v1_0.optimized",
    "moong_whole": "model_moong_whole_v1_0.optimized",
    "urad_dal": "urad_chilka_v1_3.optimized",
    "kabuli_chana": "Kabuli_chana_v1_2.optimized",
    "rice": "Tibar_v7_new.optimized",
    "masoor_dal": "Masoor_v1.4.0.optimized",
    "lobia": "Lobia_v1.2_0.optimized",
    "chitra_rajma": "Rajm_new_4.optimized",
    "Dawat_rice": "Dawatv2_best.optimized",
    "Black_chana": "black_chana_v1.6.0.optimized",
    "Rosted_chana": "Rosted_chana_v2.optimized",
    "chanadal": "chana_daal_v1_1_0.optimized",
    "white_peas": "White_pea_v1.optimized",
    "moth_dal": "Mote_dal.optimized",
    "sabudana": "sabudana_v1.optimized",
    "brown_rice": "BrownRice_v1.optimized",
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
            return "Dawat_rice", 0.20
        if v == "tibar":
            return "rice", 0.30
        if v in ("gr1-ri01-st2", "gr1-ri01-rw1"):
            return "stem_rice", 0.30
        if v in ("gr1-ri01-sl3", "brown_rice"):
            return "brown_rice", 0.10
        # Legacy falls through the rice branch to the bottom default.
        return "stem_rice", 0.40

    table = {
        "chana_dal": ("chanadal", 0.20),
        "toor_dal": ("toor", 0.10),
        "moong_dal": ("chanadal", 0.10),
        "urad_white": ("chanadal", 0.10),
        "masoor_malka": ("masoor_mlka", 0.20),
        "kabuli_chana": ("kabuli_chana", 0.30),
        "moong_whole": ("moong_whole", 0.20),
        "urad_whole_black": ("urad_dal", 0.30),
        "toor": ("toor", 0.10),
        "masoor_red_whole": ("masoor_dal", 0.10),
        "lobia": ("lobia", 0.10),
        "chitra_rajma": ("chitra_rajma", 0.30),
        "black_chana": ("Black_chana", 0.32),
        "dalia": ("Rosted_chana", 0.40),
        "white_peas": ("white_peas", 0.10),
        "moth_dal": ("moth_dal", 0.10),
        "sabudana": ("sabudana", 0.10),
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

    Port of process_results. Returns (detections, fm_flag). The rules mirror the
    active (uncommented) branches of the production unit's GrabImage.py — that
    file is the source of truth for the machine this deployment replaces, and
    its thresholds differ from older copies on other units.

    Legacy inspects only detections[0] — the first detection in the frame — and
    suppresses the whole frame based on it. Kept identical to legacy so counts
    match the machine this deployment replaces. Commented-out legacy rules are
    left out, as they were inactive.
    """
    if not detections:
        return [], True

    c = normalize_commodity(commodity)
    v = normalize_commodity(variety)

    # Legacy inspects detections[0] and drops/keeps the whole frame on it
    # (GrabImage.py:534-590 use results[0]). Kept identical to legacy.
    first = detections[0]
    conf = first[4]
    cls = first[5]

    # No per-frame logging here: suppression runs on every inferred frame,
    # so logging each drop/keep floods the log. The decision this makes is
    # visible downstream only when a box actually survives — scan_session
    # logs that frame as an "FM candidate". A dropped frame is a non-event
    # and is deliberately silent.
    def _dropped(reason):
        return [], False

    def drop():
        return [], False

    if c == "rice" and v == "tibar" and cls in (2, 4, 5) and conf < 0.20:
        return _dropped("rice/tibar cls{2,4,5}<0.20")
    if c == "rice" and v == "dawat" and (
        (cls == 5 and conf < 0.50) or (cls == 0 and conf < 0.10)
    ):
        return _dropped("rice/dawat cls5<0.50 or cls0<0.10")
    if c == "rice" and v == "gr1-ri01-rw1" and (
        (cls == 5 and conf < 0.42) or (cls == 3 and conf < 0.36)
    ):
        return _dropped("rice/gr1-ri01-rw1 cls5<0.42 or cls3<0.36")
    if c == "chitra_rajma" and (
        (cls == 0 and conf < 0.80) or (cls == 5 and conf < 0.80) or (cls == 2 and conf < 0.65)
    ):
        return _dropped("chitra_rajma cls{0,5}<0.80 or cls2<0.65")
    if c == "kabuli_chana" and cls == 1 and conf < 0.60:
        return _dropped("kabuli_chana cls1<0.60")
    if c == "toor_dal" and (
        (cls == 4 and conf < 0.48) or (cls == 2 and conf < 0.25) or (cls == 5 and conf < 0.35)
    ):
        return _dropped("toor_dal cls4<0.48 or cls2<0.25 or cls5<0.35")
    if c == "chana_dal" and (
        (cls == 2 and conf < 0.40) or (cls == 4 and conf < 0.40) or (cls == 5 and conf < 0.30)
    ):
        return _dropped("chana_dal cls{2,4}<0.40 or cls5<0.30")
    if c == "masoor_red_whole" and (
        (cls == 5 and conf < 0.80) or (cls == 3 and conf < 0.60)
        or (cls == 1 and conf < 0.20) or (cls == 0 and conf < 0.30)
    ):
        return _dropped("masoor_red_whole various")
    if c == "black_chana" and cls == 6 and conf < 0.95:
        return _dropped("black_chana cls6<0.95")
    if c == "dalia" and ((cls == 3 and conf < 0.80) or (cls == 4 and conf < 0.40)):
        return _dropped("dalia cls3<0.80 or cls4<0.40")

    return detections, True


# Class code -> legacy label (GrabImage.py:524-530). Used only for logging.
_CLASS_LABELS = {0: "OT", 1: "MFM", 2: "IFM", 3: "AO", 4: "OFG", 5: "OFM"}

# Suppression thresholds as a lookup, ONLY for the human-readable FM trace in
# explain_suppression below. apply_suppression_rules above remains the single
# source of truth for the actual decision; if a threshold changes there, change
# it here too. Keyed (commodity, variety) with variety None meaning "any".
_RULE_TABLE = {
    ("rice", "tibar"): {2: 0.20, 4: 0.20, 5: 0.20},
    ("rice", "dawat"): {5: 0.50, 0: 0.10},
    ("rice", "gr1-ri01-rw1"): {5: 0.42, 3: 0.36},
    ("chitra_rajma", None): {0: 0.80, 5: 0.80, 2: 0.65},
    ("kabuli_chana", None): {1: 0.60},
    ("toor_dal", None): {4: 0.48, 2: 0.25, 5: 0.35},
    ("chana_dal", None): {2: 0.40, 4: 0.40, 5: 0.30},
    ("masoor_red_whole", None): {5: 0.80, 3: 0.60, 1: 0.20, 0: 0.30},
    ("black_chana", None): {3: 0.44},
    ("dalia", None): {3: 0.80, 4: 0.40},
}


def explain_suppression(detections: List, commodity: str, variety: str) -> str:
    """Human-readable account of why apply_suppression_rules kept this frame.

    Read-only — it makes no decision, it only describes the one the real
    function made. Called only on frames that actually become an FM, so its
    cost and verbosity never touch the hot path. Mirrors legacy's inspection
    of detections[0].
    """
    if not detections:
        return "no detections to judge"
    c = normalize_commodity(commodity)
    v = normalize_commodity(variety)
    first = detections[0]
    conf = float(first[4])
    cls = int(first[5])
    label = _CLASS_LABELS.get(cls, "?")
    rule = _RULE_TABLE.get((c, v)) or _RULE_TABLE.get((c, None))
    if rule is None:
        return ("box[0]=cls%d(%s) conf%.3f; '%s' has no suppression rules "
                "→ kept" % (cls, label, conf, c))
    if cls not in rule:
        kinds = ", ".join("cls%d" % k for k in sorted(rule))
        return ("box[0]=cls%d(%s) conf%.3f; '%s' only suppresses %s → "
                "no rule for cls%d, kept" % (cls, label, conf, c, kinds, cls))
    thr = rule[cls]
    return ("box[0]=cls%d(%s) conf%.3f; '%s' rule cls%d<%.2f; %.3f>=%.2f "
            "→ above threshold, kept" % (cls, label, conf, c, cls, thr, conf, thr))


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
        # Latches the "no model loaded" warning in predict() so it is said
        # once rather than on every frame; cleared again by load_model.
        self._warned_no_model = False

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
        # Armed again, so a later cleanup() that drops the model still gets
        # its own warning instead of being masked by this earlier one.
        self._warned_no_model = False
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
            # Silence here is dangerous: zero detections and a genuinely clean
            # sample look identical, on screen and in the saved result. The
            # model lives in this process's memory, so every backend restart
            # clears it, and it is only reloaded when the Dashboard mounts and
            # calls POST /api/camera/model — a page left open across a restart
            # never does. Twice in one hour that produced a scan that found
            # nothing and looked fine. Logged once per occurrence rather than
            # per frame: at 20-40 fps a plain warning here would be thousands
            # of identical lines a minute.
            if not self._warned_no_model:
                self._warned_no_model = True
                logger.warning(
                    "predict() called with NO MODEL LOADED — returning zero "
                    "detections. This scan will find nothing and will look "
                    "like a clean sample. Load one with POST /api/camera/model "
                    "(the Dashboard does this on mount, so reloading the page "
                    "fixes it). Expected after any backend restart."
                )
            return [], frame.copy()
        try:
            # Legacy's run_inference.py:144 does print(conf_thres, iou_thres)
            # on every predict() call. At ~17 inferred frames/sec that is a
            # flood of identical lines to journald, which blocks the writer
            # long enough to stall the WebSocket stream. We cannot edit the
            # legacy file (rollback must stay byte-identical), so suppress its
            # stdout only around this one call.
            import sys, os
            _devnull = getattr(self, "_devnull", None)
            if _devnull is None:
                _devnull = open(os.devnull, "w")
                self._devnull = _devnull
            _saved_stdout = sys.stdout
            sys.stdout = _devnull
            try:
                detections, annotated = self._model.predict(
                    frame,
                    conf_threshold=self._conf_threshold,
                    iou_threshold=iou_threshold,
                )
            finally:
                sys.stdout = _saved_stdout
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
