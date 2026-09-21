"""
Batch metadata.

The 12 fields the operator enters before a scan. Legacy read these straight off
the Qt widgets when building the Qualix datagram (main.py:1663-1710); here they
are persisted so /api/scan/submit can rebuild the datagram server-side and the
browser never has to hold them.

The returned `id` is what the frontend passes to /api/scan/start as `batch_id`.
Previously it was discarded, which left every batch row orphaned and dropped
all 12 fields from the Qualix payload.
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.schema import BatchDetails

logger = logging.getLogger(__name__)
router = APIRouter()


class BatchCreate(BaseModel):
    # Not typed by the operator — see _generate_batch_number. The form shows
    # the id as soon as it opens, which means it has to be handed out before
    # the batch is saved (GET /batch/next-number) and sent back here, so the
    # number on screen is the number stored. It is still only a request: it's
    # honoured when it's well-formed and free, and silently replaced with a
    # freshly generated one otherwise, so a stale or tampered value can never
    # produce a duplicate.
    batch_number: Optional[str] = None
    po_number: Optional[str] = None
    manufacturing_date: Optional[str] = None
    vendor_name: Optional[str] = None
    receiving_date: Optional[str] = None
    sorting_quantity: Optional[str] = None
    product_name: Optional[str] = None
    brand: Optional[str] = None
    vendor_code: Optional[str] = None
    site_code: Optional[str] = None
    product_code: Optional[str] = None
    sorter_name: Optional[str] = None

    @field_validator("sorting_quantity")
    @classmethod
    def sorting_quantity_must_be_numeric(cls, value: Optional[str]) -> Optional[str]:
        # NewBatch.jsx already strips anything but digits/a single "." as the
        # operator types (it's a weight, e.g. "40.5") — this is the same
        # check on the server side, since the frontend check alone can't be
        # trusted for a request that didn't go through that form at all.
        cleaned = (value or "").strip()
        # \d+(\.\d+)? rather than a bare float() call, which would also accept
        # forms the frontend's char-level filter can't even produce (e.g.
        # "1e10", "inf", "nan").
        if cleaned and not re.fullmatch(r"\d+(\.\d+)?", cleaned):
            raise ValueError("Sorting quantity must be a number")
        return cleaned


# Epoch seconds is 10 digits from 2001 until 2286. Zero-padding to a fixed
# width keeps every batch number the same length and makes them sort
# lexicographically in true chronological order — which is what lets
# _last_issued_ts below use a plain MAX() to find the newest one.
_TS_WIDTH = 10
_BATCH_NUMBER_LEN = 2 + _TS_WIDTH


def _device_code() -> str:
    """The two-character device namespace, validated at the point of use.

    Checked here rather than at import so a misconfigured device fails with a
    clear error on the request that actually needs it, instead of refusing to
    boot entirely — the rest of the app (viewing past results, retrying syncs)
    still works without it.
    """
    code = settings.DEVICE_CODE
    if not re.fullmatch(r"[A-Z0-9]{2}", code):
        raise HTTPException(
            status_code=500,
            detail=(
                "DEVICE_CODE is not configured. Set it to a unique "
                "2-character A-Z/0-9 code for this device before creating batches."
            ),
        )
    return code


def _last_issued_ts(db: Session, device_code: str) -> Optional[int]:
    """The timestamp in the newest batch number this device has issued.

    The LIKE pattern is anchored to the exact id length (one "_" per timestamp
    character) so it can't match a batch number in the old
    COMMODITY-VARIETY-DATE-SEQ format, which is always longer and could
    otherwise share the same two leading characters as the device code.
    """
    pattern = device_code + "_" * _TS_WIDTH
    newest = (
        db.query(func.max(BatchDetails.batch_number))
        .filter(BatchDetails.batch_number.like(pattern))
        .scalar()
    )
    if not newest:
        return None
    try:
        return int(newest[2:])
    except ValueError:
        # Something matched the shape but isn't numeric — ignore it rather than
        # blocking batch creation; the unique constraint is still the backstop.
        logger.warning("Ignoring unparseable batch number when picking next id: %s", newest)
        return None


def _generate_batch_number(db: Session, device_code: str) -> str:
    """Auto-generated, unique batch number: device code + epoch seconds.

    Replaces what used to be a manually typed field with no uniqueness check
    at all (see git history/enhancements.md) — an operator could, and did,
    type the same batch number twice. It then became the row's own primary
    key, which is unique per device but restarts at 1 on every device, so two
    machines reliably produced the same id and collided once their scans met
    in Qualix. The device code is what namespaces them apart.

    The timestamp alone is NOT a safe uniqueness guarantee here: these Jetsons
    have no battery-backed RTC, so the clock can come up in the past after a
    reboot and jump forward again once NTP syncs, re-issuing seconds it has
    already used. Two batches saved within the same second would collide too,
    which at one-second resolution is a realistic double-submit, not a
    theoretical one. So the value is clamped to stay strictly above the newest
    one this device has already issued — the id stays a real timestamp in
    normal operation, and degrades to a monotonic counter if the clock
    misbehaves rather than repeating itself.
    """
    now_ts = int(time.time())
    last_ts = _last_issued_ts(db, device_code)
    if last_ts is not None and now_ts <= last_ts:
        logger.warning(
            "Clock is not ahead of the last issued batch number (now=%d, last=%d) — "
            "using last+1. Check this device's time sync.",
            now_ts,
            last_ts,
        )
        now_ts = last_ts + 1
    return f"{device_code}{now_ts:0{_TS_WIDTH}d}"


@router.get("/next-number")
def next_batch_number(db: Session = Depends(get_db)):
    """The batch number the new-batch form shows before anything is saved.

    Nothing is reserved here — this only reads. The form sends the value back
    with the batch it creates, and /new decides whether it can still be used.
    """
    return {"status": "success", "batch_number": _generate_batch_number(db, _device_code())}


@router.post("/new")
def create_new_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    device_code = _device_code()
    fields = batch.model_dump()
    # What the form displayed, if it got that far. Anything not matching this
    # device's own id format is discarded rather than trusted — the shape is
    # what /next-number handed out, so a value in any other form did not come
    # from there.
    requested = (fields.pop("batch_number", None) or "").strip().upper()
    if requested and not re.fullmatch(rf"{device_code}\d{{{_TS_WIDTH}}}", requested):
        logger.warning("Ignoring malformed batch number from client: %s", requested)
        requested = ""

    # _generate_batch_number reads the newest existing id and steps past it,
    # which two concurrent requests can both do before either commits. The
    # unique constraint turns that race into an IntegrityError instead of a
    # duplicate; retrying re-reads the now-committed newest id and drops the
    # requested number, so a second attempt never reuses what just collided. A
    # handful of attempts is far more than a single-operator device can need.
    for attempt in range(5):
        try:
            row = BatchDetails(
                **fields,
                batch_number=requested or _generate_batch_number(db, device_code),
                created_at=datetime.now().isoformat(),
            )
            db.add(row)
            db.commit()
            db.refresh(row)  # row.id only exists after this.

            return {
                "status": "success",
                "message": "Batch details saved",
                "id": row.id,
                "batch_number": row.batch_number,
            }
        except IntegrityError:
            db.rollback()
            logger.warning("Batch number collided on attempt %d — regenerating.", attempt + 1)
            requested = ""
        except Exception as exc:
            db.rollback()
            logger.error("Failed to save batch details: %s", exc)
            raise HTTPException(status_code=500, detail=f"Failed to save batch details: {exc}")

    raise HTTPException(status_code=500, detail="Could not allocate a unique batch number")


@router.get("/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    row = db.query(BatchDetails).filter(BatchDetails.id == batch_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {
        "status": "success",
        "batch": {
            c.name: getattr(row, c.name) for c in BatchDetails.__table__.columns
        },
    }
