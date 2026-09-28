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
    detection_queue      main.py:2581       backlog of detections not yet reviewed
    process_queue        main.py:2651-2706 drain it one at a time
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

import base64
import glob
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

# _COLOR_ORDER_NOTE — why the three save sites below write their frame/crop
# as-is, dropping the cv2.COLOR_BGR2RGB conversion legacy applies at each of
# its own (main.py:1361 submit_fm_type, main.py:2464 save_raw_image).
#
# Both pipelines debayer identically: cv2.COLOR_BAYER_RG2RGB, legacy at
# GrabImage.py:45, this port at camera_service.py:230. But legacy then swaps
# the channels a SECOND time before anything downstream sees the frame —
# GrabImage.py:308's `image_rgb = cv2.cvtColor(pic, cv2.COLOR_BGR2RGB)`,
# whose `img` copy is what emit_results hands to the crop/save path. So
# legacy's saved files net TWO swaps (an identity), while this port, which
# has no equivalent of that 308 conversion, netted only ONE — writing every
# crop and raw frame with red and blue transposed.
#
# Visible as a JET-heatmap look that got mistaken for the XAI view leaking
# into the gallery: the blue food-grade belt saved as brown, cream/tan
# objects as blue. Confirmed against legacy's own output/ crops (a blue belt
# with cream rice grains) and by R/B-swapping one of this port's crops, which
# reproduces exactly that.
#
# Dropping the conversion here restores legacy's NET behavior rather than
# changing it, and makes a saved crop match what the operator already sees
# live — the stream encodes the same frame with no conversion either
# (camera.py's encode_display), which is why the live view was always right
# while the files were not. Inference is unaffected: it runs on the
# unconverted frame in both codebases.


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

        # Cumulative unique detections for the whole run (legacy existing_track_ids)
        # — used only to dedupe the tracker's own ids frame to frame, so an
        # x-axis-suppressed duplicate isn't re-evaluated (and re-logged) on
        # every subsequent frame while it's still under the camera.
        self.existing_track_ids = set()
        # Cumulative ids actually queued for operator review (a strict subset
        # of existing_track_ids) — this, not existing_track_ids, is what
        # total_fo_detected counts. Deliberate deviation from legacy on
        # request: legacy's handle_detection (main.py:2618-2622) counts every
        # new track id whether or not has_similar_x_axis suppressed it from
        # review, which double-counts a duplicate detection of the same
        # physical object as if it were a second one — never photographed,
        # since save_unselected/label_detection only ever act on self.pending.
        # See enhancements.md.
        self.counted_track_ids = set()
        self.tracker = ObjectTracker(
            x_tolerance=settings.TRACK_X_TOLERANCE_PX,
            x_tolerance_ratio=settings.TRACK_X_TOLERANCE_RATIO,
            stale_after_seconds=settings.TRACK_STALE_AFTER_SECONDS,
        )
        self.tracker.update([[0, 0, 0, 0, 0, 0]], 0, (1200, 1920))

        # Detections awaiting an operator label, keyed by index. This is the
        # ONE detection currently on the review screen; anything found while
        # it is up waits in detection_queue below.
        self.pending: List[Dict] = []
        self.pending_frame: Optional[np.ndarray] = None
        self.labelled_indices = set()

        # Detections found but not yet reviewed, oldest first. Port of legacy's
        # detection_queue (main.py:2581) drained by process_queue
        # (main.py:2651-2706) one entry at a time, gated on the que_next flag
        # that submit_all_fo_new sets (main.py:1289).
        #
        # It exists because a detection does not stop the belt instantly: the
        # conveyor takes about a second to decelerate, and frames keep being
        # inferred for that whole window (pause_capture's own delay). Without a
        # queue each of those detections simply overwrites the previous one, so
        # only the last frame's objects are ever reviewed and everything found
        # earlier in the deceleration window is discarded unseen — not shown,
        # not cropped, not counted.
        self.detection_queue: List[Dict] = []

        # Bumped every time a different frame is put on the review screen. The
        # stream loop sends the frozen frame once and then holds it, so with a
        # queue draining behind a still-paused capture it needs something to
        # tell it the frame underneath the boxes has been replaced — otherwise
        # the next queued detection's boxes are drawn over the previous
        # detection's image.
        self.frozen_frame_seq = 0

        # Pending indices do not restart at 0 for each detection. The index is
        # baked into the crop filename (label_detection), and re-labeling globs
        # `*_<index>.png` to delete the crop it is replacing — with per-detection
        # numbering that glob matches the identically-numbered box of every
        # EARLIER detection in the run too, deleting already-saved crops that
        # belong to different objects.
        self._next_pending_index = 0

        # Deliberate deviation from legacy, not a port of anything in
        # main.py: after Submit, resume() clears machine_start_locked and
        # capture_paused so the live view returns, but the belt is still
        # physically stationary (Start hasn't been pressed). Legacy's own
        # tracker still ran inference in that window and could re-detect the
        # same still-in-frame object as "new" once frames resumed after the
        # capture-pause gap, re-locking the interlock before the operator
        # ever got to press Start. Confirmed live: the FM-review overlay was
        # reappearing on its own seconds after Submit. This flag skips
        # detection (not the live preview) until Start is pressed again, so
        # Submit reliably lands on — and stays on — the Start/Stop screen.
        # See enhancements.md.
        self.detection_suspended = False

        # Port of cam_thread.capture_paused (GrabImage.py:82/95). While set,
        # legacy's capture loop grabs nothing at all, so no frames reach
        # inference and the display stays frozen on the detection frame.
        self.capture_paused = False
        # Monotonic, never reused: a pause scheduled before this reset must
        # not be able to land after it.
        self._pause_token = getattr(self, "_pause_token", 0) + 1

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
        """Begin a run, or resume one already in progress.

        Port of start_process (main.py:751-839), which is guarded by
        self.start_time_flag: the FIRST Start press for a batch resets
        conveyor_stop_count and creates the output folders, but every
        SUBSEQUENT press for the same batch (e.g. resuming after an FM
        detection was dismissed via Submit) only accumulates stop time
        (add_time_to_conveyor_stop_count) — it must not wipe the batch's
        accumulated FM counts, tracker state, or output folder. `self.active`
        already being True is this port's equivalent of start_time_flag=True.
        """
        with self._lock:
            if self.active:
                self.accumulate_stop_times()
                self.resume_capture()
                self.detection_suspended = False
                logger.info("Scan resumed (already active): sample=%s", self.sample_id)
                return self.status()

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
            # start_process clears capture_paused (main.py:812/844).
            self.resume_capture()
            logger.info(
                "Scan started: sample=%s commodity=%s variety=%s folder=%s",
                sample_id, commodity, variety, self.output_folder,
            )
            return self.status()

    # ------------------------------------------------------------------
    # Camera capture pause — port of cam_thread.capture_paused
    # ------------------------------------------------------------------
    #
    # Legacy does not merely stop the belt when a detection freezes the
    # screen: it stops grabbing frames altogether. stop_camera_with_delay
    # (main.py:726-744) waits `delay_sec` for the conveyor to decelerate and
    # then sets cam_thread.capture_paused = True, which makes the capture loop
    # (GrabImage.py:95) sleep instead of grabbing, so nothing reaches
    # inference at all. It is set on FM detection (via update_fm_image,
    # main.py:983), on manual STOP (stop_p, main.py:1085) and after the
    # Forward jog re-locks (main.py:888); it is cleared on START
    # (main.py:812/844), on the Forward jog itself (main.py:868) and when a
    # detection is resolved (main.py:1324).
    #
    # This is what keeps a stopped belt from producing further detections in
    # legacy, and what keeps its frozen review frame aligned with its boxes.

    def pause_capture(self, delay_sec: float = 1.0):
        """Pause frame capture after `delay_sec` (conveyor deceleration)."""
        self._pause_token += 1
        token = self._pause_token

        def delayed_pause():
            time.sleep(delay_sec)
            # Unlike legacy's own delayed_stop thread, this checks it is still
            # the most recent request: without it, a resume landing inside the
            # delay window would be overwritten a moment later and freeze a
            # scan that had already been released, with nothing to unfreeze it.
            if self._pause_token == token:
                self.capture_paused = True
                logger.info("Camera capture paused after %.1fs", delay_sec)

        threading.Thread(target=delayed_pause, daemon=True).start()

    def resume_capture(self):
        """Resume frame capture immediately, cancelling any pending pause."""
        self._pause_token += 1
        if self.capture_paused:
            logger.info("Camera capture resumed")
        self.capture_paused = False

    def stop_belt_manually(self):
        """Operator pressed STOP. Legacy counted this separately from FM stops
        and started the manual stop-time clock (main.py:1074-1075)."""
        with self._lock:
            self.conveyor_stop_count["stop_count"] += 1
            self.conveyor_stop_count["stop_time"] = time.time()
        conveyor_service.send("all_stop")
        # stop_p also pauses capture (main.py:1085).
        self.pause_capture(delay_sec=1.0)

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
            # As-is, not through legacy's cv2.COLOR_BGR2RGB (main.py:2464) —
            # see _COLOR_ORDER_NOTE.
            encoded = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95]
            )[1]
            with open(path, "wb") as fh:
                fh.write(encoded.tobytes())
            self.saved_frame_count += 1
        except Exception as exc:
            logger.error("save_raw_frame failed: %s", exc)

    @staticmethod
    def _iou(a, b) -> float:
        ax1, ay1, ax2, ay2 = a[:4]
        bx1, by1, bx2, by2 = b[:4]
        iw = min(ax2, bx2) - max(ax1, bx1)
        ih = min(ay2, by2) - max(ay1, by1)
        if iw <= 0 or ih <= 0:
            return 0.0
        inter = iw * ih
        union = ((ax2 - ax1) * (ay2 - ay1)) + ((bx2 - bx1) * (by2 - by1)) - inter
        return inter / union if union > 0 else 0.0

    def _merge_overlapping_detections(self, detections: List) -> List:
        """Collapse boxes that describe the same physical object into one.

        The model's own NMS is class-wise: non_max_suppression offsets each
        box by its class before handing it to torchvision.ops.nms
        (run_inference.py:727, with the default agnostic=False), so it only
        ever suppresses overlaps WITHIN a class. An object the model cannot
        decide a class for therefore comes back as two boxes at the same
        coordinates under different class ids — confirmed live, e.g.
        [1412, 759, 1459, 813] returned as both class 3 @ 0.23 and class 2 @
        0.27 in one frame.

        Downstream, nothing else can tell those apart. The tracker gives them
        separate ids, both land in `pending`, and on screen they are one
        rectangle drawn exactly over another — so the operator taps once,
        labels one of them, and the other is saved as NON-FM by
        save_unselected. create_results counts files, so one piece of foreign
        matter is reported twice.

        The highest-confidence box wins, which is also what a class-agnostic
        NMS would have kept. Input order is preserved so the boxes the operator
        sees stay in the order they were detected.
        """
        if len(detections) < 2:
            return detections

        order = {id(d): i for i, d in enumerate(detections)}
        by_confidence = sorted(
            detections, key=lambda d: d[4] if len(d) > 4 else 0.0, reverse=True
        )
        kept = []
        for det in by_confidence:
            overlap = next(
                (k for k in kept
                 if self._iou(det, k) >= settings.DETECTION_MERGE_IOU), None)
            if overlap is None:
                kept.append(det)
            else:
                logger.info(
                    "Merged a duplicate box of one object: class %s @ %.2f "
                    "dropped, overlaps class %s @ %.2f (IoU %.2f) at %s",
                    det[5] if len(det) > 5 else "?", det[4] if len(det) > 4 else 0.0,
                    overlap[5] if len(overlap) > 5 else "?",
                    overlap[4] if len(overlap) > 4 else 0.0,
                    self._iou(det, overlap), [round(v, 1) for v in det[:4]],
                )
        kept.sort(key=lambda d: order[id(d)])
        return kept

    @staticmethod
    def _center(box) -> tuple:
        return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0

    def _novel_boxes(self, boxes, y_tolerance: int = 10) -> List:
        """Return only the boxes that are NOT already awaiting operator review.

        Derived from has_similar_x_axis (main.py:2516-2563), which compared the
        x-centre alone. The belt travels in +y (the tracker's own match gate,
        sort.py's `cy >= obj['y'] - 5`, and its `y <= height - 50` exit rule
        both depend on that), so one object trailing another along the belt has
        an x-centre near-identical to the leader's BY CONSTRUCTION. An x-only
        comparison therefore cannot tell "the same object, one frame later"
        apart from "a second object a few centimetres behind the first", and
        suppresses the second one — which is what made a closely-following
        object vanish: the suppression window is the 1s deceleration delay in
        pause_capture(), which is exactly when the trailing object slides into
        view, and process_frame records its track id as seen regardless, so it
        was never offered for review again after Resume either.

        The y comparison that fixes it is DIRECTIONAL rather than a second
        distance threshold. A given object can only ever move forward down the
        frame, so a box at the same x that sits at or ahead of a pending box
        (`ny >= ey - y_tolerance`, the tolerance absorbing per-frame centroid
        jitter) is that same object seen later. A box at the same x but BEHIND
        a pending one cannot be the same object at any frame interval, so it is
        a genuinely new one and is queued. A plain |ny - ey| threshold would not
        work here: across the deceleration window the same object legitimately
        travels a long way in y, so any threshold loose enough to still suppress
        it would also swallow a real trailing object.

        Filtering is per BOX rather than per frame. Legacy's has_similar_x_axis
        returns a single bool for the whole frame and handle_detection then
        drops every box in it (main.py:2578-2586), which cannot work once
        detections are queued: the object under review stays in view for the
        whole deceleration window, so every subsequent frame contains it, and a
        frame-level verdict would discard each of those frames entirely —
        including any genuinely new object that arrived in one of them. Keeping
        the already-queued object's box out while letting the new one through
        is what makes a backlog possible at all, and it also stops an object
        the operator has already been shown from being presented, cropped and
        counted a second time.
        """
        awaiting = [
            (self._center(b), b[2] - b[0]) for b in self._boxes_awaiting_review()
        ]
        novel = []
        for box in boxes:
            if len(box) < 4:
                continue
            nx, ny = self._center(box)
            width = box[2] - box[0]
            duplicate = False
            for (ex, ey), existing_width in awaiting:
                # Scaled to the object's own width, same reasoning as the
                # tracker's _x_tolerance_for: a large object's box breathes by
                # tens of pixels between frames, so a flat threshold lets the
                # same object through as a new one. Measured live, two reviews
                # of one 306px-wide object had centres 12px apart — past a flat
                # 10px, nowhere near 25% of its width.
                x_threshold = max(
                    settings.TRACK_X_TOLERANCE_PX,
                    settings.TRACK_X_TOLERANCE_RATIO * max(width, existing_width),
                )
                if abs(nx - ex) < x_threshold and ny >= ey - y_tolerance:
                    duplicate = True
                    break
            if not duplicate:
                novel.append(box)
        return novel

    def _boxes_awaiting_review(self):
        """Every box the operator still has to look at — the one on screen plus
        the whole queued backlog. Legacy's has_similar_x_axis (main.py:2521)
        checks `self.detection_queue`, not just the detection being displayed,
        so a duplicate arriving while several are already queued is matched
        against all of them."""
        for item in self.pending:
            yield item["box"]
        for queued in self.detection_queue:
            for box in queued["boxes"]:
                yield box

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

            # Live view only, no detection, until Start is pressed again —
            # see detection_suspended's comment in reset().
            if self.detection_suspended:
                return self._snapshot(fm_detected=False)

            h, w = frame.shape[:2]

            # 1. Commodity-specific suppression (process_results).
            detections, fm_flag = apply_suppression_rules(
                detections, self.commodity, self.variety
            )

            if not detections or not fm_flag:
                return self._snapshot(fm_detected=False)

            # One box per object before anything downstream sees them — the
            # tracker, the counted ids and the operator's boxes all come off
            # this list. See _merge_overlapping_detections.
            detections = self._merge_overlapping_detections(detections)

            # 2. Pad boxes the way emit_results does before anything downstream
            #    sees them (main.py / GrabImage.py:577) — legacy uses pad=10,
            #    but the saved crops (label_detection/save_unselected cut
            #    directly from this same padded box, see their own comments)
            #    were reported as too tightly cropped to read clearly, so this
            #    was widened to 30. At 30 the object turned out to fill only
            #    about a third of its own crop — a ~30px object in a ~90px
            #    image, the rest belt — making it small and hard to read in
            #    the preview. Settled at 20 on request: a deliberate deviation
            #    from legacy's value in either direction, not a bug fix; see
            #    enhancements.md. Note this is the ONLY lever on apparent crop
            #    clarity — the object is only ~30-55 real sensor pixels, so
            #    less padding makes it render bigger, never sharper.
            #    Also sets the tap-to-classify overlay box shown live on the
            #    frozen frame, since both draw from this same list.
            boxes = [enlarge_bbox(b, pad=20, img_w=w, img_h=h) for b in detections]

            # 3. Track, then take only ids we have never seen in this run.
            self.tracker.update(detections, self.frame_count, (h, w))
            track_ids = list(self.tracker.get_tracked_objects().keys())
            new_ids = set(track_ids) - self.existing_track_ids

            # No belt-motion check here, deliberately. It is tempting (and an
            # earlier version of this file did it) to refuse new ids while
            # machine_start is locked, since a stopped belt cannot deliver new
            # material. But legacy locks machine_start on purpose during the
            # Forward jog and keeps capturing for that window (main.py:886 +
            # 868/888) — that window is exactly when the nudged-forward
            # material is supposed to be detected — so a lock-based gate would
            # break Forward outright. What actually stops a stopped belt from
            # producing detections in legacy is capture_paused: no frames are
            # grabbed at all, so process_frame is never reached. See
            # pause_capture above and the stream loop in app/api/camera.py.

            # Diagnostic for a foreign-object count that moves while the
            # material does not. A count can only grow when an id appears that
            # `existing_track_ids` has never seen, and on a stopped belt that
            # can only mean the tracker dropped a track and re-created it for
            # the same physical object — so both halves are logged together:
            # which ids are new this frame, and which were evicted (with the
            # rule that evicted them) on the way here. Remove once root-caused.
            if new_ids or self.tracker.last_evicted:
                logger.info(
                    "Track churn: new=%s evicted=%s tracked=%s counted_so_far=%s",
                    sorted(new_ids), self.tracker.last_evicted,
                    sorted(track_ids), len(self.counted_track_ids),
                )

            # Identity comes from the tracker, not from comparing boxes. An
            # object the operator has already been shown keeps its track id for
            # as long as it stays in view, so that id is what says "do not show
            # this one again" — and it keeps saying it after the operator
            # submits, which is exactly when a coordinate comparison stops
            # working. Confirmed live on batch T1179058xxxx: track 2 was shown,
            # the operator submitted, a different object (track 3) arrived a
            # moment later, and track 2 — still listed as tracked, never
            # evicted — was put on screen a second time along with it, because
            # by then nothing was awaiting review to compare its box against.
            assignment = self.tracker.last_assignment
            candidates = []
            for i, box in enumerate(boxes):
                track_id = assignment[i] if i < len(assignment) else None
                if track_id is not None and track_id in self.counted_track_ids:
                    continue
                candidates.append((track_id, box))

            # Geometry is now only a backstop, for the two cases an id cannot
            # cover: a detection the tracker refused to track at all (it gives
            # no id to anything against the left edge), and an object whose
            # track was dropped and re-minted, which arrives wearing an id
            # nobody has seen before.
            novel = self._novel_boxes([box for _, box in candidates])
            keep = {id(box) for box in novel}
            candidates = [(t, b) for t, b in candidates if id(b) in keep]

            # Nothing new is accepted while the backlog is at its limit. The
            # objects behind it are deliberately NOT marked as counted, so they
            # stay unseen ids and are detected again on a later frame once the
            # operator has worked the queue down — refusing to queue costs a
            # short delay, whereas marking them counted would lose them for the
            # rest of the scan.
            if candidates and len(self.detection_queue) >= settings.DETECTION_QUEUE_MAX:
                logger.warning(
                    "Detection backlog is at its limit of %s — holding %s "
                    "further object(s) until the operator works through it. "
                    "They stay uncounted and will be detected again.",
                    settings.DETECTION_QUEUE_MAX, len(candidates),
                )
                candidates = []

            fm_detected = False
            if candidates:
                fm_detected = True
                self._on_foreign_matter(frame, [box for _, box in candidates])
                # Only objects actually put in front of the operator count
                # toward total_fo_detected — see counted_track_ids' comment
                # above — and recording them here is also what stops each of
                # them being shown again.
                self.counted_track_ids.update(
                    t for t, _ in candidates if t is not None
                )
            elif new_ids:
                logger.info(
                    "New object(s) %s detected but not queued for review — "
                    "already shown, or overlapping something awaiting review",
                    sorted(new_ids),
                )

            # existing_track_ids still accumulates every new id regardless
            # (queued or suppressed) — purely so a suppressed duplicate isn't
            # treated as "new" again on the very next frame while the same
            # physical object is still under the camera.
            self.existing_track_ids.update(track_ids)

            return self._snapshot(fm_detected=fm_detected)

    def _on_foreign_matter(self, frame: np.ndarray, boxes: List):
        """Queue one detection, and put it on screen if nothing else is there.

        Port of handle_detection's enqueue step (main.py:2581): finding
        something only ever ADDS to the backlog. Stopping the belt and showing
        the frozen frame belong to _promote_next_detection below, which is
        legacy's fm_control (main.py:2604-2650) — in legacy those run from the
        process_queue thread at pop time, not at detection time.
        """
        self.detection_queue.append({
            "frame": frame.copy(),
            "boxes": [list(box) for box in boxes],
        })
        # One raw frame per detection, queued or not — this is what
        # update_fm_count reports as "Frame Count".
        self.save_raw_frame(frame)

        if self.pending_frame is None:
            self._promote_next_detection(immediate=False)
        else:
            logger.info(
                "Foreign matter detected while another detection is under "
                "review — queued behind it (%s waiting): %s box(es)",
                len(self.detection_queue), len(boxes),
            )

    def _promote_next_detection(self, immediate: bool) -> bool:
        """Move the oldest queued detection onto the review screen.

        Returns False when the queue is empty, which is the caller's signal
        that the backlog is drained and the live view can come back.

        `immediate` controls when the stream freezes. On the first detection
        of a burst the belt is still running, so capture is paused a second
        later once it has decelerated — the same delay legacy applies via
        stop_camera_with_delay (main.py:2699). Draining the rest of the queue
        happens with the belt already stopped (Submit never restarts it, see
        resume), so there is nothing to wait for and a delay would only show
        the operator a second of live feed over the frame they are meant to be
        reviewing.
        """
        if not self.detection_queue:
            self.pending = []
            self.pending_frame = None
            self.labelled_indices = set()
            return False

        item = self.detection_queue.pop(0)
        boxes = item["boxes"]

        csc = self.conveyor_stop_count
        csc["fm_count"] += 1
        csc["fm_time"] = time.time()

        # Ordering matters: the interlock is engaged BEFORE FM_detected goes
        # out, so a machine_start arriving in between cannot win the race.
        conveyor_service.lock_machine_start(reason="FM_detected")
        conveyor_service.send("FM_detected")

        # Ordered by how far each object has travelled down the belt, furthest
        # first. Within one frozen frame every box was found at the same
        # instant, so there is no "found first" among them — but the one
        # furthest along entered the camera's view earliest, which is the same
        # order the operator watched them arrive in. Left alone, the order is
        # whatever the model's NMS emitted, which is by confidence: the numbers
        # on the crops would then follow how sure the model was rather than
        # anything the operator can see, and the reclassify gallery lists
        # unlabelled crops in exactly this order.
        boxes = sorted(boxes, key=lambda b: (b[1] + b[3]) / 2.0, reverse=True)

        self.pending = []
        for box in boxes:
            self.pending.append({
                "index": self._next_pending_index,
                "box": [float(v) for v in box[:4]],
                "confidence": float(box[4]) if len(box) > 4 else None,
                "class_id": int(box[5]) if len(box) > 5 else None,
            })
            self._next_pending_index += 1
        # Temporary diagnostic for the count-vs-visible-boxes discrepancy
        # (confirmed NOT a cropping issue — object-fit: fill already shows the
        # whole frame). Logging raw coordinates so the next occurrence shows
        # whether the "missing" box is a near-duplicate overlapping another
        # (renders as one box) or has degenerate/out-of-range size. Remove
        # once root-caused — see enhancements.md / 9 -
        # post_remediation_session_log.md.
        logger.info(
            "Pending box coordinates: %s",
            [(p["index"], p["box"], round(p["box"][2] - p["box"][0], 1),
              round(p["box"][3] - p["box"][1], 1), p["confidence"], p["class_id"])
             for p in self.pending],
        )
        self.pending_frame = item["frame"]
        self.frozen_frame_seq += 1
        self.labelled_indices = set()

        if immediate:
            # Cancel any pause already scheduled and freeze right now — see the
            # docstring. _pause_token is bumped so a pause queued before this
            # promotion cannot land afterwards and re-freeze a released scan.
            self._pause_token += 1
            self.capture_paused = True
        else:
            # update_fm_image pauses capture 1s later, once the belt has
            # decelerated (main.py:983) — from here on the operator reviews a
            # frozen frame and no further inference runs until they resolve it.
            self.pause_capture(delay_sec=1.0)

        logger.info(
            "Foreign matter on screen: %s box(es) awaiting operator label "
            "(%s more detection(s) queued behind it)",
            len(boxes), len(self.detection_queue),
        )
        return True

    # ------------------------------------------------------------------
    # Operator interaction
    # ------------------------------------------------------------------

    def label_detection(self, index: int, fm_name: str) -> Dict:
        """Operator tapped a box and chose an FM type.

        Port of ImageLabel.mousePressEvent -> crop_and_save -> submit_fm_type
        (main.py:232-259, 141-147, 1343-1360). The saved filename IS the record:
        create_results counts files by their prefix.

        Re-labeling an index already marked is allowed, on request — the
        operator can change their mind about a box's FM type while still on
        the same frozen frame, before Resume. legacy (and this port,
        originally) refused a second tap on an already-labeled box outright.
        The old crop is deleted first (matched by its filename's trailing
        _<index>.png, which every crop for this index ends with regardless of
        FM type or timestamp) so a changed mind doesn't leave the previous,
        now-wrong classification also counted — counting is filename-prefix
        based (create_results), so a stale file left behind would silently
        double-count this one object under two different types.
        """
        with self._lock:
            if not self.active:
                raise RuntimeError("No active scan")
            if self.pending_frame is None:
                raise RuntimeError("No frozen frame awaiting labels")
            item = next((p for p in self.pending if p["index"] == index), None)
            if item is None:
                raise KeyError(f"No pending detection with index {index}")

            relabelling = index in self.labelled_indices
            if relabelling:
                for old_path in glob.glob(
                    os.path.join(self.output_folder, f"*_{index}.png")
                ):
                    try:
                        os.remove(old_path)
                        logger.info(
                            "Re-labeling detection %s — removed its previous "
                            "crop %s before saving the new one.",
                            index, os.path.basename(old_path),
                        )
                    except OSError as exc:
                        logger.error(
                            "Could not remove previous crop %s while "
                            "re-labeling detection %s: %s",
                            old_path, index, exc,
                        )

            x1, y1, x2, y2 = [int(round(v)) for v in item["box"]]
            h, w = self.pending_frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                raise ValueError("Degenerate bounding box; nothing to crop")

            crop = self.pending_frame[y1:y2, x1:x2]
            safe_name = fm_name.replace(" ", "_")
            # Nanosecond timestamp AND the box's own index, not just a
            # millisecond timestamp: confirmed live, two boxes on the same
            # reviewed frame tapped in quick succession landed in the same
            # millisecond, giving two different crops (different FM types,
            # different files) the exact same timestamp suffix — and since
            # that suffix is also this crop's own object_id (see
            # _crop_object_id), the collision showed up as two entirely
            # different objects both displaying as the same "Object N" in
            # the reclassify gallery. Nanosecond resolution alone made a
            # repeat far less likely but still isn't a real guarantee (clock
            # resolution isn't specified/guaranteed by the platform); the
            # box index costs nothing and rules it out deterministically,
            # since two boxes from the very same label_detection pass always
            # have distinct indices. history.py's get_result_images derives
            # its own display fm_type generically (strips ALL trailing
            # underscore-separated numeric tokens, not just one) specifically
            # so this extra token doesn't need any matching change there.
            filename = f"{safe_name}_{time.time_ns()}_{index}.png"
            path = os.path.join(self.output_folder, filename)
            # Written as-is, NOT through the cv2.COLOR_BGR2RGB conversion
            # legacy's submit_fm_type applies (main.py:1361) — see
            # _COLOR_ORDER_NOTE above for why copying that line literally
            # swapped red and blue in every saved crop.
            cv2.imwrite(path, crop)

            self.labelled_indices.add(index)
            logger.info(
                "%s detection %s as %r -> %s",
                "Re-labelled" if relabelling else "Labelled", index, fm_name, filename,
            )
            return {"saved": filename, "relabelled": relabelling, **self.pending_status()}

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
                # Nanosecond, same reasoning as label_detection's own
                # filename above — the box index already disambiguates
                # different boxes from the same frame, but not two
                # unclassified boxes across two frames swept within the
                # same millisecond.
                path = os.path.join(
                    self.output_folder, f"NON-FM_{time.time_ns()}_{item['index']}.png"
                )
                # As-is, same as label_detection — see _COLOR_ORDER_NOTE.
                cv2.imwrite(path, crop)
                saved += 1
            return saved

    def resume(self) -> Dict:
        """Operator dismissed the frozen frame — release the interlock so
        Start is allowed again, but do NOT restart the belt.

        Port of submit_all_fo_new (main.py:1310-1348): it unlocks
        machine_start_locked and clears capture_paused (live view returns),
        but every line that would actually send machine_start or restart the
        conveyor/camera threads is commented out in legacy. The belt only
        moves again once the operator explicitly presses Start, which is
        start_process (main.py:751-839) — a genuinely different function.
        """
        with self._lock:
            self.save_unselected()
            self.accumulate_stop_times()
            self.pending = []
            self.pending_frame = None
            self.labelled_indices = set()

            # Legacy's submit_all_fo_new sets que_next = True (main.py:1289),
            # which lets the process_queue thread pop the next detection rather
            # than returning to the live view. The operator keeps reviewing
            # until the backlog captured while the belt was decelerating is
            # drained; only then does the live feed and its Start/Stop sidebar
            # come back. Shown immediately, with no settling delay: legacy
            # sleeps 0.41-0.56s before each pop (main.py:2672-2696) to let the
            # belt carry the object to the pickup position, but the belt is
            # already stopped by the time a QUEUED item is promoted — Submit
            # unlocks the interlock without ever sending machine_start — so
            # here that delay would buy nothing but a blank screen.
            if self._promote_next_detection(immediate=True):
                return {"resumed": True, **self.status()}

            # See the flag's own comment in reset(): suspend detection until
            # Start is pressed again, so the live view can safely return
            # without immediately re-locking on the same still-in-frame object.
            self.detection_suspended = True
        conveyor_service.unlock_machine_start(reason="detection resolved")
        # Legacy clears capture_paused as part of resolving the detection
        # (main.py:1324), which is what restarts the live feed.
        self.resume_capture()
        return {"resumed": True, **self.status()}

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
        (main.py:1372-1452).

        dict.fromkeys, not a plain list: legacy iterates
        `filename_mapping.items()`, a DICT comprehension over
        `analysis_parameters` (main.py:1443-1457), so a name appearing twice
        in that list collapses to one key and is matched once. This port
        originally iterated the list itself, and since most commodities
        already carry "FM" in their own analysis vocabulary, the hardcoded
        + ["FM", ...] below made "FM" appear twice — counting every
        FM-prefixed crop twice, inflating both that row and total_fo_detected
        (which is summed from these values) in the saved record AND in the
        Qualix datagram. Confirmed on batch milind4550: 2 FM crops on disk
        stored as "FM": 4, total 247 against 245 real crops. Restores legacy's
        own behavior rather than changing it.

        The inner loop deliberately does NOT break on first match, matching
        legacy exactly: if one param were a prefix of another, legacy counts
        the file under both. No commodity's vocabulary currently has such a
        pair, so this is theoretical — but it is legacy's semantics, so it is
        left alone.
        """
        params = list(dict.fromkeys(list(self.analysis_parameters) + ["FM", "NON-FM"]))
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

    def _crop_fm_type(self, name: str) -> str:
        """Reverse create_results' own prefix match for one filename, so the
        reclassify gallery can label each crop with the type it's currently
        counted as — same stem-splitting logic, applied to one name instead
        of a whole directory listing."""
        params = sorted(list(self.analysis_parameters) + ["FM", "NON-FM"], key=len, reverse=True)
        parts = name.split("_")
        stem = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
        return next((key for key in params if stem.startswith(key)), "NON-FM")

    def _crop_object_id(self, name: str) -> str:
        """The part of a crop's filename that is NOT its FM-type prefix —
        e.g. `1789033003109` for `Insect_1789033003109.png`, or
        `1789033003109_0` for `NON-FM_1789033003109_0.png` (save_unselected
        appends the box index too, to disambiguate multiple unclassified
        boxes from the same frame). This is the one part of the filename
        that's genuinely unique to this specific captured object — unlike
        the FM-type prefix, which is exactly what a reclassify changes.
        relabel_crop preserves it across a rename for that reason: it's the
        closest thing this design has to a real, stable per-object name.
        """
        fm_type = self._crop_fm_type(name)
        stem_no_ext = name.rsplit(".", 1)[0]
        prefix = fm_type.replace(" ", "_") + "_"
        if stem_no_ext.startswith(prefix):
            return stem_no_ext[len(prefix):]
        return stem_no_ext

    def list_pending_crops(self) -> List[Dict]:
        """Crops saved so far for the batch on the review screen — after
        Submit, before Confirm/Discard — so the operator can reclassify one
        before saving. Not a legacy feature (legacy has no reclassify path
        at all, live or at submit); see enhancements.md.

        Ordered by file mtime, EARLIEST captured first (on request — Object 1
        is the first object identified this scan, Object N the most recent;
        see ReclassifyObjects.jsx's own objectNumberById) — not by name:
        relabel_crop renames the file (new FM-type prefix), and a plain
        alphabetical `sorted(os.listdir(...))` would then reshuffle that
        crop to wherever its new name happens to sort — confirmed live,
        reported as objects visibly changing position on every reclassify.
        os.rename() does not touch a file's mtime (only its ctime), so
        sorting by mtime keeps every crop in its original capture order
        regardless of how many times it's since been renamed.
        """
        with self._lock:
            folder = self.output_folder
            if not folder or not os.path.isdir(folder):
                return []
            crops = []
            for name in os.listdir(folder):
                if not name.lower().endswith((".png", ".jpg", ".jpeg")):
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                with open(path, "rb") as fh:
                    data = base64.b64encode(fh.read()).decode("ascii")
                mime = "png" if name.lower().endswith(".png") else "jpeg"
                crops.append({
                    "name": name,
                    "fm_type": self._crop_fm_type(name),
                    "object_id": self._crop_object_id(name),
                    "data_uri": f"data:image/{mime};base64,{data}",
                    "_mtime": os.path.getmtime(path),
                })
            crops.sort(key=lambda c: c["_mtime"])
            for crop in crops:
                del crop["_mtime"]
            return crops

    def relabel_crop(self, name: str, fm_name: str) -> str:
        """Reclassify one already-saved crop before the batch is confirmed.

        The filename IS the classification record (see label_detection) —
        create_results just re-counts the directory afterward, so renaming
        the file is the whole operation. New to this port; legacy has no
        equivalent (mousePressEvent, main.py:219-246, no-ops on an
        already-labelled box and never revisits it, even at submit).

        Keeps the SAME object_id suffix (see _crop_object_id) rather than
        minting a fresh timestamp — confirmed live, the operator can tell a
        crop's file has its own name (e.g. the `..._1789033003109_0.png` in
        its path) and reasonably expects reclassifying it to change what
        it's called, not what it IS. Only the FM-type prefix changes; the
        part of the name that actually identifies this specific captured
        object stays fixed, so it can be reclassified any number of times
        and still be recognized as the same object throughout.
        """
        valid = set(self.analysis_parameters) | {"NON-FM"}
        if fm_name not in valid:
            raise ValueError(f"{fm_name!r} is not a valid FM type for this commodity")

        with self._lock:
            folder = self.output_folder
            if not folder or not os.path.isdir(folder):
                raise FileNotFoundError("No batch folder to reclassify in")
            # Ignore any directory component a caller might pass — this must
            # only ever touch a file directly inside output_folder.
            safe_source = os.path.basename(name)
            src = os.path.join(folder, safe_source)
            if not os.path.isfile(src):
                raise FileNotFoundError(f"No such crop: {name}")

            object_id = self._crop_object_id(safe_source)
            safe_name = fm_name.replace(" ", "_")
            new_name = f"{safe_name}_{object_id}.png"
            dest = os.path.join(folder, new_name)
            if dest != src:
                os.rename(src, dest)
            logger.info("Reclassified crop %s -> %s (%s)", safe_source, new_name, fm_name)
            return new_name

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
            # Belt speed as the camera saw it. Everything that decides whether
            # an object can cross the view unseen is in pixels: at S px/s an
            # object is in frame for (frame height / S) seconds, which times
            # the detection rate is how many times it was looked at. The drive
            # frequency on the VFD cannot be read from here, so this is the
            # only record of the speed a given batch actually ran at.
            speed = self.tracker.belt_speed_px_s
            logger.info(
                "Scan finished: sample=%s total_fo=%s reviewed_tracks=%s "
                "unique_tracks=%s belt=%s",
                self.sample_id, total,
                len(self.counted_track_ids), len(self.existing_track_ids),
                "%.0f px/s (%.0f ms in view)" % (speed, 1200.0 / speed * 1000)
                if speed else "not measured",
            )
            return result_dict

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def pending_status(self) -> Dict:
        return {
            "pending": self.pending,
            # How many MORE detections are waiting behind the one on screen.
            # The client uses this to keep the review overlay up and to tell
            # the operator there is more to come, instead of treating Submit as
            # always meaning "back to the live view".
            "queue_depth": len(self.detection_queue),
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
            "total_fo_detected": len(self.counted_track_ids),
            "frame_count": self.frame_count,
            "machine_start_locked": conveyor_service.machine_start_locked,
            "capture_paused": self.capture_paused,
            **self.pending_status(),
        }

    def live_state(self) -> Dict:
        """State without a frame, for the stream loop while capture is paused.

        Deliberately does not take self._lock: it only reads, and the stream
        loop must never block behind an operator action mid-frame.
        """
        return self._snapshot(fm_detected=False)

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
            "total_fo_detected": len(self.counted_track_ids),
            "frame_count": self.frame_count,
            "conveyor_stop_count": dict(self.conveyor_stop_count),
            "machine_start_locked": conveyor_service.machine_start_locked,
            **self.pending_status(),
        }


scan_session = ScanSession()
