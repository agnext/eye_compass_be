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
    batch_number: str
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

    @field_validator("batch_number")
    @classmethod
    def batch_number_must_be_alnum(cls, value: str) -> str:
        # Legacy required a non-empty alphanumeric sample id before allowing
        # the operator past the batch form (on_next_click, main.py:658-671).
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Batch number is required")
        if not cleaned.isalnum():
            raise ValueError("Batch number must be alphanumeric (no spaces or symbols)")
        return cleaned


@router.post("/new")
def create_new_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    try:
        row = BatchDetails(
            **batch.model_dump(),
            created_at=datetime.now().isoformat(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
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
