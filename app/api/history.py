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
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.models.schema import Result
from app.services.database_service import DatabaseService
from app.services.sync_lock import claim_result
from app.services.sync_service import sync_service

logger = logging.getLogger(__name__)
router = APIRouter()

# '2' means Qualix rejected the payload outright (HTTP 400) — a permanent,
# data-shaped failure, not a transient one. Legacy's own retry worker only
# ever queries sync_status == '0' (main.py:2891's get_unsynced_records), so
# legacy never retried a '2' either; resending the exact same bytes would
# just get rejected again. Labeled "Rejected" rather than "Sync Failed" (on
# request) so it doesn't read as something a retry could fix.
SYNC_LABELS = {"1": "Synced", "2": "Rejected", "0": "Pending"}


def _strip_trailing_numeric_tokens(stem: str) -> list:
    """Drop every trailing underscore-separated token that's purely digits
    (a timestamp, a box index, or both) from a crop filename's stem (no
    extension), leaving just the FM-type words. See get_result_images'
    fm_type field below for why this can't assume exactly one such token.
    """
    parts = stem.split("_")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    return parts


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
        "sync_error": r.sync_error or "",
        # Legacy history columns
        "receiving_date": scan_data.get("receiving_date", ""),
        "vendor_name": scan_data.get("vendor_name", ""),
        "sorted_quantity": scan_data.get("weight", ""),
        "total_fo_detected": total_fo,
        "image_unique_id": scan_data.get("image_unique_id", ""),
    }


def _window_start(days: int) -> Optional[str]:
    """The oldest scan date the History list will show, as "%Y-%m-%d".

    None when windowing is switched off (HISTORY_WINDOW_DAYS=0), meaning show
    everything. Returned as a string because Result.date is one — see
    get_history for why comparing it as text is sound.
    """
    if days <= 0:
        return None
    return (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")


@router.get("/")
def get_history(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    days: Optional[int] = Query(None, ge=0),
):
    """Paged history, limited to the last HISTORY_WINDOW_DAYS days (30).

    Sorted latest-first by scan date/start_time (r.date, r.start_time — both
    plain strings but written as "%Y-%m-%d"/"%H:%M:%S" by database_service.py,
    so a string sort is chronological). This deviates from legacy's
    populate_history_table (main.py:2100), which grouped rows by commodity
    name (then receiving date) instead of scan recency — changed back to
    latest-first on request.

    That zero-padded "%Y-%m-%d" shape is also why the window can be a plain
    text comparison against Result.date: for that format, and only for it,
    lexicographic order is chronological order. It keeps the filter on the
    indexed column instead of casting every row to a date to compare it.

    The window is on the scan date (when it ran), not receiving_date (when the
    goods arrived, operator-entered and free to be older). "Last 30 days"
    means the device's own last 30 days of work, which is the question the
    operator is actually asking.

    `days` overrides the configured window per request — 0 for everything.
    Nothing on the kiosk sends it; it exists so support can pull an older
    record without editing the device's .env and restarting it.

    Filtering, ordering and paging all happen in SQL now. They used to happen
    in Python over `db.query(Result).all()`, which read every scan the device
    had ever taken — full JSONB datagrams and all — to then return twenty of
    them. That was already wasteful and is worse once most of what it reads
    is outside the window and gets discarded.
    """
    try:
        window_days = settings.HISTORY_WINDOW_DAYS if days is None else days
        start = _window_start(window_days)

        query = db.query(Result)
        if start is not None:
            query = query.filter(Result.date >= start)

        total = query.count()
        rows = (
            query.order_by(Result.date.desc(), Result.start_time.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "status": "success",
            "total": total,
            "limit": limit,
            "offset": offset,
            # So the screen can say what it is showing rather than letting an
            # older scan's absence read as data loss. 0 = unwindowed.
            "window_days": window_days,
            "window_start": start or "",
            "data": [_row_to_summary(r) for r in rows],
        }
    except Exception as exc:
        logger.error("Failed to fetch history: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{result_id}")
def get_result_detail(result_id: int, db: Session = Depends(get_db)):
    """Full drill-down: the per-FO Item/Count breakdown legacy showed on its
    History-detail screen.

    The FRESH-submit results table (populate_result_table, main.py:1689/2141)
    is called with ONLY create_results()'s output, so it shows just the
    FM/NON-FM/Blower/Magnetic counts. But the History-detail table
    (set_history_options_assessment, main.py:2181-2205) is built from the
    saved record's full `analysis` array instead — result + looker_data +
    total_fo_detected merged, all unfiltered (confirmed against a real prod
    device: Frame Count/FM Stop Count/Manual Stop Count/FM Stop
    Time/Manual Stop Time/Total Stop Time/total_fo_detected all show up
    there). `breakdown` here matches that — nothing excluded — since this
    endpoint only ever backs the saved/History-detail view, never the fresh
    one."""
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
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    item: str | None = Query(None),
):
    """The saved FO crops for a past scan.

    The frontend's gallery is a single scrollable grid now (not legacy's
    paged previous/next viewer), so it requests every crop in one call — the
    le cap here just needs to comfortably exceed any real scan's crop count,
    not stay small. Legacy paged through these with previous/next buttons
    (main.py:2204-2277).
    Images are returned inline as data URIs so the browser needs no separate
    static mount, and the path is resolved from the stored image_unique_id
    rather than accepting one from the client.

    `item` mirrors legacy's own drill-down (set_images/main.py:2219-2233):
    clicking a row in the breakdown table there filters the image grid to
    just that Item's crops via a case-insensitive substring match against the
    filename. Same idea here, tolerant of the Item label using spaces where
    the filename uses underscores.
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
    if item:
        needle = item.strip().lower()
        needle_slug = needle.replace(" ", "_")
        names = [
            f for f in names
            if needle in f.lower() or needle_slug in f.lower()
        ]
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
                    # are counted, so it is the label to show. A plain
                    # rsplit("_", 1) assumed exactly one trailing numeric
                    # token (a timestamp) — already wrong for NON-FM crops
                    # (NON-FM_<ts>_<box index>.png has two), and now also for
                    # labelled ones (scan_session.label_detection appends a
                    # box index too, to make its object_id collision-proof).
                    # Stripping every trailing all-digit token, not just the
                    # last one, handles any of these regardless of how many
                    # numeric suffixes a given crop's filename happens to have.
                    "fm_type": " ".join(
                        _strip_trailing_numeric_tokens(name.rsplit(".", 1)[0])
                    ),
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

    The automatic worker retries '0' records every SYNC_RETRY_INTERVAL_MINUTES
    (default 30); this is the operator-facing equivalent for when they do not
    want to wait.

    Answers 409 if that record is already being delivered — by the post that
    followed the batch, by the worker, or by an earlier click of this same
    button. Pressing it during the (roughly half-minute) window one of those is
    open would otherwise send the scan twice in parallel, which Qualix shrugs
    off but Google Sheets does not: post_to_sheets checks for an existing row
    and then appends, so two deliveries interleaving between those steps both
    append. See sync_lock.
    """
    r = db.query(Result).filter(Result.id == result_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Result not found")
    if not isinstance(r.result, dict):
        raise HTTPException(status_code=422, detail="Stored payload is not a JSON object")

    if not sync_service.is_authenticated:
        # Under AUTH_PROVIDER=keycloak, is_authenticated has already tried (and
        # failed) to get the sync service token, and there is no separate
        # Qualix password login left to attempt — the same guard the retry
        # worker uses. Without it this fell back to a direct legacy Qualix
        # login even in Keycloak mode, which could not help either way:
        # _auth_headers() ignores self.access_token there and sends the
        # Keycloak service token regardless.
        if settings.AUTH_PROVIDER == "keycloak" or not sync_service.login_qualix(
            settings.SYNC_SERVICE_USERNAME, settings.SYNC_SERVICE_PASSWORD
        ):
            raise HTTPException(status_code=502, detail="Could not authenticate with Qualix")

    with claim_result(result_id) as granted:
        if not granted:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This record is already being sent. Give it a moment — the "
                    "status updates on its own."
                ),
            )

        status, error_code, error_detail = sync_service.post_analysis_data(r.result)
        if status == "1":
            try:
                sync_service.post_to_sheets(r.result)
            except Exception as exc:
                logger.error("Sheets resync failed: %s", exc)

        DatabaseService(db).set_sync_status(result_id, status, error_detail)
        return {
            "status": "success" if status == "1" else "failed",
            "sync_status": status,
            "sync_label": SYNC_LABELS.get(status, "Unknown"),
            "error_code": error_code,
            "sync_error": error_detail,
        }
