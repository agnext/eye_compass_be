"""The archive run, the low-disk safety net, and the background task.

A run
    1. Find the archive SSD; if it is not connected, try again next check.
    2. Find the samples on the device (Data_Collection/, output_frame/, and
       output/ when SSD_ARCHIVE_INCLUDE_CROPS is on).
    3. SSD clean-up: samples whose date (the SSD's date folder) is more than
       SSD_ARCHIVE_SSD_RETENTION_DAYS ago are deleted from the SSD; with
       SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD, only those confirmed in S3.
    4. Check the SSD has room for everything not on it yet.
    5. Copy and verify every file. Device files are not touched here. A
       sample whose files are all on the SSD is recorded in folder_copies.
       Samples in use are not copied, nor ones already older than the SSD
       keeps samples (they stay on the device).
    6. Device clean-up: once SSD_ARCHIVE_DEVICE_RETENTION_DAYS have passed
       since a sample was taken (0: at once), it is removed from the device
       only if it is not in use, EVERY file of it is re-hashed now and matches
       its SSD copy, its SSD copy is recorded, and, with
       SSD_ARCHIVE_REQUIRE_CLOUD, it is also confirmed in S3 as it is now.
       Otherwise the whole sample stays.
    7. Record the result; the day counts as done only if nothing failed.

Low disk (DEVICE_LOW_DISK_FREE_PERCENT), checked every hour whether or not
the SSD is connected: the oldest samples with a confirmed copy on the SSD or
in S3 are removed early until there is room again. See free_space_if_low.

Why inside the backend: the records are written in the same process that
copies and deletes, so they cannot miss an update. And the backend knows
exactly what is in use: no run starts, and a run in progress stops, while a
batch scan or data collection is running, and the folders of the active
batch, held batches and the current collection run are never removed. The S3
uploader and this archiver never change device files at the same time
(folder_copies.device_files_lock).
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

DATE_FOLDER = "%Y-%m-%d"
# The low-disk safety net stops once free space is this far above its threshold
LOW_DISK_MARGIN_PERCENT = 5.0


class Deferred(Exception):
    """A scan or data collection started mid-run, or the backend is stopping.
    Stop cleanly between files, retry later."""


# Set when the backend shuts down, so a run in progress stops after the file it
# is on. Without it the run carries on to the end, and the backend's exit (which
# waits for this worker's thread) hangs until systemd kills it.
_stopping = threading.Event()


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
    kept_for_cloud: int = 0
    kept_past_ssd_retention: int = 0
    ssd_deleted_samples: int = 0
    ssd_deleted_bytes: int = 0
    ssd_kept_for_cloud: int = 0
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
    """The folder under its tree: <commodity>/<variety>/<folder>."""
    return sample.path.relative_to(cfg.data_root / sample.source).as_posix()


def _record_copy(cfg: ArchiveConfig, target: Path, sample: Sample) -> bool:
    """Record that every file of the sample is on the SSD. True once committed."""
    from app.services import dc_history, folder_copies

    rel = _rel_path(cfg, sample)
    if sample.source == dc_history.SUBDIR and not dc_history.ensure_row(rel):
        return False
    return folder_copies.record_ssd(
        sample.source, rel, target.name, dest_dir(target, sample).relative_to(target).as_posix())


def _in_cloud(cfg: ArchiveConfig, sample: Sample) -> bool:
    from app.services import folder_copies

    return folder_copies.in_cloud(
        folder_copies.get(sample.source, _rel_path(cfg, sample)), sample.newest_mtime)


def _busy() -> Optional[str]:
    from app.services.s3_worker import S3UploaderTask

    return S3UploaderTask._busy()


def _why_stop() -> Optional[str]:
    """Checked between files: the backend stopping, or a scan or collection."""
    if _stopping.is_set():
        return "the backend is shutting down"
    return _busy()


def _protected_dirs() -> Set[str]:
    """Folders in use or that may still be written to. Raises if the database
    cannot say which batches are held: unknown is not "none"."""
    from app.services.s3_worker import S3UploaderTask

    return S3UploaderTask._protected_dirs()


def _in_use(sample: Sample, protected: Set[str]) -> bool:
    real = os.path.realpath(sample.path)
    return any(real == p or real.startswith(p + os.sep) or p.startswith(real + os.sep)
               for p in protected)


def _discover(cfg: ArchiveConfig) -> List[Sample]:
    now = time.time()
    samples: List[Sample] = []
    for source in SOURCES:
        if source.name not in cfg.sources:
            continue
        found = discover(source, cfg.data_root, now, cfg.min_file_age_seconds)
        logger.debug("[%s] %d sample(s), %d file(s) ready", source.name, len(found),
                     sum(len(s.files) for s in found))
        samples.extend(found)
    return samples


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


def _remove_sample(cfg: ArchiveConfig, sample: Sample, stats: Stats, by: str) -> int:
    """Delete the sample's files from the device and record it. Returns bytes."""
    from app.services import folder_copies

    size = sum(f.stat().st_size for f in sample.files)
    removed_all = True
    for f in sample.files:
        try:
            f.unlink()
            stats.deleted_files += 1
        except OSError as e:
            removed_all = False
            stats.errors.append(f"delete {f}: {e}")
            logger.error("Could not delete from device: %s (%s)", f, e)
    stats.deleted_samples += 1
    stats.deleted_bytes += size
    _remove_empty_dirs(sample.path, cfg.data_root / sample.source)
    if removed_all:
        folder_copies.record_removed(sample.source, _rel_path(cfg, sample), by)
    return size


def _all_verified(target: Path, sample: Sample, repair: bool) -> bool:
    """Every file of the sample re-hashed now and equal to its SSD copy.
    Raises Deferred if a scan starts or the backend stops part way."""
    for f in sample.files:
        reason = _why_stop()
        if reason:
            raise Deferred(reason)
        dest = dest_for(target, sample, f)
        if copier.verified_copy(f, dest) is None:
            if not repair:
                return False
            # The SSD copy is missing or damaged. Copy again with full
            # checksums (a damaged file is kept and the good copy is written
            # next to it), then verify once more.
            outcome = copier.archive_file(f, dest, quick=False)
            logger.warning("[%s] SSD copy of %s failed verification - re-copied (%s)",
                           sample.source, f.relative_to(sample.path), outcome.status)
            if copier.verified_copy(f, dest) is None:
                return False
    return True


def _past_ssd_retention(cfg: ArchiveConfig, sample: Sample) -> bool:
    """Already older than the SSD keeps samples. Copied, it would be deleted
    from the SSD at the next run (and copied again the day after), so it is
    neither copied nor removed from the device: it stays there."""
    if cfg.ssd_retention_days <= 0:
        return False
    return sample.taken_at.date() < date.today() - timedelta(days=cfg.ssd_retention_days)


def copy_phase(samples: List[Sample], target: Path, cfg: ArchiveConfig, stats: Stats,
               dry_run: bool, busy: Callable[[], Optional[str]], protected: Set[str]) -> None:
    for sample in samples:
        if not sample.files:
            continue
        # A batch still open or held can have crops relabelled (renamed) or
        # replaced; copied now, the SSD would keep the old names next to the
        # new ones. It is copied once it is closed.
        if _in_use(sample, protected):
            continue
        if _past_ssd_retention(cfg, sample):
            stats.kept_past_ssd_retention += 1
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
        # Only a sample whose every file is on the SSD is recorded; one still
        # being written to (too_new) is recorded on a later day.
        if not dry_run and not failed and not sample.too_new:
            _record_copy(cfg, target, sample)


def cleanup_phase(samples: List[Sample], target: Path, cfg: ArchiveConfig, stats: Stats,
                  dry_run: bool, protected: Set[str],
                  busy: Callable[[], Optional[str]]) -> None:
    # Exact times, not dates: with 0 days a sample taken today goes too.
    cutoff_time = time.time() - cfg.device_retention_days * 86400

    for sample in samples:
        if sample.taken_at.timestamp() > cutoff_time:
            continue
        # Still being added to (files changed within the retention period, or
        # within the last SSD_ARCHIVE_MIN_FILE_AGE_SECONDS): keep
        if sample.too_new or sample.newest_mtime > cutoff_time:
            logger.info("[%s] %s has recent files - kept on device", sample.source, sample.name)
            continue
        if not sample.files or _past_ssd_retention(cfg, sample):
            continue
        if _in_use(sample, protected):
            stats.kept_in_use += 1
            logger.info("[%s] %s is in use (open or held batch) - kept on device",
                        sample.source, sample.name)
            continue
        if cfg.require_cloud and not _in_cloud(cfg, sample):
            stats.kept_for_cloud += 1
            logger.debug("[%s] %s not confirmed in S3 yet - kept on device",
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

        if not _all_verified(target, sample, repair=True):
            stats.kept_unverified += 1
            logger.warning("[%s] %s not fully verified on the SSD - kept on device",
                           sample.source, sample.name)
            continue

        # The record must say where the sample went before it leaves the device.
        if not _record_copy(cfg, target, sample):
            stats.kept_unrecorded += 1
            logger.warning("[%s] %s verified on the SSD but could not be recorded in the "
                           "database - kept on device", sample.source, sample.name)
            continue

        size = _remove_sample(cfg, sample, stats, "ssd_archive")
        logger.info("[%s] removed from device (verified on SSD, %s): %s",
                    sample.source, _fmt(size), sample.name)


def _date_folder(name: str) -> Optional[date]:
    """'2026-10-01' -> date; None for anything else (not ours)."""
    try:
        d = datetime.strptime(name, DATE_FOLDER).date()
    except ValueError:
        return None
    return d if d.strftime(DATE_FOLDER) == name else None


def ssd_cleanup_phase(target: Path, cfg: ArchiveConfig, stats: Stats, dry_run: bool,
                      busy: Callable[[], Optional[str]]) -> None:
    """Delete from the SSD the samples dated more than
    SSD_ARCHIVE_SSD_RETENTION_DAYS ago: <SSD>/<date>/<source>/<c>/<v>/<sample>.

    Only folders in the archiver's own layout are touched: a date-named folder
    at the SSD root, one of the source folders in it, and sample folders three
    levels below that. Anything else on the drive is left alone.
    """
    from app.services import folder_copies

    if cfg.ssd_retention_days <= 0:
        return
    cutoff = date.today() - timedelta(days=cfg.ssd_retention_days)
    root = os.path.realpath(str(target))
    try:
        days = sorted(p for p in target.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError as e:
        stats.errors.append(f"read {target}: {e}")
        return

    for day in days:
        when = _date_folder(day.name)
        if when is None or when >= cutoff:
            continue
        for source in SOURCES:
            base = day / source.name
            if not base.is_dir() or base.is_symlink():
                continue
            folders = [p for p in base.glob("*/*/*") if p.is_dir() and not p.is_symlink()]
            copies = folder_copies.by_ssd_path(p.relative_to(target).as_posix() for p in folders)
            for folder in folders:
                reason = busy()
                if reason:
                    raise Deferred(reason)
                if not os.path.realpath(folder).startswith(root + os.sep):
                    continue
                copy = copies.get(folder.relative_to(target).as_posix())
                if cfg.ssd_delete_require_cloud and not folder_copies.in_cloud(copy):
                    stats.ssd_kept_for_cloud += 1
                    continue
                size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
                if dry_run:
                    stats.ssd_deleted_samples += 1
                    stats.ssd_deleted_bytes += size
                    continue
                try:
                    shutil.rmtree(folder)
                except OSError as e:
                    stats.errors.append(f"delete from SSD {folder}: {e}")
                    logger.error("Could not delete from the SSD: %s (%s)", folder, e)
                    continue
                stats.ssd_deleted_samples += 1
                stats.ssd_deleted_bytes += size
                if copy is not None:
                    folder_copies.record_ssd_removed(copy.tree, copy.rel_path)
                logger.info("[%s] deleted from the SSD (dated %s, older than %d days, %s): %s",
                            source.name, day.name, cfg.ssd_retention_days, _fmt(size),
                            folder.name)
            if not dry_run:
                # Its now-empty commodity/variety folders, then the date
                # folder if nothing else is in it. Nothing else is tidied.
                _remove_empty_dirs(base, target)


def run_once(cfg: ArchiveConfig, dry_run: bool = False) -> Dict:
    """One archive run. Returns its summary; summary["result"] is "ok",
    "errors", "ssd-not-connected", "ssd-full" or "deferred"."""
    started = time.time()
    target = find_target(cfg)
    if target is None:
        return {"result": "ssd-not-connected"}

    logger.info("SSD archive run started on %s%s", target,
                " (dry run - nothing will be written or deleted)" if dry_run else "")
    busy = _why_stop
    in_use = _protected_dirs()
    stats = Stats()
    # First, as it frees room on the SSD for today's copy.
    try:
        ssd_cleanup_phase(target, cfg, stats, dry_run, busy)
    except Deferred as e:
        logger.info("SSD archive run put off: %s. Continued at the next check.", e)
        return {"result": "deferred", "target": str(target)}
    samples = _discover(cfg)

    needed = 0
    for s in samples:
        if _why_stop():
            logger.info("SSD archive run put off: %s. Continued at the next check.", _why_stop())
            return {"result": "deferred", "target": str(target)}
        if _in_use(s, in_use) or _past_ssd_retention(cfg, s):
            continue
        for f in s.files:
            d = dest_for(target, s, f)
            if not (d.exists() and copier.looks_identical(f, d)):
                needed += f.stat().st_size
    free = shutil.disk_usage(target).free
    logger.info("%d sample(s) | to copy: %s | SSD free: %s (reserve %s)", len(samples),
                _fmt(needed), _fmt(free), _fmt(cfg.ssd_reserve_bytes))

    result = "ok"
    if needed + cfg.ssd_reserve_bytes > free:
        logger.error("Not enough space on the archive SSD - nothing copied, nothing removed")
        stats.errors.append("ssd full")
        result = "ssd-full"
    else:
        try:
            copy_phase(samples, target, cfg, stats, dry_run, busy, in_use)
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
        "kept unrecorded %d, kept in use %d, waiting for S3 %d, older than the SSD keeps %d | "
        "SSD: deleted %d sample(s) / %s, kept for S3 %d | %.1fs",
        stats.copied, _fmt(stats.copied_bytes), stats.conflicts, stats.present,
        stats.failed, stats.deleted_samples, stats.deleted_files,
        _fmt(stats.deleted_bytes), stats.kept_unverified, stats.kept_unrecorded,
        stats.kept_in_use, stats.kept_for_cloud, stats.kept_past_ssd_retention,
        stats.ssd_deleted_samples, _fmt(stats.ssd_deleted_bytes), stats.ssd_kept_for_cloud,
        time.time() - started,
    )
    if stats.kept_past_ssd_retention:
        logger.warning(
            "%d sample(s) on the device are already older than the SSD keeps samples "
            "(%d days): not copied, left on the device", stats.kept_past_ssd_retention,
            cfg.ssd_retention_days)
    summary = {k: v for k, v in stats.__dict__.items() if k != "errors"}
    summary.update(result=result, target=str(target), errors=stats.errors[:50])
    return summary


# ---------------------------------------------------------------------------
# Low disk
# ---------------------------------------------------------------------------

def _free_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return 100.0 * usage.free / usage.total if usage.total else 100.0


def free_space_if_low(cfg: ArchiveConfig) -> Optional[Dict]:
    """While the device is below DEVICE_LOW_DISK_FREE_PERCENT free, remove the
    oldest samples that have a confirmed copy, until it is
    LOW_DISK_MARGIN_PERCENT above it. Returns a summary, or None if the disk
    is not low.

    The normal clean-up cannot always run: the SSD may be unplugged for weeks,
    S3 may be offline, retention may not have been reached. Without this the
    disk fills and collection stops.

    A sample counts as copied if it is on the connected SSD (re-verified file
    by file now), or, with the SSD unplugged, recorded on it since it last
    changed, or confirmed in S3 since it last changed (not for output/: the
    batch crops History shows need an SSD copy). Retention days and
    SSD_ARCHIVE_REQUIRE_CLOUD are not applied here; the in-use and "files
    changed in the last few minutes" rules are. A sample with no copy
    anywhere is never removed.
    """
    from app.services import folder_copies

    threshold = cfg.low_disk_free_percent
    if threshold <= 0 or not cfg.data_root.is_dir():
        return None
    if _free_percent(cfg.data_root) >= threshold:
        return None

    goal = threshold + LOW_DISK_MARGIN_PERCENT
    logger.warning("Device low on space: %.1f%% free (safety net below %.0f%%) - removing the "
                   "oldest samples that have a copy on the SSD or in S3",
                   _free_percent(cfg.data_root), threshold)
    target = find_target(cfg, writable=False, quiet=True)
    in_use = _protected_dirs()
    every = ArchiveConfig(**{**cfg.__dict__, "sources": tuple(s.name for s in SOURCES)})
    samples = sorted((s for s in _discover(every) if s.files and not s.too_new),
                     key=lambda s: s.taken_at)
    stats = Stats()
    no_copy = 0
    for sample in samples:
        if _free_percent(cfg.data_root) >= goal:
            break
        if _in_use(sample, in_use):
            continue
        reason = _why_stop()
        if reason:
            logger.info("Low-disk clean-up stopped: %s. Continued at the next check.", reason)
            break
        copy = folder_copies.get(sample.source, _rel_path(cfg, sample))
        if target is not None and folder_copies.on_ssd(copy, sample.newest_mtime) \
                and _all_verified(target, sample, repair=False):
            where = "verified on SSD"
        elif target is None and folder_copies.on_ssd(copy, sample.newest_mtime):
            where = "recorded on SSD"
        elif sample.source != "output" and folder_copies.in_cloud(copy, sample.newest_mtime):
            # Not batch crops: History shows those from the device or the
            # SSD, never from S3, so a crop folder needs an SSD copy here.
            where = "in S3"
        else:
            no_copy += 1
            continue
        size = _remove_sample(cfg, sample, stats, "low_disk")
        logger.warning("[%s] low disk: removed from device (%s, %s): %s",
                       sample.source, where, _fmt(size), sample.name)

    free_now = _free_percent(cfg.data_root)
    if free_now < threshold:
        logger.error("Device still low on space (%.1f%% free): %d sample(s) have no copy on "
                     "the SSD or in S3 and are kept. Connect the archive SSD or the internet.",
                     free_now, no_copy)
    return {"removed_samples": stats.deleted_samples, "removed_bytes": stats.deleted_bytes,
            "kept_no_copy": no_copy, "free_percent": round(free_now, 1)}


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
        _stopping.clear()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, _lower_priority)
        if not settings.SSD_ARCHIVE_ENABLED:
            logger.info("SSD archive off; low-disk safety net on (below %s%% free), checked "
                        "every %ss", settings.DEVICE_LOW_DISK_FREE_PERCENT,
                        settings.SSD_ARCHIVE_CHECK_INTERVAL_SECONDS)
        else:
            logger.info(
                "SSD archive worker started: checked every %ss, device keeps %s day(s), SSD "
                "keeps %s day(s)%s, crops %s, S3 copy required %s, low-disk safety net below "
                "%s%% free",
                settings.SSD_ARCHIVE_CHECK_INTERVAL_SECONDS,
                settings.SSD_ARCHIVE_DEVICE_RETENTION_DAYS,
                settings.SSD_ARCHIVE_SSD_RETENTION_DAYS or "all",
                " (deleted only once in S3)" if settings.SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD else "",
                "included" if settings.SSD_ARCHIVE_INCLUDE_CROPS else "not included",
                "yes" if settings.SSD_ARCHIVE_REQUIRE_CLOUD else "no",
                settings.DEVICE_LOW_DISK_FREE_PERCENT,
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
        _stopping.set()
        self._executor.shutdown(wait=False)

    def tick(self, force: bool = False) -> Optional[Dict]:
        """The low-disk check, then (with SSD_ARCHIVE_ENABLED) today's run if
        it has not succeeded yet and the machine is idle. Returns the run's
        summary, or None if not run."""
        from app.core.config import settings
        from app.services.folder_copies import device_files_lock

        cfg = ArchiveConfig.from_settings()
        reason = _busy()
        if reason:
            logger.debug("SSD archive put off: %s.", reason)
            return None
        with device_files_lock() as held:
            if not held:
                logger.info("SSD archive put off: the S3 upload is running. Will check again.")
                return None
            # Only on a device whose settings remove data at all: with both
            # the SSD archive and S3 deletion off, nothing is ever removed.
            if settings.SSD_ARCHIVE_ENABLED or settings.S3_DELETE_AFTER_UPLOAD:
                free_space_if_low(cfg)
            if not settings.SSD_ARCHIVE_ENABLED:
                return None
            today = date.today().isoformat()
            if not force and load_state().get("last_success_date") == today:
                return None
            summary = run_once(cfg)
        if summary["result"] == "ssd-not-connected":
            logger.debug("SSD archive: no archive SSD connected")
            return summary
        fields = {"last_attempt_at": datetime.now(), "target": summary.get("target"),
                  "last_result": summary}
        if summary["result"] == "ok":
            fields["last_success_date"] = today
        _save_state(**fields)
        return summary
