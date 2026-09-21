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
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schema import BatchDetails

logger = logging.getLogger(__name__)
router = APIRouter()


class BatchCreate(BaseModel):
    # No longer taken from the operator — see _generate_batch_number. Nothing
    # in this request needs to supply one.
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


def _slug(value: Optional[str], max_len: int = 12) -> str:
    """Commodity/variety, as they appear in a batch number.

    Strips everything but letters/digits and uppercases what's left, so a
    commodity like "Basmati Rice" becomes "BASMATIRICE" rather than embedding
    spaces or punctuation into an id that gets used as a filename prefix and
    an external reference elsewhere. Capped at max_len so one long commodity
    name doesn't dominate the whole id — this is meant to be recognizable at
    a glance, not a full transcription.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return cleaned[:max_len] or "NA"


def _generate_batch_number(commodity: Optional[str], variety: Optional[str], row_id: int) -> str:
    """Auto-generated, guaranteed-unique batch number.

    Replaces what used to be a manually typed field with no uniqueness check
    at all (see git history/enhancements.md) — an operator could, and did,
    type the same batch number twice. Built from:
      - the commodity and variety, so it's recognizable at a glance rather
        than an opaque number;
      - today's date, so it's obvious which day a batch belongs to without
        looking anything up;
      - the row's own database id, zero-padded — this is what actually makes
        it unique. It's a primary key, so it can never collide, and reusing
        it means there's no separate counter to build or get out of sync.
    """
    date_part = datetime.now().strftime("%Y%m%d")
    return f"{_slug(commodity)}-{_slug(variety)}-{date_part}-{row_id:06d}"


@router.post("/new")
def create_new_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    try:
        row = BatchDetails(
            **batch.model_dump(),
            created_at=datetime.now().isoformat(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)  # row.id only exists after this — needed below.

        row.batch_number = _generate_batch_number(row.product_name, row.product_code, row.id)
        db.commit()

        return {
            "status": "success",
            "message": "Batch details saved",
            "id": row.id,
            "batch_number": row.batch_number,
        }
    except Exception as exc:
        db.rollback()
        logger.error("Failed to save batch details: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to save batch details: {exc}")


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
