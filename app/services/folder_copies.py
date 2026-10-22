"""Where each session folder has been copied, and who may remove it.

Two background jobs copy the device's session folders off the device: the
SSD archiver (app/services/ssd_archive) and the S3 uploader
(app/services/s3_worker.py). Each records here, per folder, what it has
confirmed (FolderCopy), and each reads the other's record before removing a
folder from the device. History reads it to find a batch or collection run
that has left the device.

Also the lock that keeps the two from working on device files at the same
time, held across processes (scripts/s3_upload_now.py runs outside the
backend).
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

TREES = ("output", "output_frame", "Data_Collection")

# eye_compass_be/.device_files.lock. Not inside a tree either job walks.
LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".device_files.lock")
_thread_lock = threading.Lock()


@contextmanager
def device_files_lock():
    """Yields True if this caller now holds the lock, False if the other job
    has it. Never waits: whoever finds it taken tries again at its next check."""
    if not _thread_lock.acquire(blocking=False):
        yield False
        return
    fh = None
    try:
        try:
            fh = open(LOCK_PATH, "w")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        except OSError as exc:
            # Cannot create the lock file: still exclusive within this process.
            logger.warning("Device files lock file unavailable (%s): %s", LOCK_PATH, exc)
        yield True
    finally:
        if fh is not None:
            fh.close()
        _thread_lock.release()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def get(tree: str, rel_path: str):
    """The folder's FolderCopy row (detached), or None."""
    return get_many(tree, [rel_path]).get(rel_path)


def get_many(tree: str, rel_paths: Iterable[str]) -> Dict[str, object]:
    from app.core.database import SessionLocal
    from app.models.schema import FolderCopy

    rels = list(rel_paths)
    if not rels:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(FolderCopy).filter(
            FolderCopy.tree == tree, FolderCopy.rel_path.in_(rels)).all()
        for r in rows:
            db.expunge(r)
        return {r.rel_path: r for r in rows}
    except Exception as exc:
        logger.error("Could not read copy records for %s: %s", tree, exc)
        return {}
    finally:
        db.close()


def _update(tree: str, rel_path: str, apply) -> bool:
    from app.core.database import SessionLocal
    from app.models.schema import FolderCopy

    db = SessionLocal()
    try:
        row = db.query(FolderCopy).filter_by(tree=tree, rel_path=rel_path).first()
        if row is None:
            row = FolderCopy(tree=tree, rel_path=rel_path, cloud_files_verified=0)
            db.add(row)
        apply(row)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Could not record copy of %s/%s: %s", tree, rel_path, exc)
        return False
    finally:
        db.close()


def record_ssd(tree: str, rel_path: str, ssd_name: str, ssd_path: str) -> bool:
    """Every file of the folder is on the archive SSD now. True once committed:
    nothing is removed from the device on the SSD's account until it is."""
    def apply(row):
        row.ssd_name = ssd_name
        row.ssd_path = ssd_path
        row.ssd_copied_at = datetime.now()
        row.ssd_removed_at = None
    return _update(tree, rel_path, apply)


def record_ssd_removed(tree: str, rel_path: str) -> None:
    """Deleted from the SSD after SSD_ARCHIVE_SSD_RETENTION_DAYS."""
    def apply(row):
        row.ssd_removed_at = datetime.now()
    _update(tree, rel_path, apply)


def by_ssd_path(ssd_paths: Iterable[str]) -> Dict[str, object]:
    """FolderCopy rows by their ssd_path (detached), for the SSD clean-up."""
    from app.core.database import SessionLocal
    from app.models.schema import FolderCopy

    paths = list(ssd_paths)
    if not paths:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(FolderCopy).filter(FolderCopy.ssd_path.in_(paths)).all()
        for r in rows:
            db.expunge(r)
        return {r.ssd_path: r for r in rows}
    finally:
        db.close()


def record_cloud(tree: str, rel_path: str, files_verified: int, all_verified: bool) -> None:
    """After the S3 uploader has been through the folder. Only a pass in which
    every file was confirmed identical on S3 sets cloud_uploaded_at."""
    def apply(row):
        row.cloud_files_verified = files_verified
        if all_verified:
            row.cloud_uploaded_at = datetime.now()
    _update(tree, rel_path, apply)


def record_removed(tree: str, rel_path: str, by: str) -> None:
    def apply(row):
        row.removed_at = datetime.now()
        row.removed_by = by
    _update(tree, rel_path, apply)


def _confirmed(at: Optional[datetime], newest_mtime: Optional[float]) -> bool:
    """Confirmed, and nothing in the folder changed after that."""
    if at is None:
        return False
    return newest_mtime is None or newest_mtime <= at.timestamp()


def on_ssd(copy, newest_mtime: Optional[float] = None) -> bool:
    return bool(copy) and not copy.ssd_removed_at and _confirmed(copy.ssd_copied_at, newest_mtime)


def in_cloud(copy, newest_mtime: Optional[float] = None) -> bool:
    return bool(copy) and _confirmed(copy.cloud_uploaded_at, newest_mtime)


# ---------------------------------------------------------------------------
# Reading back from the SSD
# ---------------------------------------------------------------------------

def ssd_folder(copy) -> Optional[str]:
    """The folder on the archive SSD, if that drive is connected now and the
    folder is on it."""
    if not (on_ssd(copy) and copy.ssd_path):
        return None
    from app.services.ssd_archive.config import ArchiveConfig
    from app.services.ssd_archive.target import connected_target

    target = connected_target(ArchiveConfig.from_settings())
    if target is None:
        return None
    base = os.path.realpath(str(target))
    folder = os.path.realpath(os.path.join(base, copy.ssd_path))
    if not folder.startswith(base + os.sep) or not os.path.isdir(folder):
        return None
    return folder


def ssd_connected() -> bool:
    from app.services.ssd_archive.config import ArchiveConfig
    from app.services.ssd_archive.target import connected_target

    return connected_target(ArchiveConfig.from_settings()) is not None


def storage_state(copy, where: Optional[str], in_progress: bool = False) -> str:
    """Where a folder's images can be seen, for History.

    where: "device", "ssd" or None, from whichever was found to hold images.
        on_device            on this device
        on_ssd               left the device, shown from the archive SSD
        in_cloud             left the device, not readable on the SSD now,
                             confirmed in S3
        missing_from_ssd     copied to the SSD, left the device, not found on
                             the SSD now (unplugged, or not on it), not in S3
        removed_from_ssd     left the device, then deleted from the SSD after
                             SSD_ARCHIVE_SSD_RETENTION_DAYS, not in S3
        missing_from_device  gone, and no copy anywhere is recorded
    """
    if where == "device" or in_progress:
        return "on_device"
    if where == "ssd":
        return "on_ssd"
    if in_cloud(copy):
        return "in_cloud"
    if on_ssd(copy):
        return "missing_from_ssd"
    if copy and copy.ssd_removed_at:
        return "removed_from_ssd"
    return "missing_from_device"


def copy_info(copy) -> Dict:
    """The copies, for History's detail views."""
    def iso(t):
        return t.isoformat(timespec="seconds") if t else None
    return {
        "on_ssd": on_ssd(copy),
        "in_cloud": in_cloud(copy),
        # Some files confirmed in S3, not all
        "cloud_partial": bool(copy) and not in_cloud(copy) and bool(copy.cloud_files_verified),
        "ssd": {
            "ssd": copy.ssd_name, "ssd_path": copy.ssd_path,
            "copied_at": iso(copy.ssd_copied_at),
        } if on_ssd(copy) else None,
        "cloud_uploaded_at": iso(copy.cloud_uploaded_at) if copy else None,
        "cloud_files_verified": (copy.cloud_files_verified or 0) if copy else 0,
        "removed_at": iso(copy.removed_at) if copy else None,
        "ssd_removed_at": iso(copy.ssd_removed_at) if copy else None,
        "removed_by": copy.removed_by if copy else None,
    }
