"""
Scan session — the inspection state machine.

This is the piece the original segregation left out. In the legacy app it lived
across appLogic and ConveyorController in main.py; a long-lived in-process
object held the whole scan. HTTP requests are stateless, so that state lives
here instead, in one server-side session.

The legacy sequence being reproduced:

    start_process        main.py:741-850   create output folders, stamp start time
    handle_detection     main.py:2566-2591 cumulative unique-track counting
    has_similar_x_axis   main.py:2516-2563 duplicate suppression
    fm_control           main.py:2604-2650 lock interlock, stop belt, freeze frame
    ImageLabel.mousePress main.py:232-259  operator taps a box
    crop_and_save        main.py:141-147   crop written as <FM_name>_<ts>.png
    submit_fm_type       main.py:1343-1360 the actual imwrite
    save_raw_image       main.py:2413-2441 r_frame_N.jpg, feeds "Frame Count"
    create_results       main.py:1372-1452 count files by filename prefix
    update_fm_count      main.py:1535-1563 the six looker_data metrics
    add_time_to_...      main.py:1592-1615 stop-time accumulation
    submit_create_result main.py:1618-1660 assemble result + result.json

Counting note: legacy never counted "objects currently tracked". It accumulated
unique track ids for the whole run (existing_track_ids) and produced the final
per-FM breakdown by listing saved crop files. Both are reproduced here.
"""

import json
import logging
import os
import shutil
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

import cv2
import numpy as np

from app.core.config import settings
from app.services.conveyor_service import conveyor_service
from app.services.inference_service import apply_suppression_rules, enlarge_bbox
from app.services.sort import ObjectTracker

logger = logging.getLogger(__name__)


def _slug(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "_")


class ScanSession:
    """One inspection run. There is a single active session at a time, matching
    the single-operator, single-conveyor nature of the machine."""

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        self.active = False
        self.sample_id = ""
        self.commodity = ""
        self.variety = ""
        self.batch: Dict = {}
        self.folder_name = ""
        self.output_folder = ""
        self.output_frame_folder = ""
        self.start_date = ""
        self.start_time = ""
        self.end_time = ""
        self.frame_count = 0
        self.saved_frame_count = 0

        # Cumulative unique detections for the whole run (legacy existing_track_ids).
        self.existing_track_ids = set()
        self.tracker = ObjectTracker(x_tolerance=10)
        self.tracker.update([[0, 0, 0, 0, 0, 0]], 0, (1200, 1920))

        # Detections awaiting an operator label, keyed by index.
        self.pending: List[Dict] = []
        self.pending_frame: Optional[np.ndarray] = None
        self.labelled_indices = set()

        # Legacy conveyor_stop_count (main.py:104, 2610-2618).
        self.conveyor_stop_count = {
            "fm_count": 0,
            "stop_count": 0,
            "fm_time": 0,
            "stop_time": 0,
            "total_fm_time": 0,
            "total_stop_time": 0,
        }

        self.analysis_parameters: List[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, sample_id: str, commodity: str, variety: str,
              analysis_parameters: List[str] = None, batch: Dict = None) -> Dict:
        """Begin a run. Port of start_process (main.py:741-790)."""
        with self._lock:
            self.reset()
            now = datetime.now()

            self.sample_id = sample_id
            self.commodity = commodity
            self.variety = variety
            self.batch = batch or {}
            self.analysis_parameters = list(analysis_parameters or [])
            self.start_date = now.strftime("%Y-%m-%d")
            self.start_time = now.strftime("%H:%M:%S")
            # image_unique_id in the Qualix datagram (main.py:1673).
            self.folder_name = f"{sample_id}_{now.strftime('%Y%m%d%H%M%S')}"

            root = settings.OUTPUT_DIR
            self.output_folder = os.path.join(
                root, "output", _slug(commodity), _slug(variety), self.folder_name
            )
            self.output_frame_folder = os.path.join(
                root, "output_frame", _slug(commodity), _slug(variety), self.folder_name
            )
            os.makedirs(self.output_folder, exist_ok=True)
            os.makedirs(self.output_frame_folder, exist_ok=True)

            self.active = True
            logger.info(
                "Scan started: sample=%s commodity=%s variety=%s folder=%s",
                sample_id, commodity, variety, self.output_folder,
            )
            return self.status()

    def stop_belt_manually(self):
        """Operator pressed STOP. Legacy counted this separately from FM stops
        and started the manual stop-time clock (main.py:1074-1075)."""
        with self._lock:
            self.conveyor_stop_count["stop_count"] += 1
            self.conveyor_stop_count["stop_time"] = time.time()
        conveyor_service.send("all_stop")

    def accumulate_stop_times(self):
        """Port of add_time_to_conveyor_stop_count (main.py:1592-1615)."""
        with self._lock:
            csc = self.conveyor_stop_count
            now = time.time()
            if csc["fm_time"]:
                csc["total_fm_time"] += now - csc["fm_time"]
                csc["fm_time"] = 0
            if csc["stop_time"]:
                csc["total_stop_time"] += now - csc["stop_time"]
                csc["stop_time"] = 0

    # ------------------------------------------------------------------
    # Frame path
    # ------------------------------------------------------------------

    def save_raw_frame(self, frame: np.ndarray):
        """Write r_frame_N.jpg at quality 95 (main.py:2413-2441).

        This is what update_fm_count counts as "Frame Count", and what the S3
        worker uploads. Without it that metric is always zero.
        """
        try:
            path = os.path.join(self.output_frame_folder, f"r_frame_{self.saved_frame_count}.jpg")
            encoded = cv2.imencode(
                ".jpg", cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), [cv2.IMWRITE_JPEG_QUALITY, 95]
            )[1]
            with open(path, "wb") as fh:
                fh.write(encoded.tobytes())
            self.saved_frame_count += 1
        except Exception as exc:
            logger.error("save_raw_frame failed: %s", exc)

    @staticmethod
    def _x_center(box) -> float:
        return (box[0] + box[2]) / 2.0

    def _has_similar_x_axis(self, boxes, x_threshold: int = 10) -> bool:
        """Port of has_similar_x_axis (main.py:2516-2563).

        Legacy compared against detections still queued for operator attention;
        `self.pending` is the equivalent here.
        """
        if not self.pending:
            return False
        new_centers = [self._x_center(b) for b in boxes if len(b) >= 4]
        if not new_centers:
            return False
        for item in self.pending:
            existing = self._x_center(item["box"])
            for nx in new_centers:
                if abs(nx - existing) < x_threshold:
                    return True
        return False

    def process_frame(self, frame: np.ndarray, detections: List) -> Dict:
        """Feed one inferred frame through the detection state machine.

        Returns a dict describing what the client should show.
        """
        # Idle preview (no scan started): stream frames but change no state.
        # The message shape stays identical so the client never has to branch.
        if not self.active:
            return self._snapshot(fm_detected=False)

        with self._lock:
            self.frame_count += 1
            h, w = frame.shape[:2]

            # 1. Commodity-specific suppression (process_results).
            detections, fm_flag = apply_suppression_rules(
                detections, self.commodity, self.variety
            )

            if not detections or not fm_flag:
                return self._snapshot(fm_detected=False)

            # 2. Pad boxes the way emit_results does before anything downstream
            #    sees them (main.py / GrabImage.py:577).
            boxes = [enlarge_bbox(b, pad=10, img_w=w, img_h=h) for b in detections]

            # 3. Track, then take only ids we have never seen in this run.
            self.tracker.update(detections, self.frame_count, (h, w))
            track_ids = list(self.tracker.get_tracked_objects().keys())
            new_ids = set(track_ids) - self.existing_track_ids

            # Deliberate deviation from legacy: a new physical object can only
            # appear under the camera if the belt is actually advancing, so
            # nothing "new" should be countable while machine_start is locked
            # (an FM stop already in effect, awaiting Resume/Forward/Submit).
            # Neither legacy's nor this port's inference loop is otherwise
            # gated on belt motion at all, so a stationary object sitting
            # under the camera during that stop can still generate fresh
            # tracker ids from ordinary frame-to-frame detection jitter (see
            # enhancements.md) — this closes that off at the source, not just
            # for the subset that happens to collide with something already
            # pending.
            if new_ids and conveyor_service.machine_start_locked:
                logger.info(
                    "FM detected but not counted or queued (belt already "
                    "locked/stopped): %s", new_ids
                )
                return self._snapshot(fm_detected=False)

            fm_detected = False
            if new_ids and not self._has_similar_x_axis(boxes, x_threshold=10):
                fm_detected = True
                self._on_foreign_matter(frame, boxes)
                # Deliberate deviation from legacy (main.py:2597-2622): only
                # count ids we actually surface for operator review. Legacy
                # adds every new id to existing_track_ids unconditionally,
                # even ones has_similar_x_axis suppressed from the pending
                # queue, so total_fo_detected silently inflates from tracker
                # jitter on a stationary object (see enhancements.md). An id
                # suppressed here stays out of existing_track_ids, so it's
                # still "new" on a later frame and gets a fair chance to be
                # counted once whatever it collided with in `pending` clears.
                self.existing_track_ids.update(new_ids)
            elif new_ids:
                logger.info(
                    "FM detected but not counted or queued (similar x-axis "
                    "already pending): %s", new_ids
                )

            return self._snapshot(fm_detected=fm_detected)

    def _on_foreign_matter(self, frame: np.ndarray, boxes: List):
        """Port of fm_control (main.py:2604-2650).

        Ordering matters: the interlock is engaged BEFORE FM_detected goes out,
        so a machine_start arriving in between cannot win the race.
        """
        csc = self.conveyor_stop_count
        csc["fm_count"] += 1
        csc["fm_time"] = time.time()

        conveyor_service.lock_machine_start(reason="FM_detected")
        conveyor_service.send("FM_detected")

        self.pending = [
            {"index": i, "box": [float(v) for v in box[:4]],
             "confidence": float(box[4]) if len(box) > 4 else None,
             "class_id": int(box[5]) if len(box) > 5 else None}
            for i, box in enumerate(boxes)
        ]
        self.pending_frame = frame.copy()
        self.labelled_indices = set()
        self.save_raw_frame(frame)

        logger.info("Foreign matter detected: %s box(es) awaiting operator label", len(boxes))

    # ------------------------------------------------------------------
    # Operator interaction
    # ------------------------------------------------------------------

    def label_detection(self, index: int, fm_name: str) -> Dict:
        """Operator tapped a box and chose an FM type.

        Port of ImageLabel.mousePressEvent -> crop_and_save -> submit_fm_type
        (main.py:232-259, 141-147, 1343-1360). The saved filename IS the record:
        create_results counts files by their prefix.
        """
        with self._lock:
            if not self.active:
                raise RuntimeError("No active scan")
            if self.pending_frame is None:
                raise RuntimeError("No frozen frame awaiting labels")
            item = next((p for p in self.pending if p["index"] == index), None)
            if item is None:
                raise KeyError(f"No pending detection with index {index}")
            if index in self.labelled_indices:
                return {"already_labelled": True, **self.pending_status()}

            x1, y1, x2, y2 = [int(round(v)) for v in item["box"]]
            h, w = self.pending_frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                raise ValueError("Degenerate bounding box; nothing to crop")

            crop = self.pending_frame[y1:y2, x1:x2]
            safe_name = fm_name.replace(" ", "_")
            filename = f"{safe_name}_{int(time.time() * 1000)}.png"
            path = os.path.join(self.output_folder, filename)
            # Legacy writes the crop through a BGR->RGB conversion (main.py:1361).
            cv2.imwrite(path, cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

            self.labelled_indices.add(index)
            logger.info("Labelled detection %s as %r -> %s", index, fm_name, filename)
            return {"saved": filename, **self.pending_status()}

    def save_unselected(self) -> int:
        """Boxes the operator did not classify are NON-FM (main.py:1325-1341)."""
        with self._lock:
            if self.pending_frame is None:
                return 0
            saved = 0
            for item in self.pending:
                if item["index"] in self.labelled_indices:
                    continue
                x1, y1, x2, y2 = [int(round(v)) for v in item["box"]]
                h, w = self.pending_frame.shape[:2]
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)
                if x2 <= x1 or y2 <= y1:
                    continue
                crop = self.pending_frame[y1:y2, x1:x2]
                path = os.path.join(
                    self.output_folder, f"NON-FM_{int(time.time() * 1000)}_{item['index']}.png"
                )
                cv2.imwrite(path, cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
                saved += 1
            return saved

    def resume(self) -> Dict:
        """Operator finished with the frozen frame — release the belt.

        Legacy unlocked from Submit (main.py:1291) and Forward (main.py:856,877).
        """
        with self._lock:
            self.save_unselected()
            self.accumulate_stop_times()
            self.pending = []
            self.pending_frame = None
            self.labelled_indices = set()
        conveyor_service.unlock_machine_start(reason="detection resolved")
        ok = conveyor_service.send("machine_start")
        return {"resumed": ok, **self.status()}

    def cancel(self) -> Dict:
        """Discard the run — archive its crops, don't delete them.

        Port of cancel_result (main.py:2033-2052): every file already saved to
        output_folder is moved into <OUTPUT_DIR>/rejected/<commodity>/<variety>/
        <folder_name>/ for later review, not deleted. commodity/variety are
        used here exactly as legacy did — NOT slugified, unlike output_folder's
        own path (main.py:2039 uses currentText() directly while start_process
        lowercases and underscores it at main.py:770) — a literal legacy
        inconsistency reproduced rather than "fixed".
        """
        with self._lock:
            sample_id = self.sample_id
            if self.output_folder and os.path.isdir(self.output_folder):
                rejected_folder = os.path.join(
                    settings.OUTPUT_DIR, "rejected", self.commodity, self.variety, self.folder_name
                )
                os.makedirs(rejected_folder, exist_ok=True)
                for name in os.listdir(self.output_folder):
                    src = os.path.join(self.output_folder, name)
                    if os.path.isfile(src):
                        shutil.move(src, rejected_folder)
            self.reset()
        return {"cancelled": True, "sample_id": sample_id}

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def create_results(self, blower_fo: int, magnetic_fo: int) -> Dict[str, int]:
        """Count saved crops by filename prefix. Port of create_results
        (main.py:1372-1452)."""
        params = list(self.analysis_parameters) + ["FM", "NON-FM"]
        counter = Counter()

        if os.path.isdir(self.output_folder):
            for name in os.listdir(self.output_folder):
                parts = name.split("_")
                stem = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
                for key in params:
                    if stem.startswith(key):
                        counter[key] += 1

        counter["Blower FO"] = blower_fo
        counter["Magnetic FO"] = magnetic_fo
        return dict(counter)

    def update_fm_count(self) -> Dict[str, float]:
        """The six looker_data metrics. Port of update_fm_count (main.py:1535-1563)."""
        frame_count = 0
        if os.path.isdir(self.output_frame_folder):
            frame_count = len(
                [f for f in os.listdir(self.output_frame_folder) if f.lower().endswith(".jpg")]
            )

        csc = self.conveyor_stop_count
        return {
            "Frame Count": frame_count,
            "FM Stop Count": csc["fm_count"],
            # stop_count is only ever incremented by stop_p() (main.py:1074),
            # which fires for BOTH the manual Stop button and Submit
            # (submit_video -> stop_p, main.py:1571). This "-1" discounts that
            # implicit submit-time stop so the metric reflects only stops the
            # operator actually chose mid-run (main.py:1554).
            "Manual Stop Count": max(0, csc["stop_count"] - 1),
            "FM Stop Time": round(csc["total_fm_time"], 3),
            "Manual Stop Time": round(csc["total_stop_time"], 3),
            "Total Stop Time": round(csc["total_stop_time"] + csc["total_fm_time"], 3),
        }

    def finish(self, blower_fo: int, magnetic_fo: int) -> Dict:
        """Close the run and assemble the result payload.

        Port of submit_video + submit_create_result (main.py:1566-1660),
        including writing result.json into the batch folder.
        """
        with self._lock:
            self.end_time = datetime.now().strftime("%H:%M:%S")
            self.accumulate_stop_times()

            data = self.create_results(blower_fo, magnetic_fo)
            looker = self.update_fm_count()
            total = sum(v for v in data.values() if isinstance(v, int))

            result_dict = {
                "sample_id": self.sample_id,
                "date": self.start_date,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "total_fo_detected": total,
                "result": data,
                "looker_data": looker,
            }

            try:
                os.makedirs(self.output_folder, exist_ok=True)
                with open(os.path.join(self.output_folder, "result.json"), "w") as fh:
                    json.dump(result_dict, fh, indent=4)
            except Exception as exc:
                logger.error("Could not write result.json: %s", exc)

            self.active = False
            logger.info(
                "Scan finished: sample=%s total_fo=%s unique_tracks=%s",
                self.sample_id, total, len(self.existing_track_ids),
            )
            return result_dict

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def pending_status(self) -> Dict:
        return {
            "pending": self.pending,
            "labelled": sorted(self.labelled_indices),
            "awaiting_label": [
                p["index"] for p in self.pending if p["index"] not in self.labelled_indices
            ],
        }

    def _snapshot(self, fm_detected: bool) -> Dict:
        return {
            "fm_detected": fm_detected,
            # Port of frame_fm_count (main.py:2667/2675, fed by fo_count =
            # len(coo) in GrabImage.py:621) — the count for the detection
            # instance currently on screen, which is what label_fm_count
            # actually displays live in legacy. NOT the same as
            # total_fo_detected below, which legacy only ever uses for the
            # final saved result/looker_data, never shown on this label.
            "frame_fm_count": len(self.pending),
            "total_fo_detected": len(self.existing_track_ids),
            "frame_count": self.frame_count,
            "machine_start_locked": conveyor_service.machine_start_locked,
            **self.pending_status(),
        }

    def status(self) -> Dict:
        return {
            "active": self.active,
            "sample_id": self.sample_id,
            "commodity": self.commodity,
            "variety": self.variety,
            "start_date": self.start_date,
            "start_time": self.start_time,
            "image_unique_id": self.folder_name,
            "output_folder": self.output_folder,
            "total_fo_detected": len(self.existing_track_ids),
            "frame_count": self.frame_count,
            "conveyor_stop_count": dict(self.conveyor_stop_count),
            "machine_start_locked": conveyor_service.machine_start_locked,
            **self.pending_status(),
        }


scan_session = ScanSession()
