"""
Data Collection history: one database row per collection run.

A run's frames live in <OUTPUT_DIR>/Data_Collection/<commodity>/<variety>/
<epoch>_<sample_id>/. This module keeps a DataCollectionSession row for each
such folder, so past runs can be listed and reviewed. The folder stays the
source of truth for the images: whether they are still on the device is read
from it every time, never assumed from the row.

Lifecycle, driven from app/api/camera.py:

    record_start   first Start or Capture of a visit: the folder is created
                   and the row with it. Opening the page creates neither.
    record_update  Stop and Capture: counts so far.
    finalize       Submit, leaving for the next run, or startup: the run is
                   over. With no frames saved, the row and the empty folder
                   are removed instead (see discard_if_empty).

Every function here is best effort and never raises: a database problem must
not stop frames being collected. Failures are logged.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

SUBDIR = "Data_Collection"
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
_FOLDER_RE = re.compile(r"^(\d{9,11})_(.+)$")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def root() -> str:
    return os.path.join(settings.OUTPUT_DIR, SUBDIR)


def rel_path_of(folder: str) -> str:
    return os.path.relpath(folder, root()).replace(os.sep, "/")


def folder_of(rel_path: str) -> Optional[str]:
    """Absolute folder for a stored rel_path, or None if it would leave the
    Data_Collection tree."""
    base = os.path.realpath(root())
    folder = os.path.realpath(os.path.join(base, rel_path))
    return folder if folder.startswith(base + os.sep) else None


def list_images(folder: Optional[str]) -> List[str]:
    if not folder:
        return []
    try:
        with os.scandir(folder) as it:
            return sorted(
                e.name for e in it
                if e.is_file(follow_symlinks=False) and e.name.lower().endswith(IMAGE_EXTS)
            )
    except OSError:
        return []


def _newest_image_mtime(folder: str, names: List[str]) -> Optional[float]:
    newest = None
    for name in names:
        try:
            m = os.lstat(os.path.join(folder, name)).st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    return newest


def _ts(value: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(value) if value else None


# ---------------------------------------------------------------------------
# Recording lifecycle
# ---------------------------------------------------------------------------

def record_start(folder: str, sample_id: str, commodity: str, variety: str,
                 capture_settings: Dict, started_at: float) -> None:
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    rel = rel_path_of(folder)
    db = SessionLocal()
    try:
        if db.query(DataCollectionSession).filter_by(rel_path=rel).first():
            return
        db.add(DataCollectionSession(
            rel_path=rel, sample_id=sample_id, commodity=commodity, variety=variety,
            status="recording", source="live", started_at=_ts(started_at),
            frames_saved=0, frames_seen=0, frames_dropped=0,
            capture_settings={
                k: capture_settings.get(k)
                for k in ("save_every_n_frames", "image_format", "jpeg_quality")
            },
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not record data collection start for %s: %s", rel, exc)
    finally:
        db.close()


def _apply_status(row, status: Dict) -> None:
    row.frames_saved = int(status.get("frame_count") or 0)
    row.frames_seen = int(status.get("frames_seen") or 0)
    row.frames_dropped = int(status.get("frames_dropped") or 0)
    row.duration_seconds = int(round(status.get("duration_seconds") or 0))
    if status.get("stop_reason"):
        row.stop_reason = status["stop_reason"]
    if status.get("ended_at"):
        try:
            row.ended_at = datetime.fromisoformat(status["ended_at"])
        except ValueError:
            pass


def record_update(folder: str, status: Dict) -> None:
    """Counts so far, from camera._dc_status(). The row stays `recording`."""
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    rel = rel_path_of(folder)
    db = SessionLocal()
    try:
        row = db.query(DataCollectionSession).filter_by(rel_path=rel).first()
        if row is None:
            return
        _apply_status(row, status)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not update data collection record %s: %s", rel, exc)
    finally:
        db.close()


def discard_if_empty(folder: str) -> bool:
    """Remove a run that saved nothing: its folder, then its row.

    The CALLER guarantees the run is over: not recording, and no frame write
    still queued. That alone is not trusted with the disk, though. The folder
    is removed with os.rmdir, which the kernel refuses for a folder holding
    anything at all, half-written files included, so a frame that landed at
    the last moment keeps both the folder and the row. The row is removed
    only if it also says nothing was saved, so a run whose frames went to S3
    (folder gone, frames_saved > 0) is never mistaken for an empty one.
    """
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    if os.path.isdir(folder):
        try:
            os.rmdir(folder)
        except OSError:
            return False
    rel = rel_path_of(folder)
    db = SessionLocal()
    try:
        row = db.query(DataCollectionSession).filter_by(rel_path=rel).first()
        if row is not None and not row.frames_saved:
            db.delete(row)
            db.commit()
        logger.info("Data collection run %s saved no frames: folder and record removed", rel)
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Could not remove empty data collection record %s: %s", rel, exc)
        return False
    finally:
        db.close()


def finalize(folder: str, status: Dict, final_status: str) -> None:
    """The run is over (see discard_if_empty for what the caller guarantees)."""
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    if not status.get("frame_count") and not list_images(folder):
        if discard_if_empty(folder):
            return

    rel = rel_path_of(folder)
    db = SessionLocal()
    try:
        row = db.query(DataCollectionSession).filter_by(rel_path=rel).first()
        if row is None:
            return
        _apply_status(row, status)
        # A frame the counters missed (e.g. a write that reported failure but
        # left a file) is still a frame the operator has; the folder wins.
        row.frames_saved = max(row.frames_saved or 0, len(list_images(folder)))
        row.status = final_status
        if row.ended_at is None:
            row.ended_at = datetime.now()
        db.commit()
        logger.info("Data collection run %s %s: %s frame(s)", rel, final_status, row.frames_saved)
    except Exception as exc:
        db.rollback()
        logger.error("Could not finalize data collection record %s: %s", rel, exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup: close runs cut off by a restart, import folders from before this
# ---------------------------------------------------------------------------

def _display_names(db) -> tuple:
    """Folder segment -> configured commodity name / variety code, so an
    imported folder named toor_dal/pu2-td04-gn0 shows as the operator knows it."""
    from app.models.schema import CommodityDetails
    from app.services.inference_service import normalize_commodity

    commodities, varieties = {}, {}
    for c in db.query(CommodityDetails).all():
        if c.commodity:
            commodities.setdefault(normalize_commodity(c.commodity), c.commodity)
        for v in c.variety or []:
            code = v.get("variety_code") if isinstance(v, dict) else None
            if code:
                varieties.setdefault(normalize_commodity(code), code)
    return commodities, varieties


def reconcile_on_startup() -> None:
    """Run once at startup, before the S3 worker and before any request.

    Nothing can be writing into Data_Collection at that point, which is what
    makes it safe to remove empty folders here.
    """
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    db = SessionLocal()
    closed = removed = imported = 0
    try:
        # 1. Runs that were recording when the backend stopped.
        for row in db.query(DataCollectionSession).filter_by(status="recording").all():
            folder = folder_of(row.rel_path)
            names = list_images(folder)
            if not row.frames_saved and not names:
                if folder and os.path.isdir(folder):
                    try:
                        os.rmdir(folder)
                    except OSError:
                        row.status = "interrupted"
                        continue
                db.delete(row)
                removed += 1
                continue
            row.frames_saved = max(row.frames_saved or 0, len(names))
            newest = _newest_image_mtime(folder, names) if names else None
            row.ended_at = row.ended_at or _ts(newest) or row.updated_at
            if row.started_at and row.ended_at:
                row.duration_seconds = max(0, int((row.ended_at - row.started_at).total_seconds()))
            row.status = "interrupted"
            closed += 1
        db.commit()

        # 2. Folders with no row: from before this table, or a row lost.
        known = {r for (r,) in db.query(DataCollectionSession.rel_path).all()}
        base = root()
        if os.path.isdir(base):
            commodities, varieties = _display_names(db)
            for folder in _session_folders(base):
                rel = rel_path_of(folder)
                if rel in known:
                    continue
                names = list_images(folder)
                if not names:
                    try:
                        os.rmdir(folder)  # only succeeds if completely empty
                        removed += 1
                    except OSError:
                        pass
                    continue
                c_seg, v_seg, leaf = rel.split("/", 2)
                m = _FOLDER_RE.match(leaf)
                started = _ts(int(m.group(1))) if m else None
                ended = _ts(_newest_image_mtime(folder, names))
                db.add(DataCollectionSession(
                    rel_path=rel,
                    sample_id=m.group(2) if m else leaf,
                    commodity=commodities.get(c_seg, c_seg),
                    variety=varieties.get(v_seg, v_seg),
                    status="completed", source="imported",
                    started_at=started or ended, ended_at=ended,
                    duration_seconds=(
                        max(0, int((ended - started).total_seconds()))
                        if started and ended else 0),
                    frames_saved=len(names), frames_seen=None, frames_dropped=0,
                    capture_settings={
                        "image_format": os.path.splitext(names[0])[1].lstrip(".").lower()},
                ))
                imported += 1
            db.commit()
        logger.info(
            "Data collection history: %s interrupted run(s) closed, %s empty "
            "run(s) removed, %s folder(s) imported", closed, removed, imported)
    except Exception as exc:
        db.rollback()
        logger.error("Data collection history reconcile failed: %s", exc)
    finally:
        db.close()


def _session_folders(base: str) -> List[str]:
    level = [base]
    for _ in range(3):
        nxt = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    nxt.extend(e.path for e in it if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
        level = nxt
    return sorted(level)


# ---------------------------------------------------------------------------
# S3 worker
# ---------------------------------------------------------------------------

def record_cloud_result(rel_path: str, files_verified: int, all_verified: bool) -> None:
    """After the S3 worker has been through one Data_Collection folder.

    files_verified counts files confirmed identical on S3 in this pass.
    all_verified means every file in the folder was. Only that marks the run
    uploaded; a partial pass is picked up again next run.
    """
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    db = SessionLocal()
    try:
        row = db.query(DataCollectionSession).filter_by(rel_path=rel_path).first()
        if row is None:
            return
        row.cloud_files_verified = max(row.cloud_files_verified or 0, files_verified)
        if all_verified and row.cloud_uploaded_at is None:
            row.cloud_uploaded_at = datetime.now()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not record S3 result for data collection %s: %s", rel_path, exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The SSD archiver (app/services/ssd_archive)
# ---------------------------------------------------------------------------

def record_ssd_copy(rel_path: str, ssd_name: str, ssd_path: str,
                    at: Optional[datetime] = None) -> bool:
    """Every file of the run is on the archive SSD, at ssd_path under its root.

    True only once that is committed: the archiver removes nothing from the
    device until it is, so the history always knows where a run went. Repeat
    calls keep the first ssd_archived_at. A folder with no row yet (from
    before the history table, reached before startup imported it) gets one.
    """
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    when = at or datetime.now()
    db = SessionLocal()
    try:
        row = db.query(DataCollectionSession).filter_by(rel_path=rel_path).first()
        if row is None:
            c_seg, v_seg, leaf = (rel_path.split("/", 2) + ["", "", ""])[:3]
            m = _FOLDER_RE.match(leaf)
            row = DataCollectionSession(
                rel_path=rel_path, sample_id=m.group(2) if m else leaf,
                commodity=c_seg, variety=v_seg, status="completed", source="imported",
                started_at=_ts(int(m.group(1))) if m else when,
                frames_saved=len(list_images(folder_of(rel_path))))
            db.add(row)
        row.ssd_name = ssd_name
        row.ssd_path = ssd_path
        if row.ssd_archived_at is None:
            row.ssd_archived_at = when
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Could not record %s as archived to the SSD: %s", rel_path, exc)
        return False
    finally:
        db.close()


def record_ssd_removed(rel_path: str, at: Optional[datetime] = None) -> None:
    """The run was removed from the device after its SSD copy was verified."""
    from app.core.database import SessionLocal
    from app.models.schema import DataCollectionSession

    db = SessionLocal()
    try:
        row = db.query(DataCollectionSession).filter_by(rel_path=rel_path).first()
        if row is not None and row.ssd_removed_at is None:
            row.ssd_removed_at = at or datetime.now()
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not record %s as removed from the device: %s", rel_path, exc)
    finally:
        db.close()


def ssd_folder(row) -> Optional[str]:
    """The run's folder on the archive SSD, if that drive is connected now and
    the folder is on it."""
    if not (row.ssd_archived_at and row.ssd_path):
        return None
    from app.services.ssd_archive.config import ArchiveConfig
    from app.services.ssd_archive.target import connected_target

    target = connected_target(ArchiveConfig.from_settings())
    if target is None:
        return None
    base = os.path.realpath(str(target))
    folder = os.path.realpath(os.path.join(base, row.ssd_path))
    if not folder.startswith(base + os.sep) or not os.path.isdir(folder):
        return None
    return folder


def ssd_connected() -> bool:
    from app.services.ssd_archive.config import ArchiveConfig
    from app.services.ssd_archive.target import connected_target

    return connected_target(ArchiveConfig.from_settings()) is not None


def ssd_info(row) -> Optional[Dict]:
    """What the archiver recorded for this run, or None if it never copied it."""
    if not row.ssd_archived_at:
        return None
    return {
        "ssd": row.ssd_name,
        "ssd_path": row.ssd_path,
        "archived_at": row.ssd_archived_at.isoformat(timespec="seconds"),
        "removed_at": row.ssd_removed_at.isoformat(timespec="seconds") if row.ssd_removed_at else None,
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def image_folder(row):
    """(folder, where, names) for the run's frames: the device while it has
    any, else the archive SSD if the run was copied there and the drive is
    connected. where is "device", "ssd" or None (nowhere to read them)."""
    folder = folder_of(row.rel_path)
    names = list_images(folder)
    if names:
        return folder, "device", names
    ssd = ssd_folder(row)
    names = list_images(ssd)
    if names:
        return ssd, "ssd", names
    return None, None, []


def storage_state(row, where: Optional[str]) -> str:
    """on_device | on_device_uploaded | partially_uploaded | on_ssd
    | missing_from_ssd | uploaded_cleaned | missing_from_device.

    where is image_folder()'s answer. on_ssd: gone from the device, readable
    on the archive SSD. missing_from_ssd: copied to the SSD, gone from the
    device, and not found on the SSD now (the drive is unplugged, or the
    folder is not on it). missing_from_device: gone from the device, and
    neither the archiver nor the S3 worker accounts for it."""
    if where == "device":
        if row.cloud_uploaded_at:
            return "on_device_uploaded"
        if row.cloud_files_verified:
            return "partially_uploaded"
        return "on_device"
    if row.status == "recording":
        return "on_device"
    if row.ssd_archived_at:
        return "on_ssd" if where == "ssd" else "missing_from_ssd"
    if row.cloud_uploaded_at or (
            row.frames_saved and (row.cloud_files_verified or 0) >= row.frames_saved):
        return "uploaded_cleaned"
    return "missing_from_device"


def window_bounds(date_from: Optional[str], date_to: Optional[str]):
    """YYYY-MM-DD strings to [start, end) datetimes; either may be None."""
    start = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
    end = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)) if date_to else None
    return start, end


# Thumbnails. The frames are full camera frames (about 1.6 MB each as PNG), so
# the gallery is never sent the frames themselves. Kept in memory rather than
# on disk: writing them next to the frames would make the S3 worker upload
# them and would reset the folder's age, which decides when it is uploaded.
THUMB_WIDTH = 360
_THUMB_CACHE_MAX = 600
_thumb_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_thumb_lock = threading.Lock()
# At most two decodes at once, so opening a gallery cannot take every core
# from the camera and inference.
_decode_slots = threading.BoundedSemaphore(2)


def thumbnail(path: str) -> Optional[bytes]:
    import cv2

    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    with _thumb_lock:
        hit = _thumb_cache.get(key)
        if hit is not None:
            _thumb_cache.move_to_end(key)
            return hit
    with _decode_slots:
        img = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_2)
        if img is None:
            return None
        h, w = img.shape[:2]
        if w > THUMB_WIDTH:
            img = cv2.resize(img, (THUMB_WIDTH, int(h * THUMB_WIDTH / w)),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return None
    data = buf.tobytes()
    with _thumb_lock:
        _thumb_cache[key] = data
        while len(_thumb_cache) > _THUMB_CACHE_MAX:
            _thumb_cache.popitem(last=False)
    return data
