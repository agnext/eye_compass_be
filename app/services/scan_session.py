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

import atexit
import base64
import glob
import queue
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


def _fmt_boxes(boxes: List) -> str:
    """Detections as legacy printed them: (x1, y1, x2, y2, conf, class).

    Legacy logged the raw tuple, so the confidence came out at full float
    precision (`0.5370410680770874`). Rounded to 3 places here — the extra
    digits are noise from the model's float32 output, and the line is much
    easier to scan without them. Coordinates stay exact.
    """
    out = []
    for b in boxes:
        if len(b) >= 6:
            out.append("({}, {}, {}, {}, {:.3f}, {})".format(
                int(b[0]), int(b[1]), int(b[2]), int(b[3]), float(b[4]), int(b[5])))
        elif len(b) >= 4:
            out.append("({}, {}, {}, {})".format(
                int(b[0]), int(b[1]), int(b[2]), int(b[3])))
    return "[" + ", ".join(out) + "]"


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


def _numbering_on_disk(output_folder: str, output_frame_folder: str) -> Dict[str, int]:
    """The next free number of each kind of file a run writes, from its folders.

    Used by ScanSession.restore() so a continued run never reuses a number:
        pending_index  one past the highest crop index (`..._<index>.png` in
                       output/, `frame_<index>` in fm_full_frames/)
        frame          one past the highest fm/ frame number
        r_frame        one past the highest r_frame_<n>.jpg
        crops          how many crops exist — the FMs reviewed so far
    """
    out = {"pending_index": 0, "frame": 0, "r_frame": 0, "crops": 0}

    def trailing_int(stem: str):
        tail = stem.rsplit("_", 1)[-1]
        return int(tail) if tail.isdigit() else None

    if output_folder and os.path.isdir(output_folder):
        for name in os.listdir(output_folder):
            stem, ext = os.path.splitext(name)
            if ext.lower() not in (".png", ".jpg", ".jpeg"):
                continue
            out["crops"] += 1
            n = trailing_int(stem)
            if n is not None:
                out["pending_index"] = max(out["pending_index"], n + 1)

    if output_frame_folder and os.path.isdir(output_frame_folder):
        for name in os.listdir(output_frame_folder):
            stem, ext = os.path.splitext(name)
            if stem.startswith("r_frame_") and ext.lower() == ".jpg":
                n = trailing_int(stem)
                if n is not None:
                    out["r_frame"] = max(out["r_frame"], n + 1)
        for sub, key in (("fm", "frame"), (os.path.join("fm", "low_confidence_frames"), "frame"),
                         ("fm_full_frames", "pending_index")):
            folder = os.path.join(output_frame_folder, sub)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                n = trailing_int(os.path.splitext(name)[0])
                if n is not None:
                    out[key] = max(out[key], n + 1)
    return out


def archive_crops(output_folder: str, commodity: str, variety: str, folder_name: str) -> None:
    """Move a discarded run's crops to rejected/, or delete them.

    Port of cancel_result (main.py:2033-2052): every file directly in
    output_folder goes to <OUTPUT_DIR>/rejected/<commodity>/<variety>/
    <folder_name>/. commodity/variety are used raw, NOT slugified, exactly as
    legacy did (main.py:2039) — a literal legacy inconsistency reproduced
    rather than "fixed". With REJECTED_SAVE_ENABLED=false they are deleted
    instead. output_frame/ is never touched, in either case.

    Shared by Cancel on a live batch and Discard on a held one.
    """
    if not output_folder or not os.path.isdir(output_folder):
        return
    if settings.REJECTED_SAVE_ENABLED:
        rejected_folder = os.path.join(
            settings.OUTPUT_DIR, "rejected", commodity, variety, folder_name,
        )
        os.makedirs(rejected_folder, exist_ok=True)
        for name in os.listdir(output_folder):
            src = os.path.join(output_folder, name)
            if os.path.isfile(src):
                shutil.move(src, rejected_folder)
    else:
        for name in os.listdir(output_folder):
            src = os.path.join(output_folder, name)
            if os.path.isfile(src):
                os.remove(src)
        logger.info(
            "Batch %s discarded; crops deleted (REJECTED_SAVE_ENABLED=false)",
            folder_name,
        )


def _write_fm_triple(stem: str, image: np.ndarray, detections: List) -> None:
    """Write <stem>.png / .txt / .conf — one frame's training artifacts.

    Runs on the frame-writer thread; see save_fm_training_artifacts for what
    these files are and why they exist. The .txt is YOLO format normalised
    against this image's own dimensions, deliberately without the confidence,
    and the .conf carries the confidences on their own in the same row order,
    exactly as legacy split them (main.py:2510-2525) — a label file a training
    run can read unmodified, with the model's certainty kept alongside it
    rather than mixed in.
    """
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    img_h, img_w = image.shape[:2]
    encoded = cv2.imencode(
        ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, settings.FM_FRAME_PNG_COMPRESSION]
    )[1]
    with open(stem + ".png", "wb") as fh:
        fh.write(encoded.tobytes())
    with open(stem + ".txt", "w") as txt, open(stem + ".conf", "w") as conf:
        for det in detections:
            x1, y1, x2, y2, confidence, class_id = det[:6]
            w = (x2 - x1) / img_w
            h = (y2 - y1) / img_h
            x = (x1 + x2) / 2 / img_w
            y = (y1 + y2) / 2 / img_h
            txt.write(f"{int(class_id)} {x:.6f} {y:.6f} {w:.6f} {h:.6f}\n")
            conf.write(f"{float(confidence):.6f}\n")


# A crop's filename IS the record — create_results counts files by their FM-type
# prefix — so the FM name has to survive a round trip through the filesystem.
# Spaces have always been written as underscores. A forward slash cannot be
# written at all: it is the path separator, so "Insects/Pest_<ts>_<i>.png" asks
# for a file called "Pest_..." inside a directory called "Insects" that does not
# exist, and cv2.imwrite quietly returns False. The operator labels the object,
# the call succeeds, and the crop is never written — the object is lost, not
# merely miscounted. Reported live on 29 Sep after the Qualix config gained
# "Insects/Pest" and "Mould/Fungus".
#
# "~" is the stand-in for a separator: it is legal in a filename, and no FM name
# contains it, so the mapping reverses without ambiguity (unlike "-", which
# NON-FM already uses). Every writer and every reader goes through this pair.
_FM_NAME_SEPARATORS = ("/", "\\", os.sep)


def fm_filename_token(fm_name: str) -> str:
    """The form of an FM name that is safe to put in a filename."""
    token = fm_name.replace(" ", "_")
    for sep in _FM_NAME_SEPARATORS:
        if sep:
            token = token.replace(sep, "~")
    return token


def fm_name_from_token(token: str) -> str:
    """Reverse fm_filename_token, for display."""
    return token.replace("~", "/").replace("_", " ")


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
        # Frames that had nothing in them, counted so every RAW_FRAME_EVERY-th
        # one is kept. Separate from frame_count, which counts every frame:
        # throttling on that one would tie how many clean frames are saved to
        # how many detections happened, which is the coupling legacy's two
        # mutually exclusive branches specifically avoid.
        self._clean_frame_count = 0
        # Two background writers, each a queue + a daemon thread created on
        # first use and surviving reset() so a thread is never orphaned
        # mid-write by starting a new scan. They are SEPARATE on purpose:
        #
        #   jpg  — r_frame_N.jpg, the clean-belt frames. update_fm_count counts
        #          these for "Frame Count", so Submit must wait for them to
        #          land (flush_raw_frames) before it reports that number. Fast:
        #          ~32 ms a frame, and few of them.
        #   png  — the fm/ and fm_full_frames/ training triples. Nothing
        #          user-facing reads these before they are written: Cancel does
        #          not move output_frame/ at all, and the S3 worker leaves a
        #          folder alone for S3_MIN_AGE_MINUTES. They are slow (~200 ms a
        #          PNG) and there are many, so making Submit or Cancel wait for
        #          them is what made those buttons hang for seconds after a busy
        #          scan. They finish on their own in the background instead; only
        #          shutdown drains them, so a clean stop still loses nothing.
        self._jpg_writer_queue = getattr(self, "_jpg_writer_queue", None)
        self._png_writer_queue = getattr(self, "_png_writer_queue", None)

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
        # FMs already reviewed before this session picked the batch up — set by
        # restore() when a held or interrupted batch is continued. The tracker
        # starts over on a continued batch (its ids restart, and the objects it
        # knew are long gone from the belt), so the earlier reviews cannot live
        # in counted_track_ids; they are carried as this number instead and
        # added to the live count. Always 0 for a batch started fresh.
        self.prior_fo_count = 0
        self.tracker = ObjectTracker(
            x_tolerance=settings.TRACK_X_TOLERANCE_PX,
            x_tolerance_ratio=settings.TRACK_X_TOLERANCE_RATIO,
            stale_after_seconds=settings.TRACK_STALE_AFTER_SECONDS,
            revive_within_seconds=settings.TRACK_REVIVE_WITHIN_SECONDS,
            travel_margin=settings.TRACK_TRAVEL_MARGIN,
            min_travel_px=settings.TRACK_MIN_TRAVEL_PX,
            edge_margin_px=settings.TRACK_EDGE_MARGIN_PX,
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

        # Where this scan is in the "stop, settle, look" cycle:
        #   idle      — watching the belt, nothing found yet
        #   settling  — something was found, the belt has been told to stop,
        #               and we are waiting for it to actually stop. Nothing is
        #               shown or counted in this window.
        #   sampling  — the belt is stopped; collecting a few frames to combine
        #   reviewing — the operator has the result on screen
        #
        # The point of the cycle is that a detection is not a snapshot of the
        # one frame something was first spotted in. The belt takes about a
        # second to stop, and in that second the model keeps finding objects it
        # missed the first time — an object at 0.15 confidence is found in one
        # frame and not the next. Freezing the first frame means the rest arrive
        # afterwards as separate detections, so five objects sitting together on
        # the belt reach the operator as four and then one.
        self.review_phase = "idle"
        self._settle_deadline = 0.0
        self._samples: List[Dict] = []
        # The frame and boxes that triggered the stop, kept as a fallback: if
        # the stationary frames somehow turn up nothing, this is still shown
        # rather than the sighting being silently dropped.
        self._trigger: Optional[Dict] = None
        # Objects that left the bottom of the frame while the belt was still
        # stopping, keyed by track id. They were on the belt when the stop was
        # commanded but are gone by the time it is stationary, so the fresh
        # look that builds the review screen cannot find them — they are the
        # one thing the stop/settle/look cycle can lose that the old
        # freeze-the-first-frame behaviour caught. Each is kept with the frame
        # it was last seen in, and shown after the main screen.
        self._escaped: Dict = {}

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

        # Monotonic deadline until which detection is skipped because the belt
        # has only just been told to start and is still physically at rest.
        # Set by the first Start of a batch; see DETECTION_START_GRACE_SECONDS.
        self._start_grace_deadline = 0.0

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
            self._start_grace_deadline = (
                time.monotonic() + settings.DETECTION_START_GRACE_SECONDS
                if settings.DETECTION_START_GRACE_SECONDS > 0 else 0.0
            )
            logger.info(
                "Scan started: sample=%s commodity=%s variety=%s folder=%s",
                sample_id, commodity, variety, self.output_folder,
            )
            return self.status()

    # ------------------------------------------------------------------
    # Hold / continue — see app/services/scan_progress.py
    # ------------------------------------------------------------------

    def progress_state(self) -> Dict:
        """What scan_progress stores for this run, so it can be continued later.

        Only the in-memory part of a run. Its results are on disk already: the
        crops in output_folder are what create_results counts, and the r_frame
        files are what Frame Count counts.
        """
        return {
            "folder_name": self.folder_name,
            "sample_id": self.sample_id,
            "commodity": self.commodity,
            "variety": self.variety,
            "batch_id": (self.batch or {}).get("id"),
            "analysis_parameters": list(self.analysis_parameters),
            "start_date": self.start_date,
            "start_time": self.start_time,
            "output_folder": self.output_folder,
            "output_frame_folder": self.output_frame_folder,
            "frame_count": self.frame_count,
            "saved_frame_count": self.saved_frame_count,
            "clean_frame_count": self._clean_frame_count,
            "next_pending_index": self._next_pending_index,
            "conveyor_stop_count": dict(self.conveyor_stop_count),
        }

    def restore(self, state: Dict) -> Dict:
        """Load a held or interrupted run back in, ready for Start or Submit.

        The run continues in its own folders, so everything it captured before
        is still counted at Submit. Three things make that safe:

        * File numbering continues past what is on disk, not only past what the
          stored state says. A crop's name ends in its pending index, and
          re-labelling deletes `*_<index>.png` — reusing an index would delete
          an earlier object's crop. fm/ and r_frame files are numbered the same
          way and would be overwritten. The disk is checked too because a power
          cut can land after a file was written but before its row was.
        * The live FM count carries on from the earlier reviews (prior_fo_count).
        * A stop that was in progress when the run was interrupted is closed at
          its last recorded moment rather than left running, so the outage is
          not counted as stop time. Time on hold is never stop time.

        Detection stays suspended until Start, the same as after Submit, so the
        objects still sitting under the camera are not reported the moment the
        live view comes back.
        """
        with self._lock:
            self.reset()
            self.sample_id = state.get("sample_id") or ""
            self.commodity = state.get("commodity") or ""
            self.variety = state.get("variety") or ""
            batch_id = state.get("batch_id")
            self.batch = {"id": batch_id} if batch_id else {}
            self.analysis_parameters = list(state.get("analysis_parameters") or [])
            self.start_date = state.get("start_date") or ""
            self.start_time = state.get("start_time") or ""
            self.folder_name = state.get("folder_name") or ""
            self.output_folder = state.get("output_folder") or ""
            self.output_frame_folder = state.get("output_frame_folder") or ""
            os.makedirs(self.output_folder, exist_ok=True)
            os.makedirs(self.output_frame_folder, exist_ok=True)

            disk = _numbering_on_disk(self.output_folder, self.output_frame_folder)
            self.frame_count = max(int(state.get("frame_count") or 0), disk["frame"])
            self.saved_frame_count = max(int(state.get("saved_frame_count") or 0), disk["r_frame"])
            self._clean_frame_count = int(state.get("clean_frame_count") or 0)
            self._next_pending_index = max(
                int(state.get("next_pending_index") or 0), disk["pending_index"],
            )

            csc = {**self.conveyor_stop_count, **(state.get("conveyor_stop_count") or {})}
            stopped_at = state.get("last_checkpoint_ts")
            for running, total in (("fm_time", "total_fm_time"), ("stop_time", "total_stop_time")):
                if csc.get(running):
                    if stopped_at and stopped_at > csc[running]:
                        csc[total] = csc.get(total, 0) + (stopped_at - csc[running])
                    csc[running] = 0
            self.conveyor_stop_count = csc

            self.prior_fo_count = disk["crops"]
            self.active = True
            self.detection_suspended = True
            self.capture_paused = False
            logger.info(
                "Scan restored: sample=%s folder=%s prior_fo=%s next_index=%s "
                "frame=%s r_frame=%s",
                self.sample_id, self.folder_name, self.prior_fo_count,
                self._next_pending_index, self.frame_count, self.saved_frame_count,
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
        """Queue r_frame_N.jpg for writing at quality 95 (main.py:2413-2441).

        This is what update_fm_count counts as "Frame Count", and what the S3
        worker uploads. Without it that metric is always zero.

        The encode and the write happen on a background thread. Measured on
        this device, a full-resolution quality-95 JPEG costs **52 ms** — more
        than twice the 23 ms TensorRT pass it sits next to — and this runs
        inside process_frame, which is the detection loop. Done inline it
        blocks the next frame, so the detection rate collapses during a burst
        of detections: exactly when several objects arrive together and the
        frame rate matters most. Confirmed live, detections during a burst
        were landing about 150 ms apart (~7 fps) against a hardware ceiling
        near 43.

        The frame number is still assigned here, in order, so the files are
        numbered by when they were captured rather than by whichever write
        finished first. flush_raw_frames() waits for the backlog, and is
        called before anything counts or moves those files.
        """
        index = self.saved_frame_count
        self.saved_frame_count += 1
        path = os.path.join(self.output_frame_folder, f"r_frame_{index}.jpg")
        # frame is copied because the caller's array is reused by the camera
        # loop as soon as this returns.
        self._enqueue_write(("jpg", path, frame.copy(), None))

    def _enqueue_write(self, job) -> None:
        """Hand one write to the right background writer (jpg or png), warning
        if that writer is falling behind.

        The queues are deliberately unbounded — dropping a training frame or a
        belt frame silently would be worse than the memory — but each entry
        holds a full uncompressed copy (~6.9 MB at 1920x1200), so a backlog is
        the one way this path can hurt the running scan. Worth saying out loud
        rather than discovering as an OOM.

        It is close on the png side: a detected frame's PNG costs ~204 ms to
        encode and they can arrive ~50 ms apart during a burst. What saves it is
        that the belt stops on a detection and capture stops with it (see
        pause_capture), so the writer gets the whole review to catch up. This
        log is here to show if a scan ever comes along where that isn't enough.
        """
        work = self._frame_writer("jpg" if job[0] == "jpg" else "png")
        work.put(job)
        depth = work.qsize()
        if depth >= 40 and depth % 20 == 0:
            logger.warning(
                "%s frame writer is %s frames behind (~%.0f MB queued) — the "
                "disk or the encode is not keeping up with the scan.",
                "jpg" if job[0] == "jpg" else "png", depth, depth * 6.9,
            )

    def save_fm_training_artifacts(self, frame: np.ndarray, detections: List,
                                   fm_flag: bool):
        """Queue the per-frame training triple: full-frame .png + .txt + .conf.

        Port of save_image (main.py:2474-2533). This is the dataset the models
        are retrained from, and it is the reason the full frame is kept at all:
        the operator-facing crops under output/ are cut down to one object and
        carry no coordinates, so they cannot be relabelled or re-used as
        detection training data on their own.

        Layout, identical to legacy:

            output_frame/<commodity>/<variety>/<folder>/fm/
                frame_<n>.png    the full frame, PNG compression 3
                frame_<n>.txt    one YOLO line per detection, no confidence
                frame_<n>.conf   one confidence per line, same order
                low_confidence_frames/
                    low_confidence_frame_<n>.{png,txt,conf}

        `fm_flag` is legacy's `high_confidence` (they are the same value —
        process_results returns it as fm_flag and main.py receives it under the
        other name): False means a commodity suppression rule threw the frame
        away, so it never reached the operator. Those frames are still the most
        valuable ones to retrain on, which is why they are kept in their own
        folder rather than dropped. Note this branch was effectively dead in
        legacy — emit_results only emitted when detections survived, so nothing
        suppressed ever reached save_image and low_confidence_frames/ stayed
        empty. Here it is wired up for real, deliberately.

        Two deviations from legacy, both intentional:

        * the boxes written are the model's own, before enlarge_bbox pads them.
          Legacy wrote the padded boxes (emit_results pads before handing them
          on), which inflates every training label by the padding on all four
          sides — fine for cutting a crop the operator can see, wrong as ground
          truth.
        * no BGR->RGB conversion on the way out, for the same reason as the
          raw frames — see _COLOR_ORDER_NOTE.
        """
        if not settings.FM_FRAMES_ENABLED:
            return
        if not detections or not self.output_frame_folder:
            return
        fm_dir = os.path.join(self.output_frame_folder, "fm")
        if fm_flag:
            stem = os.path.join(fm_dir, f"frame_{self.frame_count}")
        else:
            stem = os.path.join(
                fm_dir, "low_confidence_frames",
                f"low_confidence_frame_{self.frame_count}",
            )
        self._enqueue_write(
            ("fm", stem, frame.copy(), [list(d) for d in detections])
        )

    def save_fm_full_frames(self, frame: np.ndarray):
        """One full frame per FM on the review screen, into fm_full_frames/.

            output_frame/<commodity>/<variety>/<folder>/fm_full_frames/
                frame_<index>.png    the full frame the FM was reviewed on
                frame_<index>.txt    one YOLO line: this FM's box
                frame_<index>.conf   this FM's confidence

        fm/ keeps every frame the model saw anything in, so one object sitting
        under a stopped belt fills twenty of them and there is no telling which
        frame belongs to which counted FM. This folder is the per-FM record
        instead: it is written from the review list (`self.pending`), and every
        entry on that list becomes exactly one crop under output/, which is
        what total_fo_detected counts. So a batch reporting 20 FMs has 20
        frames here.

        `<index>` is the pending index, the same number every crop for that
        object ends its filename with (`..._<index>.png`), so a crop and its
        full frame pair up by name.

        The .txt holds only this FM's box, not every box in the frame. Several
        FMs reviewed on one screen share one image, so a file listing every box
        would make those entries identical and the folder would stop saying
        which FM each frame is for. The box is the model's own (`raw_box`),
        unpadded, for the same reason as fm/ — see save_fm_training_artifacts.

        Called with the frame the operator reviews on, after self.pending has
        been set for it. Entries whose box would produce no crop (degenerate
        after clamping — save_unselected skips those too) are skipped, so the
        count stays one-to-one with the crops.
        """
        if not settings.FM_FULL_FRAMES_ENABLED:
            return
        if frame is None or not self.output_frame_folder or not self.pending:
            return
        full_dir = os.path.join(self.output_frame_folder, "fm_full_frames")
        h, w = frame.shape[:2]
        image = None
        for item in self.pending:
            x1, y1, x2, y2 = [int(round(v)) for v in item["box"]]
            if min(w, x2) <= max(0, x1) or min(h, y2) <= max(0, y1):
                continue
            if item.get("class_id") is None or item.get("confidence") is None:
                continue
            if image is None:
                # One copy for the whole screen: every entry here is the same
                # frame, and the writer only reads it.
                image = frame.copy()
            det = list(item["raw_box"][:4]) + [item["confidence"], item["class_id"]]
            stem = os.path.join(full_dir, f"frame_{item['index']}")
            self._enqueue_write(("fm", stem, image, [det]))

    def _frame_writer(self, kind: str) -> "queue.Queue":
        """The jpg or png background writer, each started on first use.

        One thread per kind, not a pool within a kind: the writes of one kind
        are large and sequential to the same directory, so running them in
        parallel would contend for the disk without finishing sooner. The two
        kinds are split so a user action that must wait for the jpg frames (see
        flush_raw_frames) does not also wait for the far slower png backlog.
        """
        attr = "_jpg_writer_queue" if kind == "jpg" else "_png_writer_queue"
        if getattr(self, attr) is None:
            work = queue.Queue()
            setattr(self, attr, work)

            def worker():
                while True:
                    item = work.get()
                    try:
                        if item is None:
                            return
                        _kind, path, image, detections = item
                        if _kind == "jpg":
                            # As-is, not through legacy's cv2.COLOR_BGR2RGB
                            # (main.py:2464) — see _COLOR_ORDER_NOTE.
                            encoded = cv2.imencode(
                                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95]
                            )[1]
                            with open(path, "wb") as fh:
                                fh.write(encoded.tobytes())
                        else:
                            _write_fm_triple(path, image, detections)
                    except Exception as exc:
                        logger.error("frame write failed: %s", exc)
                    finally:
                        work.task_done()

            threading.Thread(
                target=worker, name=f"{kind}-frame-writer", daemon=True,
            ).start()
            # Not only for the FastAPI app: any process that touches a
            # ScanSession — a script, a test — otherwise exits with this thread
            # parked inside the queue, which aborts the interpreter on the way
            # out and makes a clean run look like a crash.
            atexit.register(self.stop_raw_frame_writer)
        return getattr(self, attr)

    def stop_raw_frame_writer(self, timeout: float = 30.0) -> None:
        """Finish the pending writes and retire BOTH writer threads.

        Called from the app's shutdown. A daemon thread is killed wherever it
        happens to be when the interpreter exits, and if that is inside
        cv2.imencode the process aborts on the way out — which reads as a crash
        in the journal rather than a clean stop, and loses whatever frame was
        being written. Unlike flush_raw_frames (jpg only), this drains the png
        writer too, so a clean stop loses no training frame either.
        """
        for attr in ("_jpg_writer_queue", "_png_writer_queue"):
            work = getattr(self, attr)
            if work is None:
                continue
            done = threading.Event()
            threading.Thread(
                target=lambda w=work: (w.join(), done.set()), daemon=True,
            ).start()
            if not done.wait(timeout):
                logger.error("%s writes did not finish within %.0fs on stop.",
                             attr, timeout)
            work.put(None)
            setattr(self, attr, None)

    def flush_raw_frames(self, timeout: float = 30.0) -> None:
        """Block until every queued r_frame JPEG is on disk.

        Called by finish() before update_fm_count counts those files for
        "Frame Count": a scan that reported the number while writes were still
        in flight would undercount. Deliberately waits ONLY on the jpg writer —
        the png training frames are not counted or moved by anything a user
        action triggers, so making Submit/Cancel wait for that much slower
        backlog is pure latency (it was the post-scan button hang). The png
        writer catches up on its own and is drained only at shutdown.
        """
        work = self._jpg_writer_queue
        if work is None:
            return
        done = threading.Event()
        threading.Thread(
            target=lambda: (work.join(), done.set()),
            daemon=True,
        ).start()
        if not done.wait(timeout):
            logger.error(
                "Raw frame writes did not finish within %.0fs — the saved "
                "frame count may be short.", timeout,
            )

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
        # Start of the clock for LATENCY_DETECT_TO_STOP_SEND below.
        _t_frame_start = time.perf_counter()

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

            # Live view only, no detection, until the belt has had a moment to
            # get moving after the batch's first Start — see
            # DETECTION_START_GRACE_SECONDS. Without this, an object already
            # lying under the camera is detected at rest, stops the belt before
            # it has moved, and is then detected again as a new object once the
            # belt carries it on, because its track id is evicted while stopped.
            if self._start_grace_deadline:
                if time.monotonic() < self._start_grace_deadline:
                    return self._snapshot(fm_detected=False)
                self._start_grace_deadline = 0.0
                logger.info(
                    "Belt start grace of %.1fs elapsed — detection active.",
                    settings.DETECTION_START_GRACE_SECONDS,
                )

            h, w = frame.shape[:2]

            # 1. Commodity-specific suppression (process_results).
            raw_detections = detections
            detections, fm_flag = apply_suppression_rules(
                detections, self.commodity, self.variety
            )

            # Training artifacts, written from the model's own output before
            # padding, merging or tracking touch it — those steps exist to
            # serve the operator's review screen, and every one of them makes
            # the boxes a worse record of what the model actually saw. Kept
            # for suppressed frames too, under their own folder; see
            # save_fm_training_artifacts.
            self.save_fm_training_artifacts(frame, raw_detections, fm_flag)

            # The clean-belt frames, and the other half of legacy's split.
            # emit_results sends a frame down exactly one of two paths
            # (GrabImage.py:621-624): one with detections in it goes to the fm/
            # triple above, one without goes — every RAW_FRAME_EVERY-th time —
            # to r_frame_N.jpg. They are mutually exclusive, and it matters
            # which is which: "Frame Count" is the number of r_frame files, and
            # under legacy that means "how much belt did this scan look at".
            #
            # This port had it on the detection events instead, so Frame Count
            # was counting review screens — 8 against legacy's ~288 for
            # comparable work, on the same Qualix field. See doc 12 §5.1.
            if not detections:
                self._clean_frame_count += 1
                every = settings.RAW_FRAME_EVERY
                if every > 0 and self._clean_frame_count % every == 0:
                    self.save_raw_frame(frame)

            # A frame with nothing in it is not a reason to stop here. It
            # still has to age the tracker — otherwise an object that vanishes
            # completely stops the staleness clock and its id lives forever —
            # and it still has to advance the stop/settle/look cycle, or a
            # sighting the model loses once the belt stops leaves the scan
            # frozen in `sampling` with the belt stopped and nothing on screen.
            if not fm_flag:
                detections = []

            # One box per object before anything downstream sees them — the
            # tracker, the counted ids and the operator's boxes all come off
            # this list. See _merge_overlapping_detections.
            # Kept for the detection log below, so the line can separate what
            # a suppression rule dropped from what merging combined — by the
            # time it runs, `detections` has been through both.
            _n_after_suppression = len(detections)
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
            _t_track = time.perf_counter()
            self.tracker.update(detections, self.frame_count, (h, w))
            _track_ms = (time.perf_counter() - _t_track) * 1000.0
            track_ids = list(self.tracker.get_tracked_objects().keys())
            new_ids = set(track_ids) - self.existing_track_ids

            # Legacy's per-frame pair: "Tracker update time" (GrabImage.py:545)
            # and the tracking line in handle_detection (main.py:2578). Both
            # fire on every inferred frame, so both sit behind DETECTION_TRACE
            # — see that setting's comment for the volume this would otherwise
            # add. `intersection` is legacy's name for the ids carried over
            # from earlier frames, i.e. objects still in view.
            if settings.DETECTION_TRACE:
                logger.info(
                    "Tracker update time: %.4f s | frame=%s tracked=%s "
                    "diff_list=%s intersection=%s queue=%s",
                    _track_ms / 1000.0, self.frame_count, sorted(track_ids),
                    sorted(new_ids),
                    sorted(set(track_ids) & self.existing_track_ids),
                    len(self.detection_queue),
                )

            # The detection-event line, always on. Unlike the two above this
            # only fires on a frame the model actually found something in, so
            # it is rare enough to leave at INFO permanently — and it is the
            # one that carries the confidence scores, which nothing else in
            # this pipeline logged. Legacy's "fm detected >>>" (main.py:2584),
            # with its raw/suppressed split made explicit: legacy logged the
            # surviving boxes only, so a frame dropped by a suppression rule
            # looked identical to a frame the model saw nothing in.
            if raw_detections:
                logger.info(
                    "Detections on frame %s: model=%s after_suppression=%s "
                    "after_merge=%s boxes=%s",
                    self.frame_count, len(raw_detections),
                    _n_after_suppression, len(detections),
                    _fmt_boxes(raw_detections),
                )

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
            if new_ids or self.tracker.last_evicted or self.tracker.last_revived:
                logger.info(
                    "Track churn: new=%s revived=%s evicted=%s tracked=%s "
                    "counted_so_far=%s",
                    sorted(new_ids), sorted(self.tracker.last_revived),
                    self.tracker.last_evicted,
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
                # A detection clipped by the bottom edge with no id is half an
                # object on its way out. The tracker deliberately refuses it a
                # new id, which means the already-shown filter above cannot
                # speak for it and the geometry backstop below would call it
                # novel and put it on screen — showing an object the operator
                # has already reviewed a second time as a half box. Anything
                # genuinely new was whole and identified further up the frame;
                # anything leaving that has never been shown is carried by
                # _note_escapes, which works off its track id. Neither needs
                # this detection.
                if (track_id is None
                        and detections[i][3] >= h - self.tracker.edge_margin_px):
                    continue
                # The padded box travels together with the unpadded
                # detection it came from. Padding exists to give the saved
                # crop some margin (see enlarge_bbox above); drawn on screen
                # it makes every object look 40px wider and taller than it
                # is, which is enough to make two separate objects overlap on
                # the review screen when they never touched on the belt. The
                # review screen draws `raw`; crops are still cut from `box`.
                candidates.append((track_id, box, detections[i]))

            # Geometry is now only a backstop, for the two cases an id cannot
            # cover: a detection the tracker refused to track at all (it gives
            # no id to anything against the left edge), and an object whose
            # track was dropped and re-minted, which arrives wearing an id
            # nobody has seen before.
            novel = self._novel_boxes([box for _, box, _ in candidates])
            keep = {id(box) for box in novel}
            candidates = [(t, b, r) for t, b, r in candidates if id(b) in keep]

            # Nothing new is accepted while the backlog is at its limit. The
            # objects behind it are deliberately NOT marked as counted, so they
            # stay unseen ids and are detected again on a later frame once the
            # operator has worked the queue down — refusing to queue costs a
            # short delay, whereas marking them counted would lose them for the
            # rest of the scan.
            # ---- stop, settle, look -------------------------------------
            # Nothing is shown or counted until the belt has stopped and a few
            # stationary frames have been combined. See review_phase in reset().
            if self.review_phase in ("settling", "sampling"):
                self._note_escapes(frame, h, w)

            if self.review_phase == "settling":
                self.existing_track_ids.update(track_ids)
                if time.monotonic() < self._settle_deadline:
                    return self._snapshot(fm_detected=False)
                self.review_phase = "sampling"
                self._samples = []
                # Falls through — the frame that ends the wait is the first
                # stationary one, and there is no reason to discard it.

            if self.review_phase == "sampling":
                self.existing_track_ids.update(track_ids)
                self._samples.append({"frame": frame, "detections": detections})
                if len(self._samples) < max(1, settings.DETECTION_SAMPLE_FRAMES):
                    return self._snapshot(fm_detected=False)
                self._promote_from_samples(h, w)
                return self._snapshot(fm_detected=bool(self.pending))

            if candidates and self.review_phase == "idle":
                # First sighting. Stop the belt now — the interlock goes on
                # before FM_detected so a machine_start cannot win the race —
                # and come back to it once it has actually stopped.
                self._trigger = {
                    "frame": frame.copy(),
                    "boxes": [list(box) for _, box, _ in candidates],
                    "raw_boxes": [list(raw) for _, _, raw in candidates],
                }
                csc = self.conveyor_stop_count
                csc["fm_count"] += 1
                csc["fm_time"] = time.time()

                # Legacy's per-object line (main.py:2589's "@@@@..."): one row
                # per object with the box that triggered it, its confidence and
                # the track id it was given. The id is what decides whether this
                # object is ever shown again (see counted_track_ids), so it is
                # the single most useful field when an object is shown twice or
                # not at all — and it is only knowable here, after assignment.
                for n, (track_id, _box, raw) in enumerate(candidates, 1):
                    logger.info(
                        "FM object %s/%s: box=%s track_id=%s",
                        n, len(candidates), _fmt_boxes([raw]), track_id,
                    )
                logger.info(
                    "Total number of bounding boxes found: %s", len(candidates)
                )

                # Legacy's LATENCY_CAPTURE_TO_STOP_SEND (main.py:2699), logged
                # in the same place: BEFORE the send, so it measures how long
                # the decision took, not how long the serial round-trip took.
                # The round-trip is already reported by conveyor_service's own
                # CONTROL_CMD_TIMING-equivalent lines.
                #
                # Measured from the start of process_frame, not from the grab:
                # this function is handed a frame and never learns when it was
                # captured. The grab and inference legs are reported separately
                # by camera.py's "Latency breakdown", so the two together cover
                # what legacy's single number did.
                logger.info(
                    "LATENCY_DETECT_TO_STOP_SEND frame_id=%s latency_ms=%.2f "
                    "fm_count=%s",
                    self.frame_count,
                    (time.perf_counter() - _t_frame_start) * 1000.0,
                    csc["fm_count"],
                )
                conveyor_service.lock_machine_start(reason="FM_detected")
                conveyor_service.send("FM_detected")
                self.review_phase = "settling"
                self._settle_deadline = (
                    time.monotonic() + settings.DETECTION_SETTLE_SECONDS)
                logger.info(
                    "Foreign matter sighted (%s object(s)) — belt stopping, "
                    "looking again in %.1fs once it has settled",
                    len(candidates), settings.DETECTION_SETTLE_SECONDS,
                )
                self.existing_track_ids.update(track_ids)
                return self._snapshot(fm_detected=False)

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
                self._on_foreign_matter(
                    frame,
                    [box for _, box, _ in candidates],
                    [raw for _, _, raw in candidates],
                )
                # Only objects actually put in front of the operator count
                # toward total_fo_detected — see counted_track_ids' comment
                # above — and recording them here is also what stops each of
                # them being shown again.
                self.counted_track_ids.update(
                    t for t, _, _ in candidates if t is not None
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

    @staticmethod
    def _overlaps_any(box, others, min_iou: float = 0.2) -> bool:
        """Is this box the same object as one of `others`?

        Plain IoU on the unpadded boxes. The belt is stopped whenever this is
        asked, so the same object measured a moment apart lands in very nearly
        the same place and a low threshold is enough.
        """
        if not others or box is None or len(box) < 4:
            return False
        ax1, ay1, ax2, ay2 = box[:4]
        area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
        for other in others:
            if other is None or len(other) < 4:
                continue
            bx1, by1, bx2, by2 = other[:4]
            ix = max(0, min(ax2, bx2) - max(ax1, bx1))
            iy = max(0, min(ay2, by2) - max(ay1, by1))
            inter = ix * iy
            if not inter:
                continue
            area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
            union = area_a + area_b - inter
            if union > 0 and inter / union >= min_iou:
                return True
        return False

    def _note_escapes(self, frame: np.ndarray, h: int, w: int):
        """Remember anything that leaves the view while the belt is stopping.

        The tracker's exit rule drops a track once it reaches the bottom of the
        frame, and it was still in view on the update that dropped it — so its
        last recorded box belongs to the frame passed in here. That pairing is
        the whole point: it gives a box and an image that actually match, for
        an object that will not be in any of the stationary frames the review
        screen is built from.

        Only ids that have never been shown are kept. An object already
        reviewed is expected to leave.
        """
        for obj_id, reason in self.tracker.last_evicted.items():
            if not reason.startswith("exit-zone"):
                continue
            if obj_id in self.counted_track_ids or obj_id in self._escaped:
                continue
            # An id minted and exit-evicted inside the same update never
            # travelled anywhere — it is an object sitting in the bottom
            # 50px of a STOPPED belt, detected afresh because the exit rule
            # had already taken its previous id. Treating that as an escape
            # queued one extra review screen per frame, each under a brand
            # new id, so the per-id guard above could never catch it and the
            # operator was shown the same parked object several times over.
            # Reported live, 29 Sep: one object, three screens.
            if self.tracker.last_evicted_first_frames.get(obj_id) == self.frame_count:
                continue
            box = self.tracker.last_evicted_boxes.get(obj_id)
            if box is None or len(box) < 4:
                continue
            self._escaped[obj_id] = {
                "frame": frame.copy(),
                "box": enlarge_bbox(box, pad=20, img_w=w, img_h=h),
                "raw_box": list(box),
            }
            logger.warning(
                "Object %s left the frame while the belt was still stopping — "
                "holding its last sighting to show after the main screen.",
                obj_id,
            )

    def _queue_escapes(self, still_visible=None):
        """Put anything that got away behind the main review screen.

        One entry each, carrying its own frame, because they were last seen at
        different moments and a crop has to be cut from the frame its box was
        measured in. They are marked as counted here rather than when shown:
        they have left the camera's view, so nothing will detect them again and
        there is no second chance to record them.

        `still_visible` is the combined stationary detection set the review
        screen is being built from. Anything overlapping one of those boxes did
        not get away at all — it is parked half out of the bottom of the frame
        and is about to be shown on the main screen — so queueing it as well
        showed the operator the same object twice.
        """
        escaped, self._escaped = self._escaped, {}
        for obj_id, item in list(escaped.items()):
            if self._overlaps_any(item["raw_box"], still_visible or []):
                logger.info(
                    "Object %s is still in view on the stopped belt — it is on "
                    "the main review screen, not queued as an escape.", obj_id,
                )
                del escaped[obj_id]
        for obj_id, item in escaped.items():
            self.detection_queue.append({
                "frame": item["frame"],
                "boxes": [item["box"]],
                "raw_boxes": [item["raw_box"]],
            })
            self.counted_track_ids.add(obj_id)
        if escaped:
            logger.warning(
                "%s object(s) left the view while the belt was stopping; "
                "queued behind the main screen: %s",
                len(escaped), sorted(escaped),
            )

    def _promote_from_samples(self, h: int, w: int):
        """Combine the stationary frames into one review screen.

        Every box from every sample goes into one list and
        _merge_overlapping_detections collapses the repeats, so an object seen
        in all three frames becomes one box at its best confidence and an
        object seen in only one is still there. This is only sound because the
        belt is stopped: boxes measured in different frames describe the same
        positions, which is exactly what is not true during the deceleration.

        The newest sample supplies the image, so the crops are cut from a frame
        every box genuinely belongs to.
        """
        samples, self._samples = self._samples, []
        trigger, self._trigger = self._trigger, None
        self.review_phase = "reviewing"

        combined = [d for sample in samples for d in sample["detections"]]
        combined = self._merge_overlapping_detections(combined)
        frame = samples[-1]["frame"] if samples else None

        # Anything that left the view while the belt was stopping goes behind
        # whatever the stationary frames turn up. Queued before every exit
        # below, including the ones that find nothing, so they still show —
        # but after `combined` exists, because anything still sitting in view
        # belongs on the main screen instead of behind it.
        self._queue_escapes(still_visible=combined)

        if not combined and trigger is not None:
            # Gate the fallback on how confident the original sighting was.
            # The stationary frames are the easiest ones the model ever gets,
            # so a sighting it cannot reproduce in any of them is more likely
            # to have been blur or noise on a moving frame than a real object.
            # Below the floor that is treated as a false positive and dropped;
            # above it the old always-show behaviour stands. See
            # DETECTION_FALLBACK_MIN_CONF — at its default of 0 this branch
            # never fires and nothing changes.
            floor = settings.DETECTION_FALLBACK_MIN_CONF
            if floor > 0:
                raw = trigger.get("raw_boxes") or []
                best = max(
                    (float(b[4]) for b in raw if len(b) >= 5), default=0.0
                )
                if best < floor:
                    logger.info(
                        "Discarded as a false positive: the re-look found "
                        "nothing in %s stationary frame(s) and the sighting "
                        "that stopped the belt reached only %.2f confidence "
                        "(floor %.2f). Not counted, not shown.",
                        len(samples), best, floor,
                    )
                    # Same two-step exit as the "nothing to fall back on" case
                    # below: anything that escaped during the stop is still
                    # owed a screen, and only if there is none do we release.
                    if self._promote_next_detection(immediate=True):
                        return
                    self._release()
                    return

            # The stationary frames found nothing — the model lost whatever it
            # saw a moment ago. Fall back to the sighting that stopped the belt
            # rather than dropping it: a spurious box the operator dismisses is
            # recoverable, a missed object is not.
            logger.warning(
                "Nothing found in %s stationary frame(s) after the belt "
                "stopped — falling back to the %s box(es) that triggered it.",
                len(samples), len(trigger["boxes"]),
            )
            self._show(trigger["frame"], trigger["boxes"],
                       trigger.get("raw_boxes"))
            return

        if not combined:
            # Nothing found and nothing to fall back on. Release rather than
            # stranding the operator on a frozen screen with no boxes — unless
            # something escaped, which is then the only thing to show.
            if self._promote_next_detection(immediate=True):
                return
            logger.info("Nothing found after the belt stopped — releasing.")
            self._release()
            return

        # One last tracker pass over the combined set, so every box carries a
        # track id and the already-shown filter applies to it. The objects have
        # been tracked throughout the settle, so these match their existing ids.
        self.tracker.update(combined, self.frame_count, (h, w))
        assignment = self.tracker.last_assignment
        boxes = []
        raw_boxes = []
        shown_ids = []
        for i, det in enumerate(combined):
            track_id = assignment[i] if i < len(assignment) else None
            if track_id is not None and track_id in self.counted_track_ids:
                continue
            boxes.append(enlarge_bbox(det, pad=20, img_w=w, img_h=h))
            raw_boxes.append(list(det))
            shown_ids.append(track_id)

        if not boxes:
            if self._promote_next_detection(immediate=True):
                return
            logger.info(
                "Everything found after the belt stopped has already been "
                "reviewed — releasing.")
            self._release()
            return

        logger.info(
            "Combined %s stationary frame(s) into one review screen: %s object(s)",
            len(samples), len(boxes),
        )
        self.counted_track_ids.update(t for t in shown_ids if t is not None)
        self.existing_track_ids.update(self.tracker.get_tracked_objects().keys())
        self._show(frame, boxes, raw_boxes)

    def _release(self):
        """Nothing to review after all — hand the belt back without a screen."""
        self.review_phase = "idle"
        self._escaped = {}
        conveyor_service.unlock_machine_start(reason="nothing to review")

    def _set_pending(self, boxes: List, raw_boxes: Optional[List] = None):
        """Turn one set of boxes into the numbered list the operator reviews.

        Ordered by how far each object has travelled down the belt, furthest
        first. Within one frozen frame every box was found at the same
        instant, so there is no "found first" among them — but the one
        furthest along entered the camera's view earliest, which is the same
        order the operator watched them arrive in. Left alone, the order is
        whatever the model's NMS emitted, which is by confidence: the numbers
        on the crops would then follow how sure the model was rather than
        anything the operator can see, and the reclassify gallery lists
        unlabelled crops in exactly this order.

        Each entry carries two boxes. `box` is padded and is what every crop
        is cut from — label_detection, save_unselected and the review panel's
        thumbnails all read it. `raw_box` is the detection as the model
        reported it, and is what the overlay draws, so two objects only look
        like they overlap on screen when they genuinely do on the belt.
        """
        if not raw_boxes or len(raw_boxes) != len(boxes):
            raw_boxes = boxes
        pairs = sorted(
            zip(boxes, raw_boxes),
            key=lambda pair: (pair[0][1] + pair[0][3]) / 2.0,
            reverse=True,
        )
        self.pending = []
        for box, raw in pairs:
            self.pending.append({
                "index": self._next_pending_index,
                "box": [float(v) for v in box[:4]],
                "raw_box": [float(v) for v in raw[:4]],
                "confidence": float(box[4]) if len(box) > 4 else None,
                "class_id": int(box[5]) if len(box) > 5 else None,
            })
            self._next_pending_index += 1

    def _show(self, frame: np.ndarray, boxes: List,
              raw_boxes: Optional[List] = None):
        """Put one set of boxes on the review screen and freeze the stream.

        The belt is already stopped by the time this runs, so capture is paused
        at once rather than after a deceleration delay — waiting would only
        show the operator a second of live feed over the frame they are meant
        to be reviewing.
        """
        self._set_pending(boxes, raw_boxes)
        self.pending_frame = frame
        self.save_fm_full_frames(frame)
        self.frozen_frame_seq += 1
        self.labelled_indices = set()
        self._pause_token += 1
        self.capture_paused = True
        logger.info(
            "Foreign matter on screen: %s box(es) awaiting operator label "
            "(%s more detection(s) queued behind it)",
            len(boxes), len(self.detection_queue),
        )

    def _on_foreign_matter(self, frame: np.ndarray, boxes: List,
                           raw_boxes: Optional[List] = None):
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
            "raw_boxes": [list(box) for box in (raw_boxes or boxes)],
        })
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

        # Newest first. The most recently identified object is the one the
        # operator is looking at on the belt, so it is the one to show next;
        # anything queued earlier keeps its place behind it.
        item = self.detection_queue.pop()
        boxes = item["boxes"]
        raw_boxes = item.get("raw_boxes")

        csc = self.conveyor_stop_count
        csc["fm_count"] += 1
        csc["fm_time"] = time.time()

        # Ordering matters: the interlock is engaged BEFORE FM_detected goes
        # out, so a machine_start arriving in between cannot win the race.
        conveyor_service.lock_machine_start(reason="FM_detected")
        conveyor_service.send("FM_detected")

        self._set_pending(boxes, raw_boxes)
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
        self.save_fm_full_frames(self.pending_frame)
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
            safe_name = fm_filename_token(fm_name)
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
            #
            # Checked, not assumed. imwrite reports failure by returning False
            # rather than raising, and a crop that is not on disk is an object
            # that never happened: not counted, not synced, not in the
            # reclassify gallery — while the operator was told the label was
            # accepted. Raising turns silent data loss into a visible error.
            if not cv2.imwrite(path, crop):
                raise RuntimeError(
                    f"Could not write crop {filename!r} for {fm_name!r}"
                )

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
                self.review_phase = "reviewing"
                return {"resumed": True, **self.status()}

            # Back to watching the belt. The next sighting starts the stop /
            # settle / look cycle over again from the top.
            self.review_phase = "idle"
            self._samples = []
            self._trigger = None

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

        No flush_raw_frames() here, unlike finish(): Cancel only moves the crops
        in output_folder, which are written synchronously (cv2.imwrite in
        label_detection / save_unselected), and it never touches output_frame/,
        which is where the background writers' files go. Waiting for that
        backlog protected nothing Cancel does and was the main reason Cancel
        hung for seconds after a busy scan. Any frames still being written land
        in the (now-abandoned) output_frame folder on their own.
        """
        with self._lock:
            sample_id = self.sample_id
            folder_name = self.folder_name
            archive_crops(self.output_folder, self.commodity, self.variety, self.folder_name)
            self.reset()
        return {"cancelled": True, "sample_id": sample_id, "folder_name": folder_name}


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
                # Matched in the filename's own spelling rather than by
                # translating the filename back into a display name. The two
                # are the same thing for a plain name, but a name carrying a
                # separator only ever exists on disk in its token form, and
                # that is the form the file actually has.
                stem = name.rsplit(".", 1)[0]
                for key in params:
                    if stem.startswith(fm_filename_token(key)):
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
        stem = name.rsplit(".", 1)[0]
        return next(
            (key for key in params if stem.startswith(fm_filename_token(key))),
            "NON-FM",
        )

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
        prefix = fm_filename_token(fm_type) + "_"
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
            safe_name = fm_filename_token(fm_name)
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
        # Outside the lock: the writer thread does not need it, and holding it
        # across a disk flush would stall the stream loop.
        self.flush_raw_frames()

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
            "total_fo_detected": self.prior_fo_count + len(self.counted_track_ids),
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
            "total_fo_detected": self.prior_fo_count + len(self.counted_track_ids),
            "frame_count": self.frame_count,
            "conveyor_stop_count": dict(self.conveyor_stop_count),
            "machine_start_locked": conveyor_service.machine_start_locked,
            **self.pending_status(),
        }


scan_session = ScanSession()
