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
import uuid
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.security import current_operator_id
from app.models.schema import BatchDetails, Result
from app.services.conveyor_service import conveyor_service
from app.services.database_service import DatabaseService
from app.services.datagram import build_datagram
from app.services.scan_session import archive_crops, scan_session
from app.services.sync_lock import claim_result
from app.services.sync_service import sync_service
from app.services import scan_progress

logger = logging.getLogger(__name__)
router = APIRouter()


def _checkpoint(awaiting_save: bool = False) -> None:
    """Record the current run in scan_progress, so it survives a power cut and
    can be held. Called after each operator action that changes the run —
    never from the per-frame loop. Best-effort: see scan_progress."""
    if scan_session.folder_name:
        scan_progress.checkpoint(scan_session.progress_state(), awaiting_save=awaiting_save)

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


class ConfirmRequest(BaseModel):
    """Body of /confirm.

    `client_request_id` is a UUID the browser mints once, before its first
    attempt, and reuses on every retry of that same save. This device runs on
    a warehouse link that drops: a POST can succeed server-side and still time
    out before the response gets home, and the operator then presses Save
    again. Without the key the second press was answered "Nothing to confirm —
    submit a result first" (the pending slot having already been consumed),
    which reads as a failure for a scan that was in fact saved — the operator's
    reasonable next move being to re-run the whole batch. With it, the retry
    returns the id from the first attempt.

    Optional so an older frontend, or curl, still works unchanged — it simply
    gets no replay protection.
    """

    client_request_id: str = ""


class RelabelCropRequest(BaseModel):
    name: str
    fm_name: str


# The batch fields the operator may correct on the results-review screen,
# before Save. Deliberately excludes batch_number (the record's identity),
# site_code, product_name and product_code: the last three come from Qualix and
# are shown for context only. vendor_code is derived from the chosen vendor
# (populate_vendor_code), so it is set from vendor_name, never typed — but it is
# accepted here because the frontend sends the pair together, exactly as the New
# Batch form does.
_EDITABLE_BATCH_FIELDS = (
    "vendor_name",
    "vendor_code",
    "manufacturing_date",
    "receiving_date",
    "brand",
    "po_number",
    "sorting_quantity",
    "sorter_name",
)

_READONLY_BATCH_FIELDS = (
    "batch_number",
    "site_code",
    "product_name",
    "product_code",
)


class PendingBatchEditRequest(BaseModel):
    vendor_name: Optional[str] = None
    vendor_code: Optional[str] = None
    manufacturing_date: Optional[str] = None
    receiving_date: Optional[str] = None
    brand: Optional[str] = None
    po_number: Optional[str] = None
    sorting_quantity: Optional[str] = None
    sorter_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Background sync
# ---------------------------------------------------------------------------

def sync_result_to_cloud(result_id: int, datagram: dict):
    """Deliver one result to Qualix and Google Sheets.

    Opens its OWN database session: the request-scoped session from
    Depends(get_db) is already closed by the time a BackgroundTask runs, so
    every write through it was silently failing.

    Claims the result first so the retry worker cannot start posting the same
    one while this is still in flight — which it otherwise can, because the
    record stays '0' for the whole duration of this call. See sync_lock.
    """
    with claim_result(result_id) as granted:
        if not granted:
            return
        _deliver(result_id, datagram)


def _deliver(result_id: int, datagram: dict):
    """The delivery itself. Only ever called holding this result's claim."""
    db = SessionLocal()
    try:
        service = DatabaseService(db)

        post_status, error_code, error_detail = (
            "0", "not_attempted", "Not signed in to Qualix when the scan was saved.",
        )
        if sync_service.is_authenticated:
            post_status, error_code, error_detail = sync_service.post_analysis_data(datagram)
        else:
            logger.warning(
                "No Qualix session — result %s stays unsynced and will be retried.",
                result_id,
            )

        # Sheets is a side channel. Legacy advanced sync_status on the Qualix
        # response alone (main.py:2930-2941); a Sheets success must not mask a
        # Qualix failure, or the retry worker never sees the record again.
        #
        # Only ever written when Qualix actually accepted the record, matching
        # what the retry worker and History's Re-sync already do. Posting it
        # unconditionally was wrong twice over: a payload Qualix *rejected*
        # (400 — e.g. "Device does not exist") still reached the sheet as
        # though it had been accepted, and a record that merely failed to
        # deliver ('0') got a row here and then a SECOND one from the retry
        # worker once it eventually went through.
        if post_status == "1":
            try:
                sync_service.post_to_sheets(datagram)
            except Exception as exc:
                logger.error("Sheets sync failed for result %s: %s", result_id, exc)

        service.set_sync_status(result_id, post_status, error_detail)
        if post_status == "1":
            logger.info("Result %s synced to Qualix.", result_id)
        elif post_status == "2":
            logger.error(
                "Result %s rejected by Qualix (400) — marked '2', will not be retried. %s",
                result_id, error_detail,
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
        # A run still loaded here was left without Save, Cancel or Hold (the
        # scan page was reloaded, or the operator went to another batch). It
        # is kept as interrupted so it can be continued, rather than lost.
        if scan_session.folder_name:
            scan_progress.interrupt(scan_session.folder_name)
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

    _checkpoint()

    conveyor_service.unlock_machine_start(reason="new scan")
    conveyor_service.send("camera_on")
    started = conveyor_service.send("machine_start")

    # A silent "success" here is worse than no response at all: the operator
    # sees a normal-looking live-scan screen with a belt that never actually
    # moved, and has no way to tell short of watching it. conveyor_service
    # already sent a fail-safe all_stop internally after exhausting its
    # retries — this just makes sure that failure actually reaches the UI.
    # Safe to raise after scan_session.start() already ran: it's idempotent
    # (see its own docstring), so pressing Start again once the belt is fixed
    # resumes the same session rather than needing any cleanup here.
    if not started:
        raise HTTPException(
            status_code=502,
            detail=(
                "The belt did not respond to the start command. Check that the "
                "conveyor controller is powered on and the serial cable is "
                "connected, then try Start again."
            ),
        )

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
        result = scan_session.label_detection(req.index, req.fm_name)
        _checkpoint()
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/resume")
def resume_scan():
    """Release the interlock and restart the belt after a detection is resolved."""
    if not scan_session.active:
        raise HTTPException(status_code=409, detail="No active scan")
    result = scan_session.resume()
    _checkpoint()
    return result


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
    started = conveyor_service.send("machine_start")
    time.sleep(0.1)
    detected = conveyor_service.send("FM_detected")
    # Re-lock and re-pause regardless of the outcome above — this is the
    # existing safe end-state either way (conveyor_service's own fail-safe
    # all_stop already fired internally if either send() failed), it just
    # must not be reported as a success the operator has no reason to
    # question. Without this, the previous frozen frame's FM boxes were left
    # on screen with no indication anything had gone wrong.
    conveyor_service.lock_machine_start(reason="forward jog re-lock")
    scan_session.pause_capture(delay_sec=1.0)
    _checkpoint()
    if not (started and detected):
        raise HTTPException(
            status_code=502,
            detail=(
                "The belt did not respond. Check that the conveyor controller "
                "is powered on and the serial cable is connected, then try "
                "Forward again."
            ),
        )
    return {"success": True, **scan_session.status()}


@router.post("/stop")
def stop_belt():
    """Manual STOP. Counted separately from FM stops in looker_data."""
    scan_session.stop_belt_manually()
    _checkpoint()
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
    scan_progress.mark_discarded(result.get("folder_name"))
    return {"success": True, **result}


@router.post("/submit")
def submit_scan(
    req: SubmitRequest,
    request: Request,
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
    _checkpoint(awaiting_save=True)

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

    operator_id = current_operator_id(request)
    datagram = build_datagram(
        db,
        session_status=status_before,
        result_payload=result_payload,
        batch=batch,
        surveyor_name=req.surveyor_name,
        operator_id=operator_id,
    )

    # The idempotency key for the save that will follow, minted here rather
    # than by the browser on the results page. It is handed back in the
    # response and the frontend holds onto it until the batch is saved or
    # discarded.
    #
    # Minting it at this point is what makes the protection survive the
    # operator leaving the results page and coming back: a key created on that
    # page is lost the moment it unmounts, so the retry that follows looks
    # like a different save and gets a 409 for a batch that was in fact
    # stored. Tied to the submission instead, it lasts as long as the
    # submission does.
    request_id = str(uuid.uuid4())

    with _pending_lock:
        _pending_submission = {
            "result_payload": result_payload,
            "datagram": datagram,
            "client_request_id": request_id,
            "commodity": status_before.get("commodity", ""),
            "variety": status_before.get("variety", ""),
            # Kept only so a reclassify (see /pending-crops/relabel below) can
            # rebuild the datagram exactly the way this endpoint just did,
            # without the frontend having to resend anything.
            "batch": batch,
            "status_before": status_before,
            "surveyor_name": req.surveyor_name,
            "operator_id": operator_id,
        }

    return {
        "status": "success",
        "result": result_payload,
        "datagram": datagram,
        # Held by the frontend and sent back on /confirm — see the comment
        # where it is minted above.
        "client_request_id": request_id,
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
            operator_id=pending.get("operator_id", ""),
        )

        _pending_submission = {**pending, "result_payload": result_payload, "datagram": datagram}

        return {
            "status": "success",
            "result": result_payload,
            "datagram": datagram,
            "crops": scan_session.list_pending_crops(),
        }


def _batch_details_response(batch) -> dict:
    """Editable + read-only batch fields, split the way the review screen shows
    them. Read straight off the BatchDetails row, so dates come back in their
    stored form (what a date input needs) rather than the datagram's already-
    formatted display form."""
    return {
        "editable": {f: (getattr(batch, f, "") or "") for f in _EDITABLE_BATCH_FIELDS},
        "readonly": {f: (getattr(batch, f, "") or "") for f in _READONLY_BATCH_FIELDS},
    }


@router.get("/pending/batch-details")
def get_pending_batch_details():
    """The batch details behind the pending submission, for the edit form.

    Only valid between /submit and /confirm-or-discard — the same window as the
    reclassify feature. Reads from _pending_submission's own BatchDetails row so
    the form is seeded with exactly what will be saved, no round trip through
    the datagram (whose field names and date formatting differ).
    """
    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(
                status_code=409,
                detail="Nothing to edit — submit a result first.",
            )
        batch = _pending_submission.get("batch")
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail="This submission has no batch details to edit.",
            )
        return {"status": "success", **_batch_details_response(batch)}


@router.post("/pending/batch-details")
def update_pending_batch_details(
    req: PendingBatchEditRequest, db: Session = Depends(get_db)
):
    """Correct the batch details on the results-review screen, before Save.

    Writes the edited fields back to the BatchDetails row AND rebuilds the
    datagram in _pending_submission from the updated row, so the correction
    reaches Qualix on /confirm without the frontend resending anything — the
    same rebuild-in-place pattern as /pending-crops/relabel. batch_number,
    site_code, product_name and product_code are never touched here (see
    _EDITABLE_BATCH_FIELDS); a value sent for one is ignored.

    The whole read-modify-write runs under _pending_lock so a Save landing at
    the same moment cannot read a half-updated datagram.
    """
    global _pending_submission

    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(
                status_code=409,
                detail="Nothing to edit — submit a result first.",
            )
        pending = _pending_submission
        batch = pending.get("batch")
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail="This submission has no batch details to edit.",
            )

        row = db.query(BatchDetails).filter(BatchDetails.id == batch.id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Batch not found")

        for field in _EDITABLE_BATCH_FIELDS:
            value = getattr(req, field)
            # None means "not sent" — leave the stored value alone. An empty
            # string is a deliberate clear and is honoured.
            if value is not None:
                setattr(row, field, value.strip() if isinstance(value, str) else value)

        try:
            db.commit()
            db.refresh(row)
        except Exception as exc:
            db.rollback()
            logger.error("Failed to update batch details: %s", exc)
            raise HTTPException(
                status_code=500, detail=f"Failed to update batch details: {exc}"
            )

        datagram = build_datagram(
            db,
            session_status=pending["status_before"],
            result_payload=pending["result_payload"],
            batch=row,
            surveyor_name=pending["surveyor_name"],
            operator_id=pending.get("operator_id", ""),
        )

        _pending_submission = {**pending, "batch": row, "datagram": datagram}

        return {
            "status": "success",
            "datagram": datagram,
            **_batch_details_response(row),
        }


@router.post("/confirm")
def confirm_scan(
    background_tasks: BackgroundTasks,
    req: ConfirmRequest = ConfirmRequest(),
    db: Session = Depends(get_db),
):
    """Persist the last /submit's result and queue delivery to Qualix/Sheets.

    Port of backtohome -> save_result (main.py:1717-1812): legacy starts
    post_api_th (the actual api_handler.post_analysis_data + app_db.
    write_results call) only when the operator clicks through from the
    results-review page — not from Submit itself. If that second click never
    happens, legacy never saves or syncs the batch either; reproduced here on
    purpose rather than fixed.

    Idempotent on req.client_request_id: a retry of a save that already landed
    returns the original result_id rather than storing the scan again. See
    ConfirmRequest for why that matters on this hardware.
    """
    global _pending_submission

    request_id = (req.client_request_id or "").strip()

    # The replay check, before the pending slot is touched. A retry that got
    # here after the original committed is answered from what the original
    # stored — and deliberately does NOT re-queue the sync: the first attempt
    # already queued it, and the retry worker picks up anything still pending,
    # so posting again would risk a duplicate reaching Qualix.
    if request_id:
        already = db.query(Result).filter(Result.client_request_id == request_id).first()
        if already:
            logger.info(
                "Replay of /confirm for request %s — returning the existing result %d "
                "instead of saving the scan again.",
                request_id,
                already.id,
            )
            return {"success": True, "result_id": already.id, "duplicate": True}

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

    # A client that sent no key of its own still gets the one /submit minted
    # for this submission, so the stored row always carries a key and a later
    # retry that does send it can be matched.
    request_id = request_id or pending.get("client_request_id", "")

    result_payload = pending["result_payload"]
    datagram = pending["datagram"]

    service = DatabaseService(db)
    try:
        saved = service.save_scan_result(
            sample_id=result_payload["sample_id"],
            commodity=pending["commodity"],
            variety=pending["variety"],
            datagram=datagram,
            date=result_payload["date"],
            start_time=result_payload["start_time"],
            stop_time=result_payload["end_time"],
            client_request_id=request_id,
        )
    except IntegrityError:
        # Two retries raced past the SELECT above and both tried to insert the
        # same key; the unique index let exactly one through. Answer with the
        # one that won rather than failing a save that did happen.
        db.rollback()
        already = db.query(Result).filter(Result.client_request_id == request_id).first()
        if already is None:
            raise
        logger.info(
            "Concurrent /confirm for request %s — returning result %d.",
            request_id,
            already.id,
        )
        return {"success": True, "result_id": already.id, "duplicate": True}

    background_tasks.add_task(sync_result_to_cloud, saved.id, datagram)
    scan_progress.mark_saved((pending.get("status_before") or {}).get("image_unique_id"))

    return {"success": True, "result_id": saved.id, "duplicate": False}


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
    scan_progress.mark_discarded(result.get("folder_name"))
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# Held batches — see app/services/scan_progress.py
# ---------------------------------------------------------------------------

@router.post("/hold")
def hold_pending():
    """Put the batch on the results page on hold instead of Save or Cancel.

    Nothing is saved to History or sent to Qualix, and nothing is moved: the
    batch's crops and frames stay in its folders, and its scan_progress row is
    marked held. The scan session is then cleared so the next batch can start.
    The batch is continued later from Held Batches on Home.

    Either the hold is recorded or nothing changes: if the database cannot
    record it, the results page keeps its batch, so the operator can still Save
    or Cancel instead of losing it.
    """
    global _pending_submission

    with _pending_lock:
        if _pending_submission is None:
            raise HTTPException(
                status_code=409,
                detail="Nothing to hold — submit a result first.",
            )
        pending = _pending_submission
        folder = (pending.get("status_before") or {}).get("image_unique_id")
        if not folder:
            raise HTTPException(status_code=409, detail="This batch has no folder to hold.")

        try:
            held = scan_progress.hold(folder)
            if not held and scan_session.folder_name == folder:
                # No row yet (an earlier checkpoint failed): write it now.
                scan_progress.checkpoint(scan_session.progress_state(), awaiting_save=True)
                held = scan_progress.hold(folder)
        except Exception as exc:
            logger.error("Could not hold batch %s: %s", folder, exc)
            held = False
        if not held:
            raise HTTPException(
                status_code=500,
                detail="Could not put this batch on hold. Save or Cancel it instead.",
            )
        _pending_submission = None

    with scan_session._lock:
        if scan_session.folder_name == folder:
            scan_session.reset()
    conveyor_service.unlock_machine_start(reason="batch held")
    return {"success": True, "folder_name": folder}


@router.get("/held")
def list_held():
    """Held and interrupted batches, for the Held Batches list on Home."""
    return {"batches": scan_progress.list_open()}


@router.post("/held/{progress_id}/resume")
def resume_held(progress_id: int):
    """Load a held or interrupted batch back into the scan session.

    The frontend then opens the live scan screen for it: Start scans more
    material into the same batch, Submit goes straight to the results page.
    Refused while another batch is being scanned.
    """
    global _pending_submission

    if scan_session.active:
        raise HTTPException(
            status_code=409,
            detail="Another batch scan is in progress. Submit or cancel it first.",
        )
    state = scan_progress.get_open(progress_id)
    if state is None:
        raise HTTPException(status_code=404, detail="This held batch no longer exists.")
    if not state.get("output_folder") or not os.path.isdir(state["output_folder"]):
        raise HTTPException(
            status_code=409,
            detail="This batch's images are no longer on the device, so it cannot be "
                   "continued. Discard it instead.",
        )

    # A results page left open for another batch (submitted, never saved,
    # cancelled or held) would otherwise sit in front of this one. Keep that
    # batch as interrupted instead of losing it.
    with _pending_lock:
        if _pending_submission is not None:
            stale = (_pending_submission.get("status_before") or {}).get("image_unique_id")
            _pending_submission = None
            scan_progress.interrupt(stale)

    with scan_session._lock:
        if scan_session.folder_name and scan_session.folder_name != state["folder_name"]:
            scan_progress.interrupt(scan_session.folder_name)
        status = scan_session.restore(state)
    scan_progress.mark_resumed(progress_id)
    _checkpoint()
    conveyor_service.unlock_machine_start(reason="held batch continued")
    return {
        "success": True,
        "batch_id": state.get("batch_id"),
        "prior_fo_count": scan_session.prior_fo_count,
        **status,
    }


@router.post("/held/{progress_id}/discard")
def discard_held(progress_id: int):
    """Discard a held or interrupted batch, the same way Cancel discards a live
    one: its crops go to rejected/ (or are deleted with REJECTED_SAVE_ENABLED=
    false), and it leaves the Held Batches list."""
    state = scan_progress.get_open(progress_id)
    if state is None:
        raise HTTPException(status_code=404, detail="This held batch no longer exists.")
    if scan_session.folder_name == state["folder_name"]:
        raise HTTPException(status_code=409, detail="This batch is open on the scan screen.")
    archive_crops(state.get("output_folder"), state.get("commodity") or "",
                  state.get("variety") or "", state["folder_name"])
    scan_progress.mark_discarded(state["folder_name"])
    return {"success": True, "sample_id": state.get("sample_id")}
