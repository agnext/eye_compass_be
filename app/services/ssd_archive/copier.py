"""Copying one file to the SSD, and proving a copy matches.

Nothing on the SSD is ever overwritten. If the SSD already holds a DIFFERENT
file under the same name (for example a resumed held batch restarted its
frame numbering after the first r_frame_0.jpg was archived), the device file
is stored next to it as "<stem>.conflict-<hash8><ext>". Both versions are
kept and nothing is lost.
"""

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
# exFAT stores mtimes to 10 ms, FAT to 2 s
_MTIME_TOLERANCE = 2.0
# Characters exFAT / NTFS reject in names
_INVALID = '<>:"\\|?*'


def safe_component(name: str) -> str:
    """Make one path component valid on exFAT/NTFS without changing normal names."""
    cleaned = "".join("_" if (c in _INVALID or ord(c) < 32) else c for c in name)
    cleaned = cleaned.rstrip(" .")
    return cleaned or "_"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def conflict_path(dest: Path, src_hash: str) -> Path:
    return dest.with_name(f"{dest.stem}.conflict-{src_hash[:8]}{dest.suffix}")


def looks_identical(src: Path, dest: Path) -> bool:
    """Cheap check used for the daily skip; full hashes are compared before deletion."""
    try:
        s, d = src.stat(), dest.stat()
    except OSError:
        return False
    return s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) <= _MTIME_TOLERANCE


def _fsync_dir(folder: Path) -> None:
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # not supported on every file system
    finally:
        os.close(fd)


def _write_verified(src: Path, dest: Path, src_hash: str) -> None:
    """Copy src to dest via a .part file; raise OSError unless the copy is exact."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        # copyfile, not copy2: exFAT/NTFS mounts reject chmod, which copy2 does
        shutil.copyfile(src, part)
        with open(part, "rb+") as f:
            os.fsync(f.fileno())
        if part.stat().st_size != src.stat().st_size:
            raise OSError("size differs from the original")
        if sha256_of(part) != src_hash:
            raise OSError("checksum differs from the original")
        try:
            st = src.stat()
            os.utime(part, (st.st_atime, st.st_mtime))
        except OSError:
            pass  # mtime is only used for the cheap skip check
        os.replace(part, dest)
        _fsync_dir(dest.parent)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


@dataclass
class CopyOutcome:
    # "present"  - an identical copy was already on the SSD
    # "copied"   - written and verified this run
    # "conflict" - written and verified under a .conflict-<hash> name
    # "failed"   - nothing usable on the SSD; the device file is untouched
    status: str
    dest: Path
    error: Optional[str] = None


def archive_file(src: Path, dest: Path, quick: bool = True) -> CopyOutcome:
    """Put an exact copy of src on the SSD at dest (or its conflict name).

    quick=True trusts an existing dest with the same size and mtime - used for
    the daily pass. quick=False always compares full checksums - used to
    repair a copy that failed verification.
    """
    try:
        if quick and dest.exists() and looks_identical(src, dest):
            return CopyOutcome("present", dest)

        src_hash = sha256_of(src)

        if dest.exists():
            if sha256_of(dest) == src_hash:
                return CopyOutcome("present", dest)
            alt = conflict_path(dest, src_hash)
            if alt.exists() and sha256_of(alt) == src_hash:
                return CopyOutcome("present", alt)
            log.warning("SSD already has a different %s - keeping both, saving as %s",
                        dest.name, alt.name)
            _write_verified(src, alt, src_hash)
            return CopyOutcome("conflict", alt)

        _write_verified(src, dest, src_hash)
        return CopyOutcome("copied", dest)

    except OSError as e:
        return CopyOutcome("failed", dest, str(e))


def verified_copy(src: Path, dest: Path) -> Optional[Path]:
    """Full-hash check used immediately before deleting src from the device.

    Returns the SSD path holding an exact copy of src, or None.
    """
    try:
        src_hash = sha256_of(src)
        for candidate in (dest, conflict_path(dest, src_hash)):
            if candidate.is_file() and candidate.stat().st_size == src.stat().st_size:
                if sha256_of(candidate) == src_hash:
                    return candidate
    except OSError as e:
        log.error("Could not verify %s: %s", src, e)
    return None
