"""
Camera streaming API.

Routes:
    WS   /ws/camera/stream    (mounted app-level via ws_router)
    GET  /api/camera/status
    POST /api/camera/model
    POST /api/camera/start    begin acquisition
    POST /api/camera/stop     end acquisition

Route paths on `router` are relative because app/main.py mounts it under
/api/camera. The WebSocket lives on a separate router mounted at the app root
so its public URL stays ws://host/ws/camera/stream.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import re
import shutil
import struct
import threading
import time

from datetime import datetime
from pathlib import Path

import cv2
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.config import settings
from app.services.camera_service import BaseCameraService, get_camera_service
from app.services import dc_history
from app.services.conveyor_service import conveyor_service
from app.services.inference_service import BaseInferenceService, get_inference_service, normalize_commodity
from app.services.scan_session import scan_session

logger = logging.getLogger(__name__)

router = APIRouter()
ws_router = APIRouter()

# How long the interlock may stay LOCKED with nothing to review before the
# stream loop treats it as wedged and clears it. Comfortably longer than the
# ~1-2s a normal detection takes to settle and put its review on screen, so
# only a genuine deadlock ever reaches it. See the self-heal block in
# camera_stream's paused branch.
STUCK_LOCK_SECONDS = 8.0


def _pack_frame(header: dict, jpeg: bytes) -> bytes:
    """One binary websocket message carrying a frame AND the state describing it.

    Layout: 4-byte big-endian header length, the header as UTF-8 JSON, then the
    raw JPEG bytes.

    The JPEG goes as raw bytes rather than base64 inside JSON so the browser
    can decode it straight to a bitmap, paint it, and release it at once -
    nothing per frame is left behind for the browser to cache.

    Header and image stay in ONE message on purpose: the boxes in `pending` are
    drawn over exactly this image, so the two must never be able to arrive
    apart or be paired with the wrong neighbour.
    """
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(head)) + head + jpeg

# ---------------------------------------------------------------------------
# Services. The camera and the TensorRT context are single physical resources,
# so they are guarded by a lock — several browser tabs must not interleave
# grabs or run inference concurrently on one CUDA context.
#
# All of it also has to run on the SAME OS thread every time. run_inference.py
# pushes a CUDA context onto whichever thread first touches it per-thread
# (run_inference.py:179-204) and only pops it via an explicit cleanup() call
# (run_inference.py:235-267) — nothing pops it automatically. FastAPI runs
# each sync request handler on a fresh thread from its own pool, and the
# asyncio default executor used inside the websocket loop is a THIRD, separate
# pool — so without this, camera/inference calls could land on a different
# thread almost every time, each one pushing a new context that never gets
# popped. At shutdown, pycuda notices contexts still on some thread's stack
# that were never cleaned up and hard-aborts the whole process ("PyCUDA
# ERROR: The context stack was not empty... Aborted (core dumped)") — this
# dedicated single-worker executor is what makes "the same thread every time"
# actually true, so the one cleanup() call at shutdown pops the one context
# that was ever pushed.
# ---------------------------------------------------------------------------

_camera: BaseCameraService | None = None
_inference: BaseInferenceService | None = None
_camera_initialized = False
_hardware_lock = threading.Lock()
_stream_clients = 0
_clients_lock = threading.Lock()
# Monotonic time the LAST browser dropped off the stream, or None while one is
# attached. Nothing here acts on it — it exists so the scan API can tell a run
# somebody is watching from one whose browser has gone (reloaded, logged out,
# closed) and is never coming back for it. See seconds_without_viewer() and
# api/scan.py's _reclaim_orphaned_scan(). Starts set, because at import time no
# browser has connected yet.
_last_viewer_gone_at: float | None = time.monotonic()

_hw_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-hw")


def seconds_without_viewer() -> float:
    """How long no browser has been subscribed to the stream, in seconds.

    0.0 while at least one is attached, so a caller can treat "zero" as
    "somebody is watching" without a second check.
    """
    with _clients_lock:
        if _stream_clients > 0 or _last_viewer_gone_at is None:
            return 0.0
        return max(0.0, time.monotonic() - _last_viewer_gone_at)


def _run_on_hw_thread(fn, *args, **kwargs):
    """Run fn on the single dedicated camera/inference thread and block for
    the result. Safe to call from a sync request handler (already running in
    some other thread) or await via run_in_executor from async code."""
    return _hw_executor.submit(fn, *args, **kwargs).result()

# ---------------------------------------------------------------------------
# Data Collection — raw frame dump for later manual labelling, no inference.
# Port of the legacy "Data Collection" page (goto_dc_page / start_dc /
# capture_image_dc / stop_dc, main.py:1843-2022), which is a completely
# separate flow from the scan/detection one above: it just streams the raw
# feed and, while "recording", saves every grabbed frame to disk untouched.
# ---------------------------------------------------------------------------

_dc_lock = threading.Lock()
_dc_recording = False
_dc_folder: str | None = None
# Frames actually written to disk this visit (what the operator gets).
_dc_frame_count = 0
# Every frame grabbed while recording, saved or not.
_dc_frames_seen = 0
# Wall-clock bounds of this visit's recording: first Start to last Stop.
_dc_started_at: float | None = None
_dc_stopped_at: float | None = None
# The one /ws/data_collection/stream connection that saves frames. Others
# (a second kiosk window, a browser) get the preview only, so files are not
# written twice and the counts stay true. Whoever connects first while the
# slot is free owns it; it is released when that connection closes.
_dc_writer: object | None = None
# Who this visit's run is for, kept from prepare for the history record, and
# whether that run has been opened (folder created, record written). A run is
# opened by its first Start or Capture, not by prepare, so a visit that never
# records leaves no folder and no record behind.
_dc_meta: dict | None = None
_dc_run_open = False

# Operator-editable settings for Data Collection, kept in their own file so
# the frame rate of a collection can be changed on the device without
# touching .env or restarting the service: it is re-read on every prepare,
# i.e. every visit to the Data Collection page.
DC_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "data_collection_settings.json"
DC_SETTINGS_DEFAULTS = {
    # Keep one frame in every N grabbed while recording. 1 = every frame
    # (legacy behaviour); 3 = every third frame.
    "save_every_n_frames": 3,
    # "png" (lossless, as legacy) or "jpg".
    "image_format": "png",
    # Only used when image_format is "jpg".
    "jpeg_quality": 95,
    # Recording stops (and the belt with it) when the output disk has less
    # free space than this, instead of failing writes silently.
    "min_free_disk_gb": 2.0,
}
_dc_settings = dict(DC_SETTINGS_DEFAULTS)


def _load_dc_settings() -> dict:
    """Read data_collection_settings.json, falling back per key to the
    defaults. A missing or broken file never blocks a collection."""
    loaded = dict(DC_SETTINGS_DEFAULTS)
    try:
        with open(DC_SETTINGS_PATH) as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            loaded.update({k: raw[k] for k in DC_SETTINGS_DEFAULTS if k in raw})
    except FileNotFoundError:
        logger.info("No %s; using data collection defaults", DC_SETTINGS_PATH.name)
    except Exception as exc:
        logger.warning("Ignoring unreadable %s: %s", DC_SETTINGS_PATH.name, exc)
    try:
        loaded["save_every_n_frames"] = max(1, int(loaded["save_every_n_frames"]))
    except (TypeError, ValueError):
        loaded["save_every_n_frames"] = DC_SETTINGS_DEFAULTS["save_every_n_frames"]
    if str(loaded["image_format"]).lower() not in ("png", "jpg"):
        loaded["image_format"] = "png"
    loaded["image_format"] = str(loaded["image_format"]).lower()
    try:
        loaded["jpeg_quality"] = min(100, max(1, int(loaded["jpeg_quality"])))
    except (TypeError, ValueError):
        loaded["jpeg_quality"] = DC_SETTINGS_DEFAULTS["jpeg_quality"]
    try:
        loaded["min_free_disk_gb"] = max(0.0, float(loaded["min_free_disk_gb"]))
    except (TypeError, ValueError):
        loaded["min_free_disk_gb"] = DC_SETTINGS_DEFAULTS["min_free_disk_gb"]
    return loaded


def _dc_free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1e9


# Disk writes run here, off the camera thread, so a slow PNG encode never
# holds up the next grab. One worker keeps files in order. Writes still in
# flight are capped at _DC_MAX_PENDING_WRITES: past that a kept frame is
# dropped (and counted) rather than letting memory grow without bound.
_dc_write_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="dc-write")
_DC_MAX_PENDING_WRITES = 16
_dc_pending_writes = 0
_dc_frames_dropped = 0
# Why recording last stopped on its own (disk full, write error), for the
# app to show; cleared on prepare and on start.
_dc_stop_reason: str | None = None


def _dc_write(path: str, frame, params: list) -> None:
    global _dc_frame_count, _dc_pending_writes, _dc_frames_dropped
    try:
        written = cv2.imwrite(path, frame, params)
    except Exception as exc:
        logger.error("Data collection write failed for %s: %s", path, exc)
        written = False
    with _dc_lock:
        _dc_pending_writes -= 1
        if written:
            _dc_frame_count += 1
        else:
            _dc_frames_dropped += 1


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _dc_status() -> dict:
    end = _dc_stopped_at if not _dc_recording else time.time()
    duration = (end - _dc_started_at) if (_dc_started_at and end) else 0.0
    return {
        "recording": _dc_recording,
        "folder": _dc_folder,
        "frame_count": _dc_frame_count,
        "frames_seen": _dc_frames_seen,
        "save_every_n_frames": _dc_settings["save_every_n_frames"],
        "frames_dropped": _dc_frames_dropped,
        "stop_reason": _dc_stop_reason,
        "started_at": _iso(_dc_started_at),
        "ended_at": _iso(end) if _dc_started_at else None,
        "duration_seconds": round(max(0.0, duration), 1),
    }


def _dc_begin_recording() -> None:
    """Caller holds _dc_lock."""
    global _dc_recording, _dc_started_at, _dc_stopped_at
    _dc_recording = True
    if _dc_started_at is None:
        _dc_started_at = time.time()
    _dc_stopped_at = None


def _dc_end_recording() -> None:
    """Caller holds _dc_lock."""
    global _dc_recording, _dc_stopped_at
    if _dc_recording:
        _dc_stopped_at = time.time()
    _dc_recording = False


def _dc_existing_dir(path: str) -> str:
    """Nearest existing directory at or above path, for a free-space check on
    a run folder that is not created until recording starts."""
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path or settings.OUTPUT_DIR


def _dc_open_run() -> None:
    """Create this visit's folder and history record, the first time it records.

    Must run before _dc_begin_recording: the stream loop writes into _dc_folder
    as soon as recording is on.
    """
    global _dc_run_open
    if _dc_run_open or not _dc_folder:
        return
    os.makedirs(_dc_folder, exist_ok=True)
    meta = _dc_meta or {}
    dc_history.record_start(
        _dc_folder, meta.get("sample_id", ""), meta.get("commodity", ""),
        meta.get("variety", ""), _dc_settings, time.time(),
    )
    _dc_run_open = True


def _dc_drain_writes(timeout: float = 10.0) -> bool:
    """Wait for frames already handed to the writer. True if all landed."""
    deadline = time.time() + timeout
    while _dc_pending_writes > 0 and time.time() < deadline:
        time.sleep(0.05)
    return _dc_pending_writes == 0


def _dc_close_run(final_status: str) -> dict:
    """End this visit's run for good: stop recording, let queued frames land,
    then finalize its record (or remove it if it saved nothing). Returns the
    run's summary. Leaves the belt alone."""
    global _dc_run_open
    with _dc_lock:
        _dc_end_recording()
    drained = _dc_drain_writes()
    with _dc_lock:
        summary = _dc_status()
    if _dc_run_open and _dc_folder:
        if drained:
            dc_history.finalize(_dc_folder, summary, final_status)
        else:
            # A frame may still be on its way into the folder, so it must not
            # be treated as empty. Counts are saved; the next startup closes it.
            logger.warning("Data collection writes still pending; %s left open", _dc_folder)
            dc_history.record_update(_dc_folder, summary)
    _dc_run_open = False
    return summary


def _ensure_services() -> bool:
    global _camera, _inference, _camera_initialized

    with _hardware_lock:
        if _camera is None:
            _camera = get_camera_service()
        if _inference is None:
            _inference = get_inference_service()

        if not _camera_initialized:
            if _camera.initialize() and _camera.start_grabbing():
                _camera_initialized = True
                logger.info("Camera initialized and grabbing")
            else:
                logger.error("Camera initialization FAILED")
                return False
        return True


def shutdown_camera_services():
    """Called from the app lifespan on shutdown.

    Must run on the same dedicated thread every other camera/inference call
    used, so cleanup() pops the CUDA context on the thread that pushed it —
    see the note above _hw_executor. The executor is shut down afterward so
    that worker thread actually exits once the context is popped.
    """
    global _camera, _inference, _camera_initialized, _engine_stopping

    # The stream engine can outlive every browser, so nothing else ends it at
    # shutdown. Stop it first, or it would go on asking for frames from a
    # camera that is about to be closed.
    _engine_stopping = True
    task = _engine_task
    if task is not None and not task.done():
        try:
            task.get_loop().call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass  # loop already closed

    def _do_shutdown():
        with _hardware_lock:
            if _camera is not None:
                try:
                    _camera.stop_grabbing()
                    _camera.close()
                except Exception as exc:
                    logger.warning("Camera shutdown error: %s", exc)
            if _inference is not None:
                try:
                    _inference.cleanup()
                except Exception as exc:
                    logger.warning("Inference shutdown error: %s", exc)

    try:
        _run_on_hw_thread(_do_shutdown)
    finally:
        _camera = None
        _inference = None
        _camera_initialized = False
        _hw_executor.shutdown(wait=True)


def is_model_loaded() -> bool:
    """Whether an inference model is loaded and ready to detect.

    Exposed for the scan start gate (app/api/scan.py): the belt must not be
    told to move while this is False, or material crosses the camera with no
    inspection and no frame record. Reads the same flag camera_health() and
    camera_status() already report, so there is a single source of truth.
    """
    return bool(_inference and _inference.is_loaded)


def camera_health() -> dict:
    return {
        "initialized": _camera_initialized,
        "model_loaded": is_model_loaded(),
        "stream_clients": _stream_clients,
        "stream_engine_running": bool(_engine_task and not _engine_task.done()),
    }


# ---------------------------------------------------------------------------
# Stream engine
#
# ONE loop grabs frames, runs inference and feeds the detection state machine,
# however many browsers are watching - including none. Browsers are only
# subscribers: each gets its own small outbox and its own sender task, and the
# engine never waits on a browser.
#
# It used to be the other way round: the loop lived inside each websocket
# handler and awaited every send, so the detector ran at the pace of the
# slowest screen. A kiosk browser that stopped reading (out of memory, swapping)
# stopped the grab loop with it - no detection and no saved frames while the
# belt kept running - and a second tab meant a second loop feeding the same
# tracker.
# ---------------------------------------------------------------------------

# How long one send to a browser may take before that browser is given up on.
# Generous on purpose: a full-resolution review frame over a slow remote link
# can legitimately take many seconds. It exists so a connection that has gone
# away without closing cannot stay subscribed, and keep the engine running,
# for ever. A browser dropped this way reconnects by itself when it recovers.
STREAM_SEND_TIMEOUT = 60.0


class _Subscriber:
    """One connected browser's outbox.

    Holds at most ONE frame message and ONE state message, so a browser that
    falls behind costs a fixed amount of memory and simply skips the frames it
    was too slow for - it can never build a backlog and never slows the engine.

    The two slots keep the order the engine produced them in: a new frame
    supersedes everything before it (an older state would describe an older
    picture), while a state arriving after a frame is kept alongside it and is
    sent after it. So the review frame, which is only ever produced once per
    review screen, is not lost to the state ticks that follow it.
    """

    __slots__ = ("frame", "state", "wake", "sent_frozen_seq", "kicked")

    def __init__(self):
        self.frame = None
        self.state = None
        self.wake = asyncio.Event()
        # Which frozen review frame this browser has already been sent. Per
        # browser, so one that connects (or reconnects) in the middle of a
        # review still gets the frame under review.
        self.sent_frozen_seq = None
        self.kicked = False

    def put_frame(self, message: bytes):
        self.frame = message
        self.state = None
        self.wake.set()

    def put_state(self, payload: dict):
        self.state = payload
        self.wake.set()

    def kick(self):
        self.kicked = True
        self.wake.set()


# Touched only from the event loop thread.
_subscribers: set = set()
_engine_task = None
_engine_stopping = False
# Browsers on the Data Collection preview, which grabs from the same camera.
_dc_clients = 0


def _detection_armed() -> bool:
    """True while the belt has been started under detection and not stopped.

    This is the one situation in which the engine keeps running with no browser
    connected: material is moving under the camera, so it still has to be
    inspected (and the belt stopped on a detection) whether or not a screen is
    there to show it. A scan becomes active on the Start press; a manual Stop
    or a detection pauses capture; after a review the scan stays suspended
    until Start is pressed again.

    Not while the Data Collection preview is open, though: it grabs from the
    same camera, and two loops would each get only some of the frames.
    """
    return (scan_session.active
            and not scan_session.capture_paused
            and not scan_session.detection_suspended
            and _dc_clients == 0)


def _ensure_engine():
    """Start the engine if it is not running. Call from the event loop."""
    global _engine_task
    if _engine_stopping:
        return
    if _engine_task is None or _engine_task.done():
        _engine_task = asyncio.get_running_loop().create_task(_stream_engine())


async def _stream_engine():
    """Grab -> infer -> detect, and hand the result to whoever is subscribed.

    Runs while at least one browser is subscribed, or while detection is armed
    (see _detection_armed). Ends by itself otherwise, exactly as the old
    per-connection loop ended when its browser left; the next connection
    starts it again.
    """
    loop = asyncio.get_running_loop()
    logger.info("Stream engine started")

    frame_interval = 1.0 / max(1, settings.STREAM_FPS)
    decimation = max(1, settings.CAMERA_FRAME_DECIMATION)
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.STREAM_JPEG_QUALITY]

    fps_counter = 0
    fps_timer = time.time()
    current_fps = 0.0
    fps_log_timer = time.time()
    raw_frame_index = 0
    # Per-stage latency, accumulated between the periodic FPS logs so the
    # same 10s log line also shows where the time within each inferred
    # frame actually goes (grab vs. inference vs. the detection/crop logic).
    stage_totals = {"grab": 0.0, "infer": 0.0, "process": 0.0}
    stage_count = 0
    # The frozen review frame, encoded once and reused for every browser that
    # still needs it. Keyed by frozen_frame_seq rather than a bool "already
    # sent it": a queued detection is promoted while capture stays paused
    # throughout, so the frame changes without the pause ever ending.
    review_cache = None
    review_cache_seq = None
    was_paused = False
    last_sent = 0.0
    # When the interlock was first seen LOCKED with nothing to review. See the
    # self-heal block in the paused branch below.
    stuck_lock_since = None
    # Whether the engine is currently running with no browser connected.
    headless = False

    def encode_display(img):
        out = img
        if settings.STREAM_MAX_WIDTH and out.shape[1] > settings.STREAM_MAX_WIDTH:
            scale = settings.STREAM_MAX_WIDTH / out.shape[1]
            out = cv2.resize(out, (settings.STREAM_MAX_WIDTH, int(out.shape[0] * scale)))
        ok, buf = cv2.imencode(".jpg", out, encode_params)
        if not ok:
            return None
        return buf.tobytes(), int(out.shape[1]), int(out.shape[0])

    def encode_review(img):
        """The frozen review frame — full resolution, no width cap.

        Everything else goes through encode_display, which caps at
        STREAM_MAX_WIDTH to keep the live stream affordable. This one is
        encoded once per review screen, with the belt already stopped, and the
        review panel cuts each object's thumbnail straight out of it on the
        client. Capped at 1280 a ~70px object would arrive as ~47px and be
        magnified from there; at full width it is the real thing.
        """
        ok, buf = cv2.imencode(
            ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, settings.REVIEW_JPEG_QUALITY])
        if not ok:
            return None
        return buf.tobytes(), int(img.shape[1]), int(img.shape[0])

    # Grab and infer are separate steps, each taking the lock for its
    # own single-owner resource (the camera handle, the TensorRT
    # context), so a frame about to be decimated away never reaches
    # inference at all. Both must still run on _hw_executor rather than
    # the default pool — see the note above _hw_executor's definition.
    def grab():
        with _hardware_lock:
            return _camera.grab_frame()

    def infer(img):
        with _hardware_lock:
            return _inference.predict(img)

    try:
        while not _engine_stopping:
            if not _subscribers:
                if not _detection_armed():
                    break
                if not headless:
                    headless = True
                    logger.warning(
                        "No browser is connected but the scan is running — "
                        "detection continues without a viewer"
                    )
            elif headless:
                headless = False
                logger.info("A browser is connected again")

            try:
                # Port of cam_thread.capture_paused (GrabImage.py:95): while a
                # detection is under review, or the belt has been stopped, legacy
                # grabs no frames at all — so no inference runs, no track ids are
                # minted, and the operator reviews a frame frozen at the moment of
                # detection. Streaming live frames here instead would (a) keep
                # producing detections off a stationary belt and (b) leave the
                # box overlay drawn over a newer frame than it was computed on.
                if scan_session.capture_paused:
                    # Self-heal the one state the operator cannot get out of:
                    # capture paused AND the interlock locked AND nothing to
                    # review. A manual Stop landing in the same moment as an FM
                    # detection leaves exactly this — Stop pauses capture, so the
                    # detection's "look again once it settles" cycle never gets
                    # the frames it needs, never emits a review, and never
                    # unlocks. The belt stays locked forever and the UI disables
                    # both Start and Stop.
                    #
                    # A real review always has pending (or a queue) behind it, and
                    # a plain manual Stop never locks the interlock, so this
                    # combination is unambiguous. The delay keeps the normal
                    # ~1-2s settle window from ever tripping it.
                    if (conveyor_service.machine_start_locked
                            and not scan_session.pending
                            and not scan_session.detection_queue):
                        if stuck_lock_since is None:
                            stuck_lock_since = time.time()
                        elif time.time() - stuck_lock_since > STUCK_LOCK_SECONDS:
                            logger.warning(
                                "Interlock locked with nothing to review for %.0fs "
                                "— clearing it (a manual Stop most likely raced an "
                                "FM detection). The belt is stopped; press "
                                "Start to resume.",
                                time.time() - stuck_lock_since,
                            )
                            # Off the event loop: force_recover now stops the
                            # belt, and a conveyor command can take seconds
                            # (up to 3 retries, or reopening the port). Called
                            # inline it would stall every stream and every
                            # request for that whole time.
                            await loop.run_in_executor(
                                None, scan_session.force_recover
                            )
                            stuck_lock_since = None
                    else:
                        stuck_lock_since = None

                    payload = {
                        "fps": 0.0,
                        "model_loaded": is_model_loaded(),
                        **scan_session.live_state(),
                    }
                    was_paused = True
                    frozen_seq = scan_session.frozen_frame_seq
                    if review_cache_seq != frozen_seq:
                        review_cache = None
                        review_cache_seq = frozen_seq
                    waiting = [s for s in _subscribers if s.sent_frozen_seq != frozen_seq]
                    if waiting and review_cache is None:
                        frozen = scan_session.pending_frame
                        if frozen is not None:
                            enc = await loop.run_in_executor(None, encode_review, frozen)
                            if enc:
                                review_cache = enc + (
                                    int(frozen.shape[1]), int(frozen.shape[0]))
                    review_message = None
                    if waiting and review_cache is not None:
                        review_jpeg, disp_w, disp_h, src_w, src_h = review_cache
                        review_message = _pack_frame({
                            **payload,
                            "kind": "review",
                            "frame_width": disp_w,
                            "frame_height": disp_h,
                            "source_width": src_w,
                            "source_height": src_h,
                            "detections": [],
                        }, review_jpeg)
                    for sub in list(_subscribers):
                        if sub.sent_frozen_seq != frozen_seq:
                            # Marked as sent even when there is no frame to
                            # send (e.g. a manual STOP has no pending_frame):
                            # the browser then gets no frame at all and simply
                            # holds the last one it has.
                            sub.sent_frozen_seq = frozen_seq
                            if review_message is not None:
                                sub.put_frame(review_message)
                                continue
                        sub.put_state(payload)
                    await asyncio.sleep(0.2)
                    continue

                # Capture is live, so nothing is wedged — drop any pending
                # stuck-lock timer (a lock held while grabbing is a normal
                # in-flight detection, not the deadlock).
                stuck_lock_since = None

                if was_paused:
                    # Just resumed — don't average the paused interval into FPS.
                    was_paused = False
                    review_cache = None
                    review_cache_seq = None
                    for sub in _subscribers:
                        sub.sent_frozen_seq = None
                    fps_counter = 0
                    fps_timer = time.time()
                    last_sent = 0.0

                _t_grab_start = time.perf_counter()
                frame = await loop.run_in_executor(_hw_executor, grab)
                _t_grab_end = time.perf_counter()
                if frame is None:
                    await asyncio.sleep(0.02)
                    continue

                if settings.USE_MOCK_CAMERA:
                    # The real camera's grab blocks until the sensor delivers,
                    # which is what paces this loop on the device. The mock returns
                    # instantly, so it needs pacing of its own or the loop spins a
                    # core flat out now that nothing else throttles it.
                    await asyncio.sleep(frame_interval / decimation)

                raw_frame_index += 1
                # Legacy processed every 2nd frame (GrabImage.py:117). The check
                # sits BEFORE inference rather than after it: running predict() and
                # then discarding the result spends a full TensorRT pass on a frame
                # nothing ever looks at, halving the detection rate the GPU could
                # otherwise sustain. Set CAMERA_FRAME_DECIMATION=1 to feed the
                # tracker every frame the camera delivers.
                if raw_frame_index % decimation != 0:
                    continue

                _t_infer_start = time.perf_counter()
                detections, annotated = await loop.run_in_executor(_hw_executor, infer, frame)
                _t_infer_end = time.perf_counter()

                # Detection state machine — belt stop, interlock, crops, counting.
                state = await loop.run_in_executor(
                    None, scan_session.process_frame, frame, detections
                )
                _t_process_end = time.perf_counter()

                stage_totals["grab"] += _t_grab_end - _t_grab_start
                stage_totals["infer"] += _t_infer_end - _t_infer_start
                stage_totals["process"] += _t_process_end - _t_infer_end
                stage_count += 1

                # Counts frames actually put through detection, which is the rate
                # that matters for whether an object can cross the view unseen —
                # it is no longer the same as the rate frames reach the browser.
                fps_counter += 1
                elapsed = time.time() - fps_timer
                if elapsed >= 1.0:
                    current_fps = fps_counter / elapsed
                    fps_counter = 0
                    fps_timer = time.time()
                    # The detection rate only existed in the websocket payload,
                    # which means it could only ever be read off a browser that
                    # happened to be open at the time. It decides whether an object
                    # can cross the view unseen, so it belongs in the journal
                    # alongside everything else about a scan. Every 10s, not every
                    # second, so it stays readable next to the detection lines.
                    if time.time() - fps_log_timer >= 10.0:
                        fps_log_timer = time.time()
                        logger.info(
                            "Detection rate: %.1f fps (decimation=%s, "
                            "%.0f ms per inferred frame)",
                            current_fps, decimation,
                            1000.0 / current_fps if current_fps else 0,
                        )
                        if stage_count:
                            logger.info(
                                "Latency breakdown (avg over %d frames): "
                                "grab=%.0f ms, infer=%.0f ms, process=%.0f ms, "
                                "total=%.0f ms",
                                stage_count,
                                1000.0 * stage_totals["grab"] / stage_count,
                                1000.0 * stage_totals["infer"] / stage_count,
                                1000.0 * stage_totals["process"] / stage_count,
                                1000.0 * sum(stage_totals.values()) / stage_count,
                            )
                        stage_totals = {"grab": 0.0, "infer": 0.0, "process": 0.0}
                        stage_count = 0

                # Detection is done for this frame. Everything below is only
                # about showing it, so with nobody watching there is nothing
                # left to do.
                if not _subscribers:
                    continue

                # Display is throttled to STREAM_FPS; DETECTION is not. This loop
                # used to sleep out the remainder of a STREAM_FPS interval on every
                # iteration, which pinned the tracker's input rate to the browser's
                # refresh rate — at STREAM_FPS=20 with decimation 2 that is 10 Hz,
                # few enough frames that a small object can cross the field of view
                # in one or two of them, or between two of them. The operator does
                # not need more than STREAM_FPS frames a second; the tracker needs
                # every frame it can get.
                now = time.time()
                # A detection that has just stopped the belt goes out immediately
                # instead of waiting out the display interval — that frame is the
                # one the operator reviews, and pause_capture freezes the stream on
                # it a moment later.
                if not state.get("fm_detected") and now - last_sent < frame_interval:
                    continue
                last_sent = now

                # Encoded off the event loop, as the paused branch above already
                # does: a full-resolution JPEG encode inline stalls the next grab,
                # which is the one thing this loop cannot afford.
                enc = await loop.run_in_executor(
                    None, encode_display, annotated if annotated is not None else frame
                )
                if enc is None:
                    continue
                jpeg, display_width, display_height = enc

                # Packed once and handed to every outbox. Nothing here waits on
                # a browser: a slow one just has this frame replaced by the
                # next before it was ever sent.
                message = _pack_frame({
                    "kind": "live",
                    "frame_width": display_width,
                    "frame_height": display_height,
                    "source_width": int(frame.shape[1]),
                    "source_height": int(frame.shape[0]),
                    "detections": [[float(v) for v in d] for d in (detections or [])],
                    "fps": round(current_fps, 1),
                    "model_loaded": is_model_loaded(),
                    **state,
                }, jpeg)
                for sub in list(_subscribers):
                    sub.put_frame(message)

            except Exception:
                # The old per-connection loop ended on any error and closed its
                # websocket, and the browser reconnected a few seconds later.
                # Same here: connected browsers are dropped so they reconnect.
                # If detection is armed the loop carries on after a pause
                # rather than leaving a moving belt uninspected.
                if _engine_stopping:
                    break
                logger.exception("Stream engine error")
                for sub in list(_subscribers):
                    sub.kick()
                _subscribers.clear()
                await asyncio.sleep(1.0)
    finally:
        logger.info("Stream engine stopped")


async def _pump(websocket: WebSocket, sub: _Subscriber):
    """Send one browser whatever is in its outbox, at that browser's own pace.

    This is the only place that waits on a browser. While a send is in flight
    the engine keeps overwriting the outbox, so when the send completes the
    next thing to go out is the newest frame, not the next one in a queue.
    """
    while True:
        await sub.wake.wait()
        sub.wake.clear()
        if sub.kicked:
            return
        frame, sub.frame = sub.frame, None
        state, sub.state = sub.state, None
        if frame is not None:
            await asyncio.wait_for(websocket.send_bytes(frame), STREAM_SEND_TIMEOUT)
        if state is not None:
            await asyncio.wait_for(websocket.send_json(state), STREAM_SEND_TIMEOUT)


async def _wait_for_disconnect(websocket: WebSocket):
    """Return when the browser closes the socket. The page never sends
    anything on this stream, so anything else that arrives is ignored."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


# ---------------------------------------------------------------------------
# WebSocket stream
# ---------------------------------------------------------------------------

@ws_router.websocket("/ws/camera/stream")
async def camera_stream(websocket: WebSocket):
    """Stream JPEG frames plus the live scan state.

    This handler does no grabbing or detecting of its own: it subscribes the
    browser to the stream engine above and forwards what the engine produces.

    Two kinds of message, told apart by websocket frame type:

    * BINARY - a frame and the state that describes it, packed by
      _pack_frame(): 4-byte big-endian header length, JSON header, JPEG bytes.
      The header is the scan-state snapshot (fm_detected, frame_fm_count,
      total_fo_detected, frame_count, machine_start_locked, capture_paused,
      pending, queue_depth, labelled, awaiting_label) plus:
          kind           "live"   - annotated display frame, width-capped
                         "review" - the frozen frame under review, full size
          frame_width, frame_height    size of the JPEG itself
          source_width, source_height  sensor size; `pending` boxes are in
                                       these coordinates
          detections     [[x1,y1,x2,y2,conf,cls], ...]
          fps            detection rate
      A review frame is sent once per review screen, not repeatedly.

    * TEXT (JSON) - no frame: {"error": ...} before closing, {"warning": ...}
      once at connect, and the state snapshot alone (with "fps": 0.0) about
      five times a second while capture is paused. A paused message without a
      frame means "keep showing the frame you have".
    """
    global _stream_clients, _last_viewer_gone_at

    await websocket.accept()
    logger.info("WebSocket client connected to /ws/camera/stream")

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(_hw_executor, _ensure_services)
    if not ok:
        await websocket.send_json({"error": "Camera not available"})
        await websocket.close()
        return

    if not _inference.is_loaded:
        # Detection silently disabled is worse than a visible warning.
        await websocket.send_json({
            "warning": "No inference model loaded. Call POST /api/camera/model "
                       "with the commodity and variety before starting a scan."
        })

    sub = _Subscriber()
    _subscribers.add(sub)
    with _clients_lock:
        _stream_clients += 1
        _last_viewer_gone_at = None
    _ensure_engine()

    pump = asyncio.ensure_future(_pump(websocket, sub))
    watch = asyncio.ensure_future(_wait_for_disconnect(websocket))
    try:
        done, _ = await asyncio.wait({pump, watch}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is None:
                if task is watch:
                    logger.info("WebSocket client disconnected")
            elif isinstance(exc, WebSocketDisconnect):
                logger.info("WebSocket client disconnected")
            elif isinstance(exc, asyncio.TimeoutError):
                logger.warning(
                    "Stream client took no data for %.0fs — dropping it "
                    "(detection is unaffected; the browser reconnects by itself)",
                    STREAM_SEND_TIMEOUT,
                )
            else:
                logger.error("WebSocket error: %s", exc)
    finally:
        _subscribers.discard(sub)
        with _clients_lock:
            _stream_clients -= 1
            if _stream_clients <= 0:
                _last_viewer_gone_at = time.monotonic()
        for task in (pump, watch):
            task.cancel()
        await asyncio.gather(pump, watch, return_exceptions=True)
        # The server does not close a websocket just because its handler
        # returns. Without this, a browser dropped from this side (engine
        # error, send timeout) would sit on a silent, still-open connection
        # instead of seeing it close and reconnecting. Bounded, because a
        # browser that is not reading cannot be sent a close frame either.
        try:
            await asyncio.wait_for(websocket.close(), 2.0)
        except Exception:
            pass
        logger.info("WebSocket stream ended")


@ws_router.websocket("/ws/data_collection/stream")
async def data_collection_stream(websocket: WebSocket):
    """Live preview for the Data Collection page — no inference, no scan_session.

    Legacy only ever emitted a preview frame from inside CollectionCameraThread.run()
    (GrabImage.py:733-826), i.e. only while recording, and this port matched that
    exactly at first. Changed on request: arriving at the page showed a grey
    placeholder until Start was pressed, which read as a broken/frozen camera
    rather than "recording hasn't started yet" — legacy's own live-scan page
    (Dashboard, /ws/camera/stream) shows its feed immediately for the same
    reason. A deliberate deviation from legacy, not a bug fix.

    A frame is now grabbed and sent on every iteration regardless of
    `_dc_recording`; only the disk write (the actual "data collection" part)
    stays gated on it, unchanged from before.
    """

    await websocket.accept()
    logger.info("WebSocket client connected to /ws/data_collection/stream")

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(_hw_executor, _ensure_services)
    if not ok:
        await websocket.send_json({"error": "Camera not available"})
        await websocket.close()
        return

    encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.STREAM_JPEG_QUALITY]

    global _dc_writer, _dc_frames_seen, _dc_pending_writes, _dc_frames_dropped
    global _dc_clients
    me = object()
    with _dc_lock:
        if _dc_writer is None:
            _dc_writer = me
    if _dc_writer is not me:
        logger.warning("Second data collection stream: preview only, not saving")

    frame_interval = 1.0 / max(1, settings.STREAM_FPS)
    last_sent = 0.0
    last_disk_check = 0.0

    def grab():
        with _hardware_lock:
            return _camera.grab_frame()

    def stop_recording_because(reason: str) -> None:
        global _dc_stop_reason
        with _dc_lock:
            _dc_stop_reason = reason
            _dc_end_recording()
        logger.warning("Data collection stopped: %s", reason)
        conveyor_service.send("all_stop")

    # Counted so the scan stream engine does not keep grabbing from the same
    # camera with no viewer while this page is open — see _detection_armed.
    _dc_clients += 1
    try:
        while True:
            # Every frame the camera delivers is grabbed and counted; only the
            # preview below is throttled to STREAM_FPS. Saving used to sit
            # inside the preview's sleep, which capped collection at
            # STREAM_FPS frames a second whatever the camera could do.
            frame = await loop.run_in_executor(_hw_executor, grab)
            if frame is None:
                await asyncio.sleep(0.02)
                continue
            if settings.USE_MOCK_CAMERA:
                # The real grab blocks until the sensor delivers, which paces
                # this loop; the mock returns at once and needs pacing.
                await asyncio.sleep(0.01)

            if _dc_writer is me and _dc_recording and _dc_folder:
                now = time.time()
                if now - last_disk_check >= 2.0:
                    last_disk_check = now
                    free = await loop.run_in_executor(None, _dc_free_gb, _dc_folder)
                    if free < _dc_settings["min_free_disk_gb"]:
                        await loop.run_in_executor(
                            None, stop_recording_because,
                            f"Output disk almost full ({free:.1f} GB free).")
                if _dc_recording:
                    # Only every Nth recorded frame is written
                    # (data_collection_settings.json); the rest are counted
                    # so the summary can say how many went past.
                    with _dc_lock:
                        _dc_frames_seen += 1
                        keep = _dc_frames_seen % _dc_settings["save_every_n_frames"] == 0
                        folder = _dc_folder
                        if keep and _dc_pending_writes >= _DC_MAX_PENDING_WRITES:
                            _dc_frames_dropped += 1
                            keep = False
                        elif keep:
                            _dc_pending_writes += 1
                    if keep:
                        # Same array, no colour conversion, as legacy's
                        # CollectionCameraThread.run (GrabImage.py:780-782).
                        ext = _dc_settings["image_format"]
                        path = os.path.join(
                            folder, f"{os.path.basename(folder)}_{time.time()}.{ext}")
                        params = ([cv2.IMWRITE_JPEG_QUALITY, _dc_settings["jpeg_quality"]]
                                  if ext == "jpg" else [])
                        _dc_write_executor.submit(_dc_write, path, frame, params)

            now = time.time()
            if now - last_sent < frame_interval:
                continue
            last_sent = now
            ok, buffer = await loop.run_in_executor(
                None, cv2.imencode, ".jpg", frame, encode_params)
            if not ok:
                continue
            await websocket.send_json({
                "frame": base64.b64encode(buffer).decode("utf-8"),
                "frame_count": _dc_frame_count,
                "frames_seen": _dc_frames_seen,
                "frames_dropped": _dc_frames_dropped,
                "recording": _dc_recording,
                "stop_reason": _dc_stop_reason,
            })

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected from data collection stream")
    except Exception as exc:
        logger.error("Data collection WebSocket error: %s", exc)
    finally:
        _dc_clients -= 1
        # Losing the saving stream mid-recording (app closed or crashed,
        # network gone) would otherwise leave the belt running with nothing
        # being saved. Stop both, as the live scan does when its stream goes.
        was_recording = False
        with _dc_lock:
            if _dc_writer is me:
                _dc_writer = None
                was_recording = _dc_recording
                _dc_end_recording()
        if was_recording:
            logger.warning("Data collection stream lost while recording: stopping the belt")
            await loop.run_in_executor(None, conveyor_service.send, "all_stop")


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@router.get("/status")
def camera_status():
    return {
        "camera_initialized": _camera_initialized,
        "model_loaded": bool(_inference and _inference.is_loaded),
        "mock_mode": settings.USE_MOCK_CAMERA,
        "stream_fps_target": settings.STREAM_FPS,
        "jpeg_quality": settings.STREAM_JPEG_QUALITY,
        "stream_clients": _stream_clients,
    }


class ModelSwitchRequest(BaseModel):
    commodity: str
    variety: str = ""


@router.post("/model")
def switch_model(req: ModelSwitchRequest):
    """Load the inference model for a commodity/variety.

    Returns 503 on failure rather than {"success": false}: a scan running with
    no model produces zero detections and looks like a clean sample.
    """
    def _do():
        if not _ensure_services():
            raise HTTPException(status_code=503, detail="Camera/inference services unavailable")
        with _hardware_lock:
            return _inference.load_model(req.commodity, req.variety)

    # Must run on _hw_executor, not whatever thread FastAPI assigns this
    # request — see the note above _hw_executor's definition.
    ok = _run_on_hw_thread(_do)

    if not ok:
        raise HTTPException(
            status_code=503,
            detail=f"Could not load a model for commodity={req.commodity!r} "
                   f"variety={req.variety!r}. Check MODEL_DIR and the model files.",
        )
    return {"success": True, "commodity": req.commodity, "variety": req.variety}


@router.post("/start")
def start_camera():
    if not _run_on_hw_thread(_ensure_services):
        raise HTTPException(status_code=503, detail="Camera initialization failed")
    return {"success": True, **camera_health()}


@router.post("/stop")
def stop_camera():
    """Stop acquisition. Legacy paired this with a 1s conveyor deceleration
    delay (stop_camera_with_delay, main.py:720-739)."""
    global _camera_initialized

    def _do():
        with _hardware_lock:
            if _camera is not None:
                _camera.stop_grabbing()

    _run_on_hw_thread(_do)
    _camera_initialized = False
    return {"success": True}


# ---------------------------------------------------------------------------
# Data Collection
# ---------------------------------------------------------------------------

class DataCollectionRequest(BaseModel):
    sample_id: str
    commodity: str
    variety: str = ""


@router.post("/data_collection/prepare")
def prepare_data_collection(req: DataCollectionRequest):
    """Create the output folder for this visit to the Data Collection page.

    Port of goto_dc_page (main.py:1843-1862): the folder is created once, from
    <OUTPUT_DIR>/Data_Collection/<commodity>/<variety>/<epoch>_<sample_id>, and
    every Start/capture Image press during this visit writes into it — it is
    not recreated per button press.
    """
    global _dc_folder, _dc_frame_count, _dc_frames_seen, _dc_settings
    global _dc_started_at, _dc_stopped_at, _dc_frames_dropped, _dc_stop_reason
    global _dc_meta

    # The three values ARE the folder path. A blank one produces a path with an
    # empty segment — <OUTPUT_DIR>/Data_Collection///<epoch>_ — so the session's
    # frames land somewhere nothing identifies and nobody can find them again
    # afterwards, which is the entire point of collecting them. The form
    # disables its own Next button for the same reason (DetailsEntry.jsx); this
    # also covers reaching the page with the form state lost, e.g. a reload.
    missing = [
        name for name, value in (
            ("sample_id", req.sample_id), ("commodity", req.commodity),
            ("variety", req.variety),
        ) if not (value or "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot start data collection without {', '.join(missing)}.",
        )

    # The sample id is used verbatim as part of a directory name below, so it
    # is restricted to characters that cannot change where that directory ends
    # up: a "/" would silently nest the session inside extra folders and a
    # ".." would put it outside OUTPUT_DIR altogether. The Add Details form
    # filters the same set as the operator types (DetailsEntry.jsx); this is
    # what makes the rule true regardless of how the request was made.
    sample_id = req.sample_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sample_id):
        raise HTTPException(
            status_code=422,
            detail="Sample id may contain only letters, numbers, hyphens and "
                   "underscores.",
        )

    unique = f"{int(time.time())}_{sample_id}"
    root = os.path.join(settings.OUTPUT_DIR, "Data_Collection")
    folder = os.path.join(
        root,
        normalize_commodity(req.commodity), normalize_commodity(req.variety),
        unique,
    )
    # Commodity and variety come from the synced Qualix config rather than
    # being typed, but they are joined into this path just the same, so the
    # result is confirmed to be inside the output directory before anything is
    # created — the same containment check history.py applies before serving a
    # crop.
    if not os.path.realpath(folder).startswith(os.path.realpath(root) + os.sep):
        raise HTTPException(
            status_code=422,
            detail="Commodity or variety is not usable as a folder name.",
        )
    # The previous visit's run, if it was never submitted (its page was left
    # with Back). Still recording, it would carry on writing into THIS visit's
    # folder before Start was pressed. Recording only; the belt is not touched.
    _dc_close_run("interrupted")
    # The folder itself is created by the first Start or Capture
    # (_dc_open_run), so a visit that records nothing leaves nothing behind.
    with _dc_lock:
        _dc_folder = folder
        _dc_meta = {"sample_id": sample_id, "commodity": req.commodity,
                    "variety": req.variety}
        _dc_frame_count = 0
        _dc_frames_seen = 0
        _dc_started_at = None
        _dc_stopped_at = None
        _dc_frames_dropped = 0
        _dc_stop_reason = None
        _dc_settings = _load_dc_settings()
    logger.info("Data collection prepared: %s (settings %s)", folder, _dc_settings)
    return {"success": True, **_dc_status()}


@router.post("/data_collection/start")
def start_data_collection():
    """Port of start_dc (main.py:1864-1869): start the belt, start saving frames.

    Legacy calls self.start_conveyor() and self.start_dc_camera() back to back
    without checking the first call's return value at all — a belt that fails
    to ack does not stop frames from being grabbed and saved. Recording here
    is not gated on the conveyor call either, for the same reason: this is a
    raw capture tool, and refusing to record because the belt didn't answer
    would be stricter than the code it is meant to reproduce.
    """
    if _dc_folder is None:
        raise HTTPException(status_code=409, detail="Call prepare before starting.")
    if _dc_writer is None:
        # Frames are only saved inside the stream loop, so with no stream
        # open the belt would run and nothing would be recorded.
        raise HTTPException(
            status_code=409,
            detail="Camera preview is not connected; nothing would be saved.",
        )
    if scan_session.active:
        raise HTTPException(
            status_code=409,
            detail="A batch inspection is in progress; finish it first.",
        )
    min_free = _dc_settings["min_free_disk_gb"]
    free = _dc_free_gb(_dc_existing_dir(_dc_folder))
    if free < min_free:
        raise HTTPException(
            status_code=507,
            detail=f"Only {free:.1f} GB free on the output disk "
                   f"(needs {min_free:g} GB). Free up space first.",
        )
    if not _run_on_hw_thread(_ensure_services):
        raise HTTPException(status_code=503, detail="Camera not available")
    # Folder and record before the belt moves: if they cannot be created, the
    # request fails with the belt still stopped. A run left empty because the
    # belt then fails to start is removed when it is closed.
    _dc_open_run()
    # Deliberately stricter than legacy, which ignored this result: showing
    # "Recording" over a belt that never moved collects the same few grains
    # over and over.
    if not conveyor_service.send("machine_start"):
        conveyor_service.send("all_stop")
        raise HTTPException(
            status_code=502,
            detail="Belt did not start: "
                   f"{conveyor_service.status().get('last_error') or 'no acknowledgment'}",
        )
    global _dc_stop_reason
    with _dc_lock:
        _dc_stop_reason = None
        _dc_begin_recording()
    return {"success": True, **_dc_status()}


@router.post("/data_collection/stop")
def stop_data_collection():
    """Port of stop_dc (main.py:1926-1931): stop saving frames, then the belt."""
    with _dc_lock:
        _dc_end_recording()
    conveyor_service.send("all_stop")
    status = _dc_status()
    if _dc_run_open and _dc_folder:
        dc_history.record_update(_dc_folder, status)
    return {"success": True, **status}


@router.post("/data_collection/capture")
def capture_image_data_collection():
    """Port of capture_image_dc (main.py:2004-2010): record for 2s, then stop.

    Only the camera thread is touched here, same as legacy — the conveyor is
    not started or stopped by this action.
    """
    if _dc_folder is None:
        raise HTTPException(status_code=409, detail="Call prepare before capturing.")
    if not _run_on_hw_thread(_ensure_services):
        raise HTTPException(status_code=503, detail="Camera not available")
    _dc_open_run()
    folder = _dc_folder
    with _dc_lock:
        _dc_begin_recording()
    time.sleep(2.0)
    with _dc_lock:
        _dc_end_recording()
    status = _dc_status()
    # Finish may have closed the run during the sleep; it recorded it then.
    if folder and _dc_folder == folder:
        dc_history.record_update(folder, status)
    return {"success": True, **status}


@router.get("/data_collection/status")
def data_collection_status():
    return _dc_status()


@router.post("/data_collection/finish")
def finish_data_collection():
    """Legacy's submit_dc/back_from_dc (main.py:2012-2022) just navigate home
    without stopping the recording thread first — if the operator forgot to
    press Stop, the camera thread and belt keep running in the background.
    That is a leak in the original, not something worth reproducing here: this
    unconditionally stops both, then the caller navigates away same as legacy.
    """
    global _dc_folder, _dc_meta
    # Frames already handed to the writer land first, so the summary's
    # "captured" matches the files in the folder.
    summary = _dc_close_run("completed")
    with _dc_lock:
        _dc_folder = None
        _dc_meta = None
    conveyor_service.send("all_stop")
    logger.info(
        "Data collection finished: %s frames seen, %s saved (every %s), %.1fs, %s",
        summary["frames_seen"], summary["frame_count"],
        summary["save_every_n_frames"], summary["duration_seconds"],
        summary["folder"],
    )
    # The summary is what the client's completion popup shows: start/end
    # time, duration, frames that went past, and frames kept.
    return {"success": True, **summary}
