"""The archiver's settings, taken from app.core.config (SSD_ARCHIVE_*)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

GIB = 1024 ** 3


@dataclass(frozen=True)
class ArchiveConfig:
    # Holds the source folders (OUTPUT_DIR)
    data_root: Path
    device_retention_days: int          # 0 = as soon as verified on the SSD
    min_file_age_seconds: int
    ssd_reserve_bytes: int
    marker_name: str
    mount_roots: Tuple[Path, ...]
    # Source folders copied: Data_Collection, output_frame, and output when
    # SSD_ARCHIVE_INCLUDE_CROPS is on
    sources: Tuple[str, ...] = ("Data_Collection", "output_frame", "output")
    # Remove from the device only once also confirmed in S3
    require_cloud: bool = False
    # Delete from the SSD after this many days (0 = never), and only if in S3
    ssd_retention_days: int = 30
    ssd_delete_require_cloud: bool = False
    # Low-disk safety net threshold, percent free; 0 = off
    low_disk_free_percent: float = 10.0
    # Refuse an archive target on the same disk as the data. Always on in
    # the backend; tests turn it off because temp dirs share one disk.
    require_separate_disk: bool = True

    @classmethod
    def from_settings(cls) -> "ArchiveConfig":
        from app.core.config import settings

        return cls(
            data_root=Path(settings.OUTPUT_DIR),
            device_retention_days=max(0, settings.SSD_ARCHIVE_DEVICE_RETENTION_DAYS),
            min_file_age_seconds=max(0, settings.SSD_ARCHIVE_MIN_FILE_AGE_SECONDS),
            ssd_reserve_bytes=int(max(0.0, settings.SSD_ARCHIVE_RESERVE_GB) * GIB),
            marker_name=settings.SSD_ARCHIVE_MARKER,
            mount_roots=tuple(Path(p) for p in settings.SSD_ARCHIVE_MOUNT_ROOTS.split(":") if p),
            sources=("Data_Collection", "output_frame")
            + (("output",) if settings.SSD_ARCHIVE_INCLUDE_CROPS else ()),
            require_cloud=settings.SSD_ARCHIVE_REQUIRE_CLOUD,
            ssd_retention_days=max(0, settings.SSD_ARCHIVE_SSD_RETENTION_DAYS),
            ssd_delete_require_cloud=settings.SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD,
            low_disk_free_percent=max(0.0, settings.DEVICE_LOW_DISK_FREE_PERCENT),
        )
