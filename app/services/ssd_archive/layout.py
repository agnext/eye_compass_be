"""Where samples live on the device and how their date is read.

Expected layout, per source folder:

    <data_root>/<source>/<commodity>/<variety>/<sample_id>/...files

A sample folder found one or two levels too high (e.g. output_frame/_2026...
written with an empty commodity/variety) is still archived, under
_unsorted/_unsorted, rather than being skipped forever.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

UNSORTED = "_unsorted"

# Never archived: a partial copy in progress, editor/OS junk
_IGNORED_SUFFIXES = (".part", ".tmp", ".swp")
_IGNORED_NAMES = (".DS_Store", "Thumbs.db")


def time_from_epoch_prefix(name: str) -> Optional[datetime]:
    """'1789387342_testentry' -> 2026-09-14 17:32:22 (10-digit epoch first)."""
    head = name.split("_", 1)[0]
    if not (head.isdigit() and len(head) == 10):
        return None
    try:
        return datetime.fromtimestamp(int(head))
    except (OverflowError, OSError, ValueError):
        return None


def time_from_datetime_suffix(name: str) -> Optional[datetime]:
    """'G11791445078_20261008131152' -> 2026-10-08 13:11:52 (YYYYMMDDHHMMSS last)."""
    tail = name.rsplit("_", 1)[-1]
    if not (tail.isdigit() and len(tail) == 14):
        return None
    try:
        return datetime.strptime(tail, "%Y%m%d%H%M%S")
    except ValueError:
        return None


@dataclass(frozen=True)
class Source:
    name: str
    parse_time: Callable[[str], Optional[datetime]]


SOURCES = (
    Source("Data_Collection", time_from_epoch_prefix),
    Source("output_frame", time_from_datetime_suffix),
)


@dataclass
class Sample:
    source: str
    commodity: str
    variety: str
    path: Path
    taken_at: datetime
    # "name" when the date came from the folder name, "mtime" when the name
    # had none and the oldest file's modification time was used instead
    date_source: str
    files: List[Path] = field(default_factory=list)
    # Files skipped this run because they changed too recently
    too_new: int = 0
    newest_mtime: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name


def _is_ignored(name: str) -> bool:
    return name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES)


def _collect_files(folder: Path):
    files = []
    for dirpath, _dirnames, filenames in os.walk(folder):
        for fname in filenames:
            if not _is_ignored(fname):
                files.append(Path(dirpath) / fname)
    files.sort()
    return files


def _has_direct_files(folder: Path) -> bool:
    try:
        with os.scandir(folder) as entries:
            return any(entry.is_file() for entry in entries)
    except OSError:
        return False


def _build_sample(source: Source, root: Path, folder: Path, now: float,
                  min_age: int) -> Optional[Sample]:
    parts = folder.relative_to(root).parts
    if len(parts) == 3:
        commodity, variety = parts[0], parts[1]
    elif len(parts) == 2:
        commodity, variety = parts[0], UNSORTED
    else:
        commodity, variety = UNSORTED, UNSORTED

    all_files = _collect_files(folder)
    ready, too_new, newest, oldest = [], 0, 0.0, None
    for f in all_files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue  # vanished while scanning
        newest = max(newest, mtime)
        oldest = mtime if oldest is None else min(oldest, mtime)
        if now - mtime >= min_age:
            ready.append(f)
        else:
            too_new += 1

    taken_at = source.parse_time(folder.name)
    date_source = "name"
    if taken_at is None:
        if oldest is None:
            return None  # empty folder with no date: nothing to do
        taken_at = datetime.fromtimestamp(oldest)
        date_source = "mtime"

    return Sample(
        source=source.name,
        commodity=commodity,
        variety=variety,
        path=folder,
        taken_at=taken_at,
        date_source=date_source,
        files=ready,
        too_new=too_new,
        newest_mtime=newest,
    )


def discover(source: Source, data_root: Path, now: float, min_age: int) -> List[Sample]:
    """Return every sample under <data_root>/<source.name>."""
    root = data_root / source.name
    if not root.is_dir():
        log.warning("[%s] folder not found, skipped: %s", source.name, root)
        return []

    samples: List[Sample] = []
    stray_files = 0
    misplaced = 0

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        dirnames.sort()

        if depth == 0:
            stray_files += sum(1 for f in filenames if not _is_ignored(f))
            continue

        is_sample = depth == 3 or (
            depth in (1, 2)
            and source.parse_time(here.name) is not None
            and _has_direct_files(here)
        )

        if is_sample:
            dirnames[:] = []  # everything below belongs to this sample
            sample = _build_sample(source, root, here, now, min_age)
            if sample is not None:
                if depth != 3:
                    misplaced += 1
                    log.debug(
                        "[%s] sample at unexpected depth, archived under %s/%s: %s",
                        source.name, sample.commodity, sample.variety, here,
                    )
                if sample.date_source == "mtime":
                    log.info(
                        "[%s] no date in name %r, using file time %s",
                        source.name, here.name, sample.taken_at.date(),
                    )
                samples.append(sample)
        else:
            stray_files += sum(1 for f in filenames if not _is_ignored(f))

    if misplaced:
        log.warning(
            "[%s] %d sample(s) are missing their commodity/variety folder; archived "
            "under %s", source.name, misplaced, UNSORTED,
        )
    if stray_files:
        log.warning(
            "[%s] %d file(s) outside any sample folder were left in place",
            source.name, stray_files,
        )
    return samples
