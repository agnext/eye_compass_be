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
import logging
import os
import re
import threading
import time

import cv2
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.config import settings
from app.services.camera_service import BaseCameraService, get_camera_service
from app.services.conveyor_service import conveyor_service
from app.services.inference_service import BaseInferenceService, get_inference_service, normalize_commodity
from app.services.scan_session import scan_session

logger = logging.getLogger(__name__)

router = APIRouter()
ws_router = APIRouter()

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

_hw_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-hw")


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
_dc_frame_count = 0


def _dc_status() -> dict:
    return {
        "recording": _dc_recording,
        "folder": _dc_folder,
        "frame_count": _dc_frame_count,
    }


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
    global _camera, _inference, _camera_initialized

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


def camera_health() -> dict:
    return {
        "initialized": _camera_initialized,
        "model_loaded": bool(_inference and _inference.is_loaded),
        "stream_clients": _stream_clients,
    }


# ---------------------------------------------------------------------------
# WebSocket stream
# ---------------------------------------------------------------------------

@ws_router.websocket("/ws/camera/stream")
async def camera_stream(websocket: WebSocket):
    """Stream annotated JPEG frames plus the live scan state.

    Message shape:
        {
          "frame": "<base64 jpeg>",
          "detections": [[x1,y1,x2,y2,conf,cls], ...],
          "fm_detected": bool,          # belt has just been stopped
          "pending": [ {index, box, confidence, class_id}, ... ],
          "awaiting_label": [int, ...],
          "total_fo_detected": int,     # unique objects for the whole run
          "machine_start_locked": bool,
          "fps": float
        }
    """
    global _stream_clients

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

    with _clients_lock:
        _stream_clients += 1

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
    # Which frozen review frame has already gone out, and whether the previous
    # iteration was a paused one. A bool "already sent it" is not enough: a
    # queued detection is promoted while capture stays paused throughout, so
    # the frame changes without the pause ever ending.
    sent_frozen_seq = None
    was_paused = False
    last_sent = 0.0

    def encode_display(img):
        out = img
        if settings.STREAM_MAX_WIDTH and out.shape[1] > settings.STREAM_MAX_WIDTH:
            scale = settings.STREAM_MAX_WIDTH / out.shape[1]
            out = cv2.resize(out, (settings.STREAM_MAX_WIDTH, int(out.shape[0] * scale)))
        ok, buf = cv2.imencode(".jpg", out, encode_params)
        if not ok:
            return None
        return base64.b64encode(buf).decode("utf-8"), int(out.shape[1]), int(out.shape[0])

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
        return (base64.b64encode(buf).decode("utf-8"),
                int(img.shape[1]), int(img.shape[0]))

    try:
        while True:
            # Port of cam_thread.capture_paused (GrabImage.py:95): while a
            # detection is under review, or the belt has been stopped, legacy
            # grabs no frames at all — so no inference runs, no track ids are
            # minted, and the operator reviews a frame frozen at the moment of
            # detection. Streaming live frames here instead would (a) keep
            # producing detections off a stationary belt and (b) leave the
            # box overlay drawn over a newer frame than it was computed on.
            if scan_session.capture_paused:
                payload = {"fps": 0.0, **scan_session.live_state()}
                was_paused = True
                frozen_seq = scan_session.frozen_frame_seq
                if sent_frozen_seq != frozen_seq:
                    frozen = scan_session.pending_frame
                    if frozen is not None:
                        enc = await loop.run_in_executor(None, encode_review, frozen)
                        if enc:
                            b64, disp_w, disp_h = enc
                            payload.update({
                                "frame": b64,
                                "frame_width": disp_w,
                                "frame_height": disp_h,
                                "source_width": int(frozen.shape[1]),
                                "source_height": int(frozen.shape[0]),
                                "detections": [],
                            })
                    # No pending_frame (e.g. a manual STOP): send no frame at
                    # all, so the client simply holds the last one it has.
                    sent_frozen_seq = frozen_seq
                await websocket.send_json(payload)
                await asyncio.sleep(0.2)
                continue

            if was_paused:
                # Just resumed — don't average the paused interval into FPS.
                was_paused = False
                sent_frozen_seq = None
                fps_counter = 0
                fps_timer = time.time()
                last_sent = 0.0

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
            b64_frame, display_width, display_height = enc

            await websocket.send_json({
                "frame": b64_frame,
                "frame_width": display_width,
                "frame_height": display_height,
                "source_width": int(frame.shape[1]),
                "source_height": int(frame.shape[0]),
                "detections": [[float(v) for v in d] for d in (detections or [])],
                "fps": round(current_fps, 1),
                **state,
            })

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        with _clients_lock:
            _stream_clients -= 1
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
    global _dc_frame_count

    await websocket.accept()
    logger.info("WebSocket client connected to /ws/data_collection/stream")

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(_hw_executor, _ensure_services)
    if not ok:
        await websocket.send_json({"error": "Camera not available"})
        await websocket.close()
        return

    encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.STREAM_JPEG_QUALITY]

    try:
        while True:
            def grab_and_maybe_save():
                with _hardware_lock:
                    frame = _camera.grab_frame()
                if frame is None or not _dc_recording or not _dc_folder:
                    return frame
                # Same array, same cv2.imwrite call as legacy's
                # CollectionCameraThread.run (GrabImage.py:780-782) — no color
                # conversion here in legacy either, so none is added here.
                image_name = time.time()
                filename = f"{os.path.basename(_dc_folder)}_{image_name}.png"
                cv2.imwrite(os.path.join(_dc_folder, filename), frame)
                return frame

            frame = await loop.run_in_executor(_hw_executor, grab_and_maybe_save)
            if frame is None:
                await asyncio.sleep(0.02)
                continue

            if _dc_recording:
                with _dc_lock:
                    _dc_frame_count += 1

            ok, buffer = cv2.imencode(".jpg", frame, encode_params)
            if not ok:
                continue
            await websocket.send_json({
                "frame": base64.b64encode(buffer).decode("utf-8"),
                "frame_count": _dc_frame_count,
                "recording": _dc_recording,
            })
            await asyncio.sleep(1.0 / max(1, settings.STREAM_FPS))

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected from data collection stream")
    except Exception as exc:
        logger.error("Data collection WebSocket error: %s", exc)


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
    global _dc_folder, _dc_frame_count

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
    os.makedirs(folder, exist_ok=True)
    with _dc_lock:
        _dc_folder = folder
        _dc_frame_count = 0
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
    global _dc_recording
    if _dc_folder is None:
        raise HTTPException(status_code=409, detail="Call prepare before starting.")
    if not _run_on_hw_thread(_ensure_services):
        raise HTTPException(status_code=503, detail="Camera not available")
    conveyor_service.send("machine_start")
    with _dc_lock:
        _dc_recording = True
    return {"success": True, **_dc_status()}


@router.post("/data_collection/stop")
def stop_data_collection():
    """Port of stop_dc (main.py:1926-1931): stop saving frames, then the belt."""
    global _dc_recording
    with _dc_lock:
        _dc_recording = False
    conveyor_service.send("all_stop")
    return {"success": True, **_dc_status()}


@router.post("/data_collection/capture")
def capture_image_data_collection():
    """Port of capture_image_dc (main.py:2004-2010): record for 2s, then stop.

    Only the camera thread is touched here, same as legacy — the conveyor is
    not started or stopped by this action.
    """
    global _dc_recording
    if _dc_folder is None:
        raise HTTPException(status_code=409, detail="Call prepare before capturing.")
    if not _run_on_hw_thread(_ensure_services):
        raise HTTPException(status_code=503, detail="Camera not available")
    with _dc_lock:
        _dc_recording = True
    time.sleep(2.0)
    with _dc_lock:
        _dc_recording = False
    return {"success": True, **_dc_status()}


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
    global _dc_recording, _dc_folder
    with _dc_lock:
        _dc_recording = False
        _dc_folder = None
    conveyor_service.send("all_stop")
    return {"success": True}
