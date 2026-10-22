"""
Data Collection history: past collection runs and their frames.

The runs themselves are recorded by app/api/camera.py through
app/services/dc_history.py. This router only reads.

Images are served as URLs rather than inlined as data URIs the way the batch
History crops are (api/history.py): a collection frame is a full camera frame,
about 1.6 MB as PNG, and a run holds around a hundred. The gallery loads small
JPEG thumbnails, and a frame is sent in full only when it is opened.

A run the SSD archiver has moved off the device is read from the archive SSD
while that drive is connected (dc_history.image_folder).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schema import DataCollectionSession
from app.services import dc_history
from app.services.inference_service import normalize_commodity

logger = logging.getLogger(__name__)
router = APIRouter()


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
# The frames of a run never change once written, so the browser may keep them.
_CACHE = {"Cache-Control": "private, max-age=86400"}


def _live_status(row: DataCollectionSession) -> Optional[dict]:
    """Counts for the run currently on the Data Collection page, which the row
    only has as of its last Stop."""
    if row.status != "recording":
        return None
    from app.api import camera

    folder = camera._dc_folder
    if folder and dc_history.rel_path_of(folder) == row.rel_path:
        return camera._dc_status()
    return None


def _summary(row: DataCollectionSession) -> dict:
    _, where, names = dc_history.image_folder(row)
    live = _live_status(row)
    return {
        "id": row.id,
        "sample_id": row.sample_id,
        "commodity": row.commodity,
        "variety": row.variety,
        "status": row.status,
        "source": row.source,
        "started_at": row.started_at.isoformat(timespec="seconds") if row.started_at else None,
        "ended_at": row.ended_at.isoformat(timespec="seconds") if row.ended_at else None,
        "duration_seconds": int(live["duration_seconds"]) if live else row.duration_seconds,
        "frames_saved": live["frame_count"] if live else row.frames_saved,
        "frames_seen": live["frames_seen"] if live else row.frames_seen,
        "frames_dropped": live["frames_dropped"] if live else row.frames_dropped,
        "images_on_device": len(names) if where == "device" else 0,
        "storage": dc_history.storage_state(row, where),
        # Copied to the archive SSD, whether or not it is still on the device.
        "on_ssd": bool(row.ssd_archived_at),
    }


def _get_row(db: Session, session_id: int) -> DataCollectionSession:
    row = db.query(DataCollectionSession).filter_by(id=session_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Collection run not found")
    return row


@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = Query(None, pattern=_DATE_RE),
    date_to: Optional[str] = Query(None, pattern=_DATE_RE),
    commodity: Optional[str] = Query(None),
    variety: Optional[str] = Query(None),
):
    """Latest first. commodity/variety match the folder segments the run was
    stored under, so a run imported from disk (folder names only) and one
    recorded live (display names) filter together."""
    query = db.query(DataCollectionSession)
    start, end = dc_history.window_bounds(date_from, date_to)
    if start:
        query = query.filter(DataCollectionSession.started_at >= start)
    if end:
        query = query.filter(DataCollectionSession.started_at < end)
    if commodity:
        prefix = normalize_commodity(commodity) + "/"
        if variety:
            prefix += normalize_commodity(variety) + "/"
        query = query.filter(DataCollectionSession.rel_path.startswith(prefix, autoescape=True))

    total = query.count()
    rows = (
        query.order_by(DataCollectionSession.started_at.desc(), DataCollectionSession.id.desc())
        .offset(offset).limit(limit).all()
    )
    return {
        "status": "success",
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [_summary(r) for r in rows],
        "ssd_connected": dc_history.ssd_connected(),
    }


@router.get("/sessions/filters")
def session_filters(db: Session = Depends(get_db)):
    """The commodities and varieties that have runs, for the filter dropdowns.
    Values are what /sessions expects; labels are what to show."""
    commodities, varieties = {}, {}
    for c, v, rel in db.query(DataCollectionSession.commodity,
                              DataCollectionSession.variety,
                              DataCollectionSession.rel_path).all():
        c_seg, v_seg = rel.split("/")[:2]
        commodities.setdefault(c_seg, c or c_seg)
        varieties.setdefault(c_seg, {}).setdefault(v_seg, v or v_seg)
    return {
        "status": "success",
        "commodities": [{"value": k, "label": commodities[k]} for k in sorted(commodities)],
        "varieties": {
            c: [{"value": k, "label": vs[k]} for k in sorted(vs)] for c, vs in varieties.items()
        },
    }


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    row = _get_row(db, session_id)
    return {
        "status": "success",
        **_summary(row),
        "stop_reason": row.stop_reason,
        "capture_settings": row.capture_settings or {},
        "cloud_files_verified": row.cloud_files_verified or 0,
        "cloud_uploaded_at": (
            row.cloud_uploaded_at.isoformat(timespec="seconds") if row.cloud_uploaded_at else None),
        "folder": f"{dc_history.SUBDIR}/{row.rel_path}",
        "ssd": dc_history.ssd_info(row),
        "ssd_connected": dc_history.ssd_connected(),
    }


@router.get("/sessions/{session_id}/images")
def list_session_images(
    session_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(48, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    row = _get_row(db, session_id)
    _, where, names = dc_history.image_folder(row)
    base = f"/api/data_collection/sessions/{session_id}/images"
    return {
        "status": "success",
        "source": where,
        "total": len(names),
        "limit": limit,
        "offset": offset,
        "images": [
            {"name": n, "thumb_url": f"{base}/{n}/thumb", "url": f"{base}/{n}"}
            for n in names[offset: offset + limit]
        ],
    }


def _image_path(db: Session, session_id: int, name: str) -> str:
    """Only a file that is actually in the run's folder, by exact name."""
    row = _get_row(db, session_id)
    folder, _, names = dc_history.image_folder(row)
    if not _NAME_RE.match(name) or name not in names:
        raise HTTPException(status_code=404, detail="Image not found")
    return os.path.join(folder, name)


@router.get("/sessions/{session_id}/images/{name}/thumb")
def get_thumbnail(session_id: int, name: str, db: Session = Depends(get_db)):
    data = dc_history.thumbnail(_image_path(db, session_id, name))
    if data is None:
        raise HTTPException(status_code=404, detail="Image could not be read")
    return Response(content=data, media_type="image/jpeg", headers=_CACHE)


@router.get("/sessions/{session_id}/images/{name}")
def get_image(session_id: int, name: str, db: Session = Depends(get_db)):
    path = _image_path(db, session_id, name)
    media = "image/png" if name.lower().endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media, headers=_CACHE)
