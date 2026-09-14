"""
Scan lifecycle API.

    POST /api/scan/start     begin a run (creates output folders, resets counters)
    GET  /api/scan/status    live session state
    POST /api/scan/label     operator classifies one detected object
    POST /api/scan/resume    release the interlock and restart the belt
    POST /api/scan/forward   nudge the belt forward, then re-freeze it
    POST /api/scan/stop      manual belt stop (counted separately from FM stops)
    POST /api/scan/submit    finish the run and build the result — NOT persisted yet
    POST /api/scan/confirm   persist the last /submit result and queue the Qualix/Sheets sync
    POST /api/scan/cancel    discard the run without submitting

The result payload is assembled server-side from the session and the batch row.
It used to be built in the browser, which is why 17 of the 21 Qualix fields were
missing and why the Qualix POST never ran (it required credentials the frontend
did not send).

/submit and /confirm are deliberately two separate steps, matching legacy: on
the live-scan page, Submit (submit_video -> submit_create_result, main.py:1566-
1661) only computes the result, writes result.json, and shows the results page
— it does NOT save to the database or call Qualix/Sheets. That only happens
when the operator confirms from the results page (backtohome -> save_result,
main.py:1717-1812), which starts the actual post_analysis_data/write_results
call. If the operator never reaches that second click in legacy, the batch is
never saved or synced at all despite result.json existing on disk — a real
fragility, reproduced here rather than fixed, since an exact match was
requested over the earlier one-step version.
"""

import json
import logging
import os
import threading
import time
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.models.schema import BatchDetails
from app.services.conveyor_service import conveyor_service
from app.services.database_service import DatabaseService
from app.services.datagram import build_datagram
from app.services.scan_session import scan_session
from app.services.sync_service import sync_service

logger = logging.getLogger(__name__)
router = APIRouter()

# Holds the last /submit's computed result until /confirm persists it — the
# in-memory equivalent of legacy's self.datagram/self.final_result sitting on
# the appLogic instance between submit_create_result and save_result. Single
# slot: this machine has one operator and one active scan at a time, same
# assumption scan_session itself already makes.
_pending_submission: dict | None = None

# Guards the whole read-modify-write cycle on _pending_submission, not just the
# assignment. FastAPI runs plain `def` endpoints on a threadpool, so several
# /pending-crops/relabel calls genuinely execute in parallel — the reclassify
# screen fires one per staged change, ~20 within a few seconds in a real
# observed batch. Each call renames its crop (scan_session.relabel_crop takes
# the session lock for that part) and then RE-COUNTS THE WHOLE FOLDER and
# overwrites this global with the totals it just measured. Without a lock
# spanning both halves, a request that measured the folder before a sibling's
# rename landed can finish last and write its now-stale counts over the
# sibling's fresher ones — and whatever is in this slot at /confirm is exactly
# what gets persisted and synced to Qualix. Confirmed live: batch milind4550's
# stored result claims FM: 4 while its folder holds only 2 FM-prefixed crops.
_pending_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ScanStartRequest(BaseModel):
    sample_id: str
    commodity: str
    variety: str = ""
    batch_id: Optional[int] = None
    # The FM vocabulary for this commodity, from /api/config/commodities.
    # create_results counts saved crops against exactly these names.
    analysis_parameters: List[str] = []


class LabelRequest(BaseModel):
    index: int
    fm_name: str


class SubmitRequest(BaseModel):
    blower_fo: str = "0"
    magnetic_fo: str = "0"
    surveyor_name: str = ""


class RelabelCropRequest(BaseModel):
    name: str
    fm_name: str


# ---------------------------------------------------------------------------
# Background sync
# ---------------------------------------------------------------------------

def sync_result_to_cloud(result_id: int, datagram: dict):
    """Deliver one result to Qualix and Google Sheets.

    Opens its OWN database session: the request-scoped session from
    Depends(get_db) is already closed by the time a BackgroundTask runs, so
    every write through it was silently failing.
    """
    db = SessionLocal()
    try:
        service = DatabaseService(db)

        post_status, error_code = ("0", "not_attempted")
        if sync_service.is_authenticated:
            post_status, error_code = sync_service.post_analysis_data(datagram)
        else:
            logger.warning(
                "No Qualix session — result %s stays unsynced and will be retried.",
                result_id,
            )

        # Sheets is a side channel. Legacy advanced sync_status on the Qualix
        # response alone (main.py:2930-2941); a Sheets success must not mask a
        # Qualix failure, or the retry worker never sees the record again.
        try:
            sync_service.post_to_sheets(datagram)
        except Exception as exc:
            logger.error("Sheets sync failed for result %s: %s", result_id, exc)

        service.set_sync_status(result_id, post_status)
        if post_status == "1":
            logger.info("Result %s synced to Qualix.", result_id)
        elif post_status == "2":
            logger.error(
                "Result %s rejected by Qualix (400) — marked '2', will not be retried.",
                result_id,
            )
        else:
            logger.warning(
                "Result %s not delivered (%s) — left at '0' for the retry worker.",
                result_id, error_code,
            )
    except Exception as exc:
        logger.error("Background sync for result %s failed: %s", result_id, exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/reset")
def reset_scan():
    """Entering the live-scan page for a batch, before Start is pressed.

    Port of on_next_click (main.py:658-661): unconditionally clears
    start_time_flag the moment the operator leaves the batch-details form for
    the live view, regardless of whether a previous batch's session was ever
    formally finished/cancelled. This is what start()'s active-batch guard
    (see scan_session.start) actually depends on to tell "resuming after
    dismissing an FM detection, same page" apart from "arrived at a fresh
    batch, possibly abandoning a previous one via Back with no cancel" — both
    look identical from start()'s point of view without this. Called once
    when the Dashboard page mounts, before Start can be pressed.

    Also unlocks the conveyor interlock — a deliberate deviation, not a
    legacy port (on_next_click touches no conveyor state at all). Confirmed
    live: a batch abandoned while `machine_start_locked` was engaged (and not
    exited via Cancel Batch or browser-Back, both of which already unlock —
    see enhancements.md) can leave a brand-new batch arriving at this exact
    page already locked, before Start is ever pressed, with no pending
    detections to Submit/Forward against to clear it — a genuine dead end for
    the operator. Unlocking here closes that last gap: arriving fresh at this
    page can never be blocked by a previous session's leftover lock.
    """
    with scan_session._lock:
        scan_session.reset()
    conveyor_service.unlock_machine_start(reason="new batch (page load)")
    return {"success": True}


@router.post("/start")
def start_scan(req: ScanStartRequest, db: Session = Depends(get_db)):
    """Begin a run and start the belt."""
    analysis = req.analysis_parameters
    if not analysis:
        # Fall back to the commodity's configured FM vocabulary.
        from app.models.schema import CommodityDetails

        row = (
            db.query(CommodityDetails)
            .filter(CommodityDetails.commodity == req.commodity)
            .first()
        )
        if row and row.analysis:
            analysis = [a for a in row.analysis if isinstance(a, str)]

    batch = None
    if req.batch_id:
        batch = db.query(BatchDetails).filter(BatchDetails.id == req.batch_id).first()

    status = scan_session.start(
        sample_id=req.sample_id,
        commodity=req.commodity,
        variety=req.variety,
        analysis_parameters=analysis,
        batch={"id": req.batch_id} if req.batch_id else {},
    )

    conveyor_service.unlock_machine_start(reason="new scan")
    conveyor_service.send("camera_on")
    started = conveyor_service.send("machine_start")

    return {"success": True, "conveyor_started": started, **status}


@router.get("/status")
def scan_status():
    return scan_session.status()


@router.post("/label")
def label_detection(req: LabelRequest):
    """Record the operator's classification of one detected object.

    This writes the crop to disk as <FM_name>_<timestamp>.png — the filename is
    what create_results counts, so this call IS the measurement.
    """
    try:
        return scan_session.label_detection(req.index, req.fm_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/resume")
def resume_scan():
    """Release the interlock and restart the belt after a detection is resolved."""
    if not scan_session.active:
        raise HTTPException(status_code=409, detail="No active scan")
    return scan_session.resume()


@router.post("/forward")
def forward_scan():
    """Port of move_conveyor_forward (main.py:851-883) — the live-scan page's
    own Forward button, distinct from the Data Collection page's (that one is
    forward_dc, already ported as /api/conveyor/forward: plain start-wait-stop,
    no interlock touch).

    This one is odd but that's what legacy actually does: unlock the
    interlock, send machine_start, wait 0.1s (a brief nudge, not the 2s jog
    used elsewhere), then send FM_detected — which immediately stops the belt
    again — and re-lock. Legacy's own comment calls the re-lock "intentional
    for forward button flow" (main.py:876): it nudges material slightly while
    reviewing a detection without actually resuming normal scanning.
    """
    if not scan_session.active:
        raise HTTPException(status_code=409, detail="No active scan")
    conveyor_service.unlock_machine_start(reason="forward jog (live scan)")
    # Legacy resumes capture for the jog (main.py:868) and pauses it again
    # once the jog has re-locked (main.py:888), so the operator gets a fresh
    # frozen frame of whatever the nudge brought into view.
    scan_session.resume_capture()
    conveyor_service.send("machine_start")
    time.sleep(0.1)
    conveyor_service.send("FM_detected")
    conveyor_service.lock_machine_start(reason="forward jog re-lock")
    scan_session.pause_capture(delay_sec=1.0)
    return {"success": True, **scan_session.status()}


@router.post("/stop")
def stop_belt():
    """Manual STOP. Counted separately from FM stops in looker_data."""
    scan_session.stop_belt_manually()
    return {"success": True, **scan_session.status()}


@router.post("/cancel")
def cancel_scan():
    """Discard the run without submitting. Port of cancel_result (main.py:2033-2052).

    Legacy moves the saved crops into a rejected/ folder rather than deleting
    them — scan_session.cancel() does the same before resetting.
    """
    conveyor_service.send("all_stop")
    conveyor_service.unlock_machine_start(reason="scan cancelled")
    result = scan_session.cancel()
    return {"success": True, **result}


@router.post("/submit")
def submit_scan(
    req: SubmitRequest,
    db: Session = Depends(get_db),
):
    """Finish the run and build the result. Port of submit_video ->
    submit_create_result (main.py:1566-1661) — stops after writing result.json
    and computing the datagram. NOT persisted to the database and NOT synced
    to Qualix/Sheets yet; that's /confirm's job, matching legacy's second,
    separate confirmation click.
    """
    global _pending_submission

    if not scan_session.active:
        raise HTTPException(status_code=409, detail="No active scan to submit")

    # Legacy refused to proceed on non-numeric FO counts (main.py:1573-1578)
    # rather than silently coercing them to 0.
    for label, value in (("Blower FO", req.blower_fo), ("Magnetic FO", req.magnetic_fo)):
        if value is None or str(value).strip() == "" or not str(value).strip().isnumeric():
            raise HTTPException(status_code=422, detail=f"Enter a valid {label} count")

    blower = int(str(req.blower_fo).strip())
    magnetic = int(str(req.magnetic_fo).strip())

    # Legacy's submit_video calls stop_p() (main.py:1571), the same function the
    # manual Stop button uses — Submit sends all_stop AND counts as a stop for
    # conveyor_stop_count. update_fm_count's "-1" specifically discounts this
    # implicit stop, not the initial start (main.py:1554 vs 741-850, which never
    # touches stop_count at all). Calling send("all_stop") directly here, without
    # going through the same counting path, would leave "Manual Stop Count"
    # wrong by exactly one whenever the belt failed to ack at scan start.
    scan_session.stop_belt_manually()

    status_before = scan_session.status()
    result_payload = scan_session.finish(blower, magnetic)

    batch = None
    batch_id = (scan_session.batch or {}).get("id")
    if batch_id:
        batch = db.query(BatchDetails).filter(BatchDetails.id == batch_id).first()
    if batch is None:
        batch = (
            db.query(BatchDetails)
            .filter(BatchDetails.batch_number == result_payload["sample_id"])
            .order_by(BatchDetails.id.desc())
            .first()
        )

    datagram = build_datagram(
        db,
        session_status=status_before,
        result_payload=result_payload,
        batch=batch,
        surveyor_name=req.surveyor_name,
    )

    with _pending_lock:
        _pending_submission = {
            "result_payload": result_payload,
            "datagram": datagram,
            "commodity": status_before.get("commodity", ""),
            "variety": status_before.get("variety", ""),
            # Kept only so a reclassify (see /pending-crops/relabel below) can
            # rebuild the datagram exactly the way this endpoint just did,
            # without the frontend having to resend anything.
            "batch": batch,
            "status_before": status_before,
            "surveyor_name": req.surveyor_name,
        }

    return {
        "status": "success",
        "result": result_payload,
        "datagram": datagram,
    }


@router.get("/pending-crops")
def list_pending_crops():
    """Crops saved for the batch currently awaiting /confirm or /discard —
    lets the review screen show what was captured before the operator saves.

    Gated on _pending_submission rather than just scan_session.active (which
    is already False by this point, see /submit) so this only ever applies
    to the specific window between Submit and Confirm/Discard, not to some
    unrelated leftover folder.
    """
    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(status_code=409, detail="Nothing pending to show crops for")
        return {"crops": scan_session.list_pending_crops()}


@router.post("/pending-crops/relabel")
def relabel_pending_crop(req: RelabelCropRequest, db: Session = Depends(get_db)):
    """Reclassify one crop before the batch is confirmed — new to this port,
    not a legacy feature (see scan_session.relabel_crop's own docstring and
    enhancements.md). Renames the file, then recounts and rebuilds the same
    result/datagram shape /submit returned, so the frontend can just replace
    its local copy of both with this response.

    The rename, the recount and the _pending_submission update all happen
    under _pending_lock as ONE atomic step — see that lock's own comment for
    the race this closes. list_pending_crops() is inside it too, so the crop
    list returned to the screen can't describe a folder state older than the
    counts returned alongside it.
    """
    global _pending_submission

    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(status_code=409, detail="Nothing pending to reclassify")

        try:
            scan_session.relabel_crop(req.name, req.fm_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        pending = _pending_submission
        prev_result = pending["result_payload"]["result"]
        blower = prev_result.get("Blower FO", 0)
        magnetic = prev_result.get("Magnetic FO", 0)

        new_data = scan_session.create_results(blower, magnetic)
        total = sum(v for v in new_data.values() if isinstance(v, int))

        result_payload = dict(pending["result_payload"])
        result_payload["result"] = new_data
        result_payload["total_fo_detected"] = total

        try:
            os.makedirs(scan_session.output_folder, exist_ok=True)
            with open(os.path.join(scan_session.output_folder, "result.json"), "w") as fh:
                json.dump(result_payload, fh, indent=4)
        except Exception as exc:
            logger.error("Could not rewrite result.json after reclassify: %s", exc)

        datagram = build_datagram(
            db,
            session_status=pending["status_before"],
            result_payload=result_payload,
            batch=pending["batch"],
            surveyor_name=pending["surveyor_name"],
        )

        _pending_submission = {**pending, "result_payload": result_payload, "datagram": datagram}

        return {
            "status": "success",
            "result": result_payload,
            "datagram": datagram,
            "crops": scan_session.list_pending_crops(),
        }


@router.post("/confirm")
def confirm_scan(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Persist the last /submit's result and queue delivery to Qualix/Sheets.

    Port of backtohome -> save_result (main.py:1717-1812): legacy starts
    post_api_th (the actual api_handler.post_analysis_data + app_db.
    write_results call) only when the operator clicks through from the
    results-review page — not from Submit itself. If that second click never
    happens, legacy never saves or syncs the batch either; reproduced here on
    purpose rather than fixed.
    """
    global _pending_submission

    # Under the lock so a reclassify still in flight can't be persisted
    # half-applied: the Save click follows the last relabel by milliseconds,
    # and this is the read whose result actually reaches the database and
    # Qualix. Taking it here makes Save wait for that relabel to land.
    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(
                status_code=409,
                detail="Nothing to confirm — submit a result first.",
            )
        pending = _pending_submission
        _pending_submission = None

    result_payload = pending["result_payload"]
    datagram = pending["datagram"]

    service = DatabaseService(db)
    saved = service.save_scan_result(
        sample_id=result_payload["sample_id"],
        commodity=pending["commodity"],
        variety=pending["variety"],
        datagram=datagram,
        date=result_payload["date"],
        start_time=result_payload["start_time"],
        stop_time=result_payload["end_time"],
    )

    background_tasks.add_task(sync_result_to_cloud, saved.id, datagram)

    return {"success": True, "result_id": saved.id}


@router.post("/discard")
def discard_pending():
    """Discard a /submit result before it's confirmed. Port of cancel_result
    (main.py:2064-2081), bound to pushButton_cancel_res on this exact screen
    (main.py:496) — legacy's results-review page has both Save (save_result,
    confirm_scan's port) and Cancel here, and this port had only the former.
    Archives the batch's crops to rejected/ rather than deleting them, same
    as scan_session.cancel() already does for Cancel Batch elsewhere.
    """
    global _pending_submission

    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(
                status_code=409,
                detail="Nothing to discard — submit a result first.",
            )
        _pending_submission = None

    result = scan_session.cancel()
    return {"success": True, **result}
