"""Finding the archive SSD.

A drive is used only if it carries the marker file at its root, written once
by `python -m app.services.ssd_archive init <mountpoint>`. Picking "the first
USB drive" instead could write into a technician's pen drive or a second disk.
Each device has its own archive SSD.
"""

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .config import ArchiveConfig

log = logging.getLogger(__name__)


def _exists(check) -> bool:
    """Run a pathlib check, treating unreadable paths (e.g. /media/root) as absent."""
    try:
        return check()
    except OSError:
        return False


def _candidate_dirs(mount_roots) -> List[Path]:
    """Directories one or two levels under each mount root.

    Covers /mnt/<label>, /media/<label>, /media/<user>/<label> and
    /run/media/<user>/<label>.
    """
    found = []
    for root in mount_roots:
        try:
            level1 = [p for p in Path(root).iterdir() if _exists(p.is_dir)]
        except OSError:
            continue
        for p in level1:
            found.append(p)
            try:
                found.extend(q for q in p.iterdir() if _exists(q.is_dir))
            except OSError:
                pass
    return found


def _same_device(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def find_target(cfg: ArchiveConfig, writable: bool = True, quiet: bool = False) -> Optional[Path]:
    """The archive drive's mount point, or None if it is not usable.

    writable=False skips the write test, for reading archived runs back.
    """
    info = (lambda *a: None) if quiet else log.info
    error = log.debug if quiet else log.error
    marked = [d for d in _candidate_dirs(cfg.mount_roots)
              if _exists((d / cfg.marker_name).is_file)]

    if not marked:
        # Checked every hour while unplugged, so not worth a line each time
        log.debug("Archive SSD not connected (no drive with %s under %s)",
                  cfg.marker_name, ", ".join(map(str, cfg.mount_roots)))
        return None

    if len(marked) > 1:
        error("More than one archive SSD connected, refusing to guess: %s",
              ", ".join(map(str, marked)))
        return None

    target = marked[0]

    if cfg.require_separate_disk:
        if not os.path.ismount(target):
            error("%s carries the marker but is not a mount point - refusing", target)
            return None
        if _same_device(target, cfg.data_root):
            error("%s is on the same disk as the data - refusing", target)
            return None

    if writable:
        probe = target / ".archiver_write_test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as e:
            log.error("Archive SSD %s is not writable: %s", target, e)
            return None

    info("Archive SSD: %s", target)
    return target


# The history screens ask for every row they show; scanning the mount roots
# each time would be wasted work, and plugging the drive in shows up soon
# enough at this interval.
_READ_CACHE_SECONDS = 10.0
_read_cache: Tuple[float, Optional[Path]] = (0.0, None)
_read_lock = threading.Lock()


def connected_target(cfg: ArchiveConfig) -> Optional[Path]:
    """The archive SSD for reading, cached briefly. None if not connected."""
    global _read_cache
    with _read_lock:
        at, target = _read_cache
        if time.monotonic() - at < _READ_CACHE_SECONDS:
            return target
        target = find_target(cfg, writable=False, quiet=True)
        _read_cache = (time.monotonic(), target)
        return target


def init_target(cfg: ArchiveConfig, mountpoint: Path) -> int:
    """Mark a mounted drive as the archive SSD. Returns an exit code."""
    mountpoint = mountpoint.resolve()
    if not mountpoint.is_dir():
        log.error("Not a folder: %s", mountpoint)
        return 2
    if cfg.require_separate_disk:
        if not os.path.ismount(mountpoint):
            log.error("%s is not a mount point - give the drive's root folder", mountpoint)
            return 2
        if _same_device(mountpoint, cfg.data_root):
            log.error("%s is on the same disk as the data - refusing", mountpoint)
            return 2

    marker = mountpoint / cfg.marker_name
    if marker.exists():
        log.info("Already marked as the archive SSD: %s", marker)
        return 0

    marker.write_text(json.dumps({
        "purpose": "EyeCompass data archive",
        "created": datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
    }, indent=2) + "\n")
    log.info("Marked %s as the archive SSD", mountpoint)
    return 0
