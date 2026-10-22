"""The archiver's settings, taken from app.core.config (SSD_ARCHIVE_*)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

GIB = 1024 ** 3


@dataclass(frozen=True)
class ArchiveConfig:
    # Holds Data_Collection/ and output_frame/ (OUTPUT_DIR)
    data_root: Path
    device_retention_days: int
    min_file_age_seconds: int
    ssd_reserve_bytes: int
    marker_name: str
    mount_roots: Tuple[Path, ...]
    # Refuse an archive target on the same disk as the data. Always on in
    # the backend; tests turn it off because temp dirs share one disk.
    require_separate_disk: bool = True

    @classmethod
    def from_settings(cls) -> "ArchiveConfig":
        from app.core.config import settings

        return cls(
            data_root=Path(settings.OUTPUT_DIR),
            device_retention_days=max(1, settings.SSD_ARCHIVE_DEVICE_RETENTION_DAYS),
            min_file_age_seconds=max(0, settings.SSD_ARCHIVE_MIN_FILE_AGE_SECONDS),
            ssd_reserve_bytes=int(max(0.0, settings.SSD_ARCHIVE_RESERVE_GB) * GIB),
            marker_name=settings.SSD_ARCHIVE_MARKER,
            mount_roots=tuple(Path(p) for p in settings.SSD_ARCHIVE_MOUNT_ROOTS.split(":") if p),
        )
