"""The archive run, and the background task that runs it once a day.

A run
    1. Find the archive SSD; if it is not connected, try again next check.
    2. Find the samples on the device (Data_Collection/, output_frame/).
    3. Check the SSD has room for everything not on it yet.
    4. Copy and verify every file. Device files are not touched here. For a
       Data Collection run whose files are all on the SSD, the run's history
       row is marked with where it went (dc_history.record_ssd_copy).
    5. Device clean-up: a sample older than SSD_ARCHIVE_DEVICE_RETENTION_DAYS
       is removed from the device only if EVERY file of it is re-hashed now
       and matches its SSD copy, and, for a Data Collection run, only once
       its row records the SSD copy. Otherwise the whole sample stays.
    6. Record the result; the day counts as done only if nothing failed.

Why inside the backend: the database is updated in the same process that
copies and deletes, so it cannot miss an update the way a separate program
calling an API could (backend restarting, token missing). And the backend
knows exactly what is in use: no run starts, and a run in progress stops,
while a batch scan or data collection is running, and the folders of the
active batch, held batches and the current collection run are never removed.
The S3 uploader and this archiver never change device files at the same time
(DEVICE_FILES_LOCK).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from . import copier
from .config import ArchiveConfig
from .layout import SOURCES, Sample, discover
from .target import find_target

logger = logging.getLogger(__name__)

TRACKED_SOURCE = "Data_Collection"  # the source with history rows
DATE_FOLDER = "%Y-%m-%d"

# Held by the S3 uploader or this archiver while it uploads, copies or
# deletes device files, so the two never work on the same folder at once.
# Neither waits for the other: whichever finds it taken tries again at its
# next check.
DEVICE_FILES_LOCK = threading.Lock()


class Deferred(Exception):
    """A scan or data collection started mid-run. Stop cleanly, retry later."""


@dataclass
class Stats:
    copied: int = 0
    conflicts: int = 0
    present: int = 0
    failed: int = 0
    copied_bytes: int = 0
    deleted_samples: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    kept_unverified: int = 0
    kept_unrecorded: int = 0
    kept_in_use: int = 0
    errors: List[str] = field(default_factory=list)


def _fmt(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def dest_dir(target: Path, sample: Sample) -> Path:
    """<SSD>/<YYYY-MM-DD>/<source>/<commodity>/<variety>/<sample_id>"""
    return (
        target
        / sample.taken_at.strftime(DATE_FOLDER)
        / sample.source
        / copier.safe_component(sample.commodity)
        / copier.safe_component(sample.variety)
        / copier.safe_component(sample.name)
    )


def dest_for(target: Path, sample: Sample, f: Path) -> Path:
    rel = f.relative_to(sample.path)
    return dest_dir(target, sample).joinpath(*(copier.safe_component(p) for p in rel.parts))


def _rel_path(cfg: ArchiveConfig, sample: Sample) -> str:
    """The history row's rel_path: <commodity>/<variety>/<folder>."""
    return sample.path.relative_to(cfg.data_root / sample.source).as_posix()


def _record_copy(cfg: ArchiveConfig, target: Path, sample: Sample) -> bool:
    """Mark a Data Collection run as on the SSD. True if recorded, or if the
    sample has no history row to mark (output_frame)."""
    if sample.source != TRACKED_SOURCE:
        return True
    from app.services import dc_history

    return dc_history.record_ssd_copy(
        _rel_path(cfg, sample), target.name,
        dest_dir(target, sample).relative_to(target).as_posix())


def _busy() -> Optional[str]:
    from app.services.s3_worker import S3UploaderTask

    return S3UploaderTask._busy()


def _protected_dirs() -> Set[str]:
    """Folders in use or that may still be written to. Raises if the database
    cannot say which batches are held: unknown is not "none"."""
    from app.services.s3_worker import S3UploaderTask

    return S3UploaderTask._protected_dirs()


def _clock_is_sane(samples: List[Sample]) -> bool:
    """A device clock far behind its own data makes deletions untrustworthy."""
    newest = max((s.newest_mtime for s in samples), default=0.0)
    if newest and time.time() < newest - 86400:
        logger.error(
            "SSD archive: system clock (%s) is behind the newest data (%s) - device "
            "clean-up skipped until the clock is correct",
            datetime.now().isoformat(timespec="seconds"),
            datetime.fromtimestamp(newest).isoformat(timespec="seconds"),
        )
        return False
    return True


def _remove_empty_dirs(start: Path, stop: Path) -> None:
    """Remove start and its empty parents, never removing stop itself."""
    for folder in sorted((p for p in start.rglob("*") if p.is_dir()),
                         key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass
    current = start
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def copy_phase(samples: List[Sample], target: Path, cfg: ArchiveConfig, stats: Stats,
               dry_run: bool, busy: Callable[[], Optional[str]]) -> None:
    for sample in samples:
        if not sample.files:
            continue
        logger.debug("[%s] %s -> %s", sample.source, sample.path, dest_dir(target, sample))
        failed = 0
        for f in sample.files:
            reason = busy()
            if reason:
                raise Deferred(reason)
            dest = dest_for(target, sample, f)
            if dry_run:
                if not (dest.exists() and copier.looks_identical(f, dest)):
                    stats.copied += 1
                    stats.copied_bytes += f.stat().st_size
                else:
                    stats.present += 1
                continue
            outcome = copier.archive_file(f, dest)
            if outcome.status == "failed":
                failed += 1
                stats.failed += 1
                stats.errors.append(f"{f}: {outcome.error}")
                logger.error("[%s] copy to SSD failed, kept on device: %s (%s)",
                             sample.source, f, outcome.error)
                if not target.exists():
                    raise OSError("archive SSD disconnected during the copy")
            elif outcome.status == "present":
                stats.present += 1
            else:
                stats.copied += 1
                stats.copied_bytes += f.stat().st_size
                if outcome.status == "conflict":
                    stats.conflicts += 1
                logger.debug("    %s %s", outcome.status, f.relative_to(sample.path))
        # Only a run whose every file is on the SSD is marked; one still being
        # written to (too_new) is marked on a later day.
        if not dry_run and not failed and not sample.too_new:
            _record_copy(cfg, target, sample)


def cleanup_phase(samples: List[Sample], target: Path, cfg: ArchiveConfig, stats: Stats,
                  dry_run: bool, protected: Set[str],
                  busy: Callable[[], Optional[str]]) -> None:
    cutoff_date = date.today() - timedelta(days=cfg.device_retention_days)
    cutoff_time = time.time() - cfg.device_retention_days * 86400

    for sample in samples:
        if sample.taken_at.date() >= cutoff_date:
            continue
        # Still being added to (e.g. a held batch resumed recently): keep
        if sample.too_new or sample.newest_mtime > cutoff_time:
            logger.info("[%s] %s is old but has recent files - kept on device",
                        sample.source, sample.name)
            continue
        if not sample.files:
            continue
        if os.path.realpath(sample.path) in protected:
            stats.kept_in_use += 1
            logger.info("[%s] %s is in use (open or held batch) - kept on device",
                        sample.source, sample.name)
            continue
        reason = busy()
        if reason:
            raise Deferred(reason)

        if dry_run:
            # Nothing was copied in a dry run, so verification cannot be shown;
            # report what a real run would remove once its copies verify.
            stats.deleted_samples += 1
            stats.deleted_files += len(sample.files)
            stats.deleted_bytes += sum(f.stat().st_size for f in sample.files)
            continue

        verified = []
        for f in sample.files:
            dest = dest_for(target, sample, f)
            if copier.verified_copy(f, dest) is None:
                # The SSD copy is missing or damaged. Copy again with full
                # checksums (a damaged file is kept and the good copy is
                # written next to it), then verify once more.
                repair = copier.archive_file(f, dest, quick=False)
                logger.warning("[%s] SSD copy of %s failed verification - re-copied (%s)",
                               sample.source, f.relative_to(sample.path), repair.status)
            if copier.verified_copy(f, dest) is None:
                break
            verified.append(f)

        if len(verified) != len(sample.files):
            stats.kept_unverified += 1
            logger.warning("[%s] %s not fully verified on the SSD - kept on device",
                           sample.source, sample.name)
            continue

        # The history must say where the run went before it leaves the device.
        if not _record_copy(cfg, target, sample):
            stats.kept_unrecorded += 1
            logger.warning("[%s] %s verified on the SSD but could not be recorded in the "
                           "database - kept on device", sample.source, sample.name)
            continue

        size = sum(f.stat().st_size for f in sample.files)
        removed_all = True
        for f in sample.files:
            try:
                f.unlink()
                stats.deleted_files += 1
            except OSError as e:
                removed_all = False
                stats.errors.append(f"delete {f}: {e}")
                logger.error("Verified on SSD but could not delete from device: %s (%s)", f, e)
        stats.deleted_samples += 1
        stats.deleted_bytes += size
        _remove_empty_dirs(sample.path, cfg.data_root / sample.source)
        if removed_all and sample.source == TRACKED_SOURCE:
            from app.services import dc_history

            dc_history.record_ssd_removed(_rel_path(cfg, sample))
        logger.info("[%s] removed from device (verified on SSD, %s): %s",
                    sample.source, _fmt(size), sample.name)


def run_once(cfg: ArchiveConfig, dry_run: bool = False) -> Dict:
    """One archive run. Returns its summary; summary["result"] is "ok",
    "errors", "ssd-not-connected", "ssd-full" or "deferred"."""
    started = time.time()
    target = find_target(cfg)
    if target is None:
        return {"result": "ssd-not-connected"}

    logger.info("SSD archive run started on %s%s", target,
                " (dry run - nothing will be written or deleted)" if dry_run else "")
    busy = _busy
    in_use = _protected_dirs()

    now = time.time()
    samples: List[Sample] = []
    for source in SOURCES:
        found = discover(source, cfg.data_root, now, cfg.min_file_age_seconds)
        logger.info("[%s] %d sample(s), %d file(s) ready", source.name, len(found),
                    sum(len(s.files) for s in found))
        samples.extend(found)

    needed = 0
    for s in samples:
        for f in s.files:
            d = dest_for(target, s, f)
            if not (d.exists() and copier.looks_identical(f, d)):
                needed += f.stat().st_size
    free = shutil.disk_usage(target).free
    logger.info("To copy: %s | SSD free: %s (reserve %s)",
                _fmt(needed), _fmt(free), _fmt(cfg.ssd_reserve_bytes))

    stats = Stats()
    result = "ok"
    if needed + cfg.ssd_reserve_bytes > free:
        logger.error("Not enough space on the archive SSD - nothing copied, nothing removed")
        stats.errors.append("ssd full")
        result = "ssd-full"
    else:
        try:
            copy_phase(samples, target, cfg, stats, dry_run, busy)
            if _clock_is_sane(samples):
                cleanup_phase(samples, target, cfg, stats, dry_run, in_use, busy)
        except Deferred as e:
            logger.info("SSD archive run stopped: %s. Continued at the next check.", e)
            result = "deferred"
        except OSError as e:
            logger.error("SSD archive copy stopped: %s - device clean-up skipped this run", e)
            stats.errors.append(str(e))
        if result == "ok" and (stats.failed or stats.errors or stats.kept_unrecorded):
            result = "errors"

    logger.info(
        "SSD archive: copied %d file(s) (%s, %d conflict-named), already on SSD %d, "
        "failed %d | device: removed %d sample(s) / %d file(s) / %s, kept unverified %d, "
        "kept unrecorded %d, kept in use %d | %.1fs",
        stats.copied, _fmt(stats.copied_bytes), stats.conflicts, stats.present,
        stats.failed, stats.deleted_samples, stats.deleted_files,
        _fmt(stats.deleted_bytes), stats.kept_unverified, stats.kept_unrecorded,
        stats.kept_in_use, time.time() - started,
    )
    summary = {k: v for k, v in stats.__dict__.items() if k != "errors"}
    summary.update(result=result, target=str(target), errors=stats.errors[:50])
    return summary


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def load_state() -> Dict:
    from app.core.database import SessionLocal
    from app.models.schema import SsdArchiveState

    db = SessionLocal()
    try:
        row = db.query(SsdArchiveState).filter(SsdArchiveState.id == 1).first()
        if row is None:
            return {}
        return {
            "last_attempt_at": row.last_attempt_at,
            "last_success_date": row.last_success_date,
            "target": row.target,
            "last_result": row.last_result,
        }
    finally:
        db.close()


def _save_state(**fields) -> None:
    from app.core.database import SessionLocal
    from app.models.schema import SsdArchiveState

    db = SessionLocal()
    try:
        row = db.query(SsdArchiveState).filter(SsdArchiveState.id == 1).first()
        if row is None:
            row = SsdArchiveState(id=1)
            db.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def _lower_priority() -> None:
    """Hashing and copying gigabytes should not take CPU from the camera and
    inference. Applies to this worker's own thread only."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError) as exc:
        logger.debug("Could not lower the SSD archive thread's priority: %s", exc)


class SsdArchiveTask:
    def __init__(self):
        self.is_running = False
        # Its own thread, so lowering its priority does not slow anything else.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ssd-archive")

    async def start(self):
        from app.core.config import settings

        self.is_running = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, _lower_priority)
        logger.info(
            "SSD archive worker started: checked every %ss, device keeps %s day(s)",
            settings.SSD_ARCHIVE_CHECK_INTERVAL_SECONDS,
            settings.SSD_ARCHIVE_DEVICE_RETENTION_DAYS,
        )
        try:
            while self.is_running:
                try:
                    await loop.run_in_executor(self._executor, self.tick)
                except Exception as exc:
                    logger.error("SSD archive check failed: %s", exc)
                await asyncio.sleep(settings.SSD_ARCHIVE_CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("SSD archive worker stopped.")
            raise

    def stop(self):
        self.is_running = False
        self._executor.shutdown(wait=False)

    def tick(self, force: bool = False) -> Optional[Dict]:
        """Run if today's run has not succeeded yet and the machine is idle.
        Returns the run's summary, or None if not run."""
        today = date.today().isoformat()
        if not force and load_state().get("last_success_date") == today:
            return None
        reason = _busy()
        if reason:
            logger.info("SSD archive put off: %s. Will check again.", reason)
            return None
        if not DEVICE_FILES_LOCK.acquire(blocking=False):
            logger.info("SSD archive put off: the S3 upload is running. Will check again.")
            return None
        try:
            summary = run_once(ArchiveConfig.from_settings())
        finally:
            DEVICE_FILES_LOCK.release()
        if summary["result"] == "ssd-not-connected":
            logger.debug("SSD archive: no archive SSD connected")
            return summary
        fields = {"last_attempt_at": datetime.now(), "target": summary.get("target"),
                  "last_result": summary}
        if summary["result"] == "ok":
            fields["last_success_date"] = today
        _save_state(**fields)
        return summary
