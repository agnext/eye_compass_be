"""
Scan history.

Legacy showed Batch Id, Commodity, Receiving Date, Vendor Name, Sorted Quantity
and Total FO (main.py:1745-1758, 2056-2087), paged 20 at a time, and offered a
per-FO drill-down plus the saved crop gallery (main.py:2110-2277).

The columns come out of the stored datagram, so they are available without any
extra tables.
"""

import base64
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.models.schema import Result
from app.services.database_service import DatabaseService
from app.services.sync_service import sync_service

logger = logging.getLogger(__name__)
router = APIRouter()

SYNC_LABELS = {"1": "Synced", "2": "Rejected", "0": "Pending"}


def _row_to_summary(r: Result) -> dict:
    payload = r.result if isinstance(r.result, dict) else {}
    scan_data = payload.get("scan_data", {}) or {}
    analysis = payload.get("analysis", []) or []

    total_fo = next(
        (a.get("totalAmount") for a in analysis if a.get("analysisName") == "total_fo_detected"),
        None,
    )

    return {
        "id": r.id,
        "sample_id": r.sample_id,
        "commodity": r.commodity,
        "variety": r.variety,
        "date": r.date,
        "start_time": r.start_time,
        "stop_time": r.stop_time,
        "sync_status": r.sync_status,
        "sync_label": SYNC_LABELS.get(r.sync_status, "Unknown"),
        # Legacy history columns
        "receiving_date": scan_data.get("receiving_date", ""),
        "vendor_name": scan_data.get("vendor_name", ""),
        "sorted_quantity": scan_data.get("weight", ""),
        "total_fo_detected": total_fo,
        "image_unique_id": scan_data.get("image_unique_id", ""),
    }


@router.get("/")
def get_history(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Paged history, newest first."""
    try:
        query = db.query(Result).order_by(Result.id.desc())
        total = query.count()
        rows = query.offset(offset).limit(limit).all()
        return {
            "status": "success",
            "total": total,
            "limit": limit,
            "offset": offset,
            "data": [_row_to_summary(r) for r in rows],
        }
    except Exception as exc:
        logger.error("Failed to fetch history: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{result_id}")
def get_result_detail(result_id: int, db: Session = Depends(get_db)):
    """Full drill-down: the per-FO Item/Count breakdown legacy showed in
    populate_result_table (main.py:2110-2128)."""
    r = db.query(Result).filter(Result.id == result_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Result not found")

    payload = r.result if isinstance(r.result, dict) else {}
    analysis = payload.get("analysis", []) or []

    return {
        "status": "success",
        **_row_to_summary(r),
        "scan_data": payload.get("scan_data", {}),
        "analysis": analysis,
        "breakdown": [
            {"item": a.get("analysisName"), "count": a.get("totalAmount")}
            for a in analysis
        ],
    }


@router.get("/{result_id}/images")
def get_result_images(
    result_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """The saved FO crops for a past scan.

    Legacy paged through these with previous/next buttons (main.py:2204-2277).
    Images are returned inline as data URIs so the browser needs no separate
    static mount, and the path is resolved from the stored image_unique_id
    rather than accepting one from the client.
    """
    r = db.query(Result).filter(Result.id == result_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Result not found")

    payload = r.result if isinstance(r.result, dict) else {}
    folder_name = (payload.get("scan_data", {}) or {}).get("image_unique_id", "")
    if not folder_name:
        return {"status": "success", "total": 0, "images": []}

    def slug(v):
        return (v or "").strip().lower().replace(" ", "_")

    folder = os.path.join(
        settings.OUTPUT_DIR, "output", slug(r.commodity), slug(r.variety), folder_name
    )
    # Guard against anything escaping the output tree.
    root = os.path.realpath(os.path.join(settings.OUTPUT_DIR, "output"))
    folder = os.path.realpath(folder)
    if not folder.startswith(root) or not os.path.isdir(folder):
        return {"status": "success", "total": 0, "images": []}

    names = sorted(
        f for f in os.listdir(folder) if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )
    page = names[offset : offset + limit]

    images = []
    for name in page:
        try:
            with open(os.path.join(folder, name), "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("ascii")
            mime = "image/png" if name.lower().endswith(".png") else "image/jpeg"
            images.append(
                {
                    "name": name,
                    # The FM type is the filename prefix — that is how results
                    # are counted, so it is the label to show.
                    "fm_type": name.rsplit("_", 1)[0].replace("_", " "),
                    "data_uri": f"data:{mime};base64,{encoded}",
                }
            )
        except Exception as exc:
            logger.warning("Could not read %s: %s", name, exc)

    return {
        "status": "success",
        "total": len(names),
        "limit": limit,
        "offset": offset,
        "images": images,
    }


@router.post("/{result_id}/resync")
def resync_result(result_id: int, db: Session = Depends(get_db)):
    """Manually re-deliver a stranded record.

    The automatic worker retries '0' records every 15 minutes; this is the
    operator-facing equivalent for when they do not want to wait.
    """
    r = db.query(Result).filter(Result.id == result_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Result not found")
    if not isinstance(r.result, dict):
        raise HTTPException(status_code=422, detail="Stored payload is not a JSON object")

    if not sync_service.is_authenticated:
        if not sync_service.login_qualix(
            settings.QUALIX_USERNAME, settings.QUALIX_PASSWORD
        ):
            raise HTTPException(status_code=502, detail="Could not authenticate with Qualix")

    status, error_code = sync_service.post_analysis_data(r.result)
    if status == "1":
        try:
            sync_service.post_to_sheets(r.result)
        except Exception as exc:
            logger.error("Sheets resync failed: %s", exc)

    DatabaseService(db).set_sync_status(result_id, status)
    return {
        "status": "success" if status == "1" else "failed",
        "sync_status": status,
        "sync_label": SYNC_LABELS.get(status, "Unknown"),
        "error_code": error_code,
    }
