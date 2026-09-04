"""
Central configuration.

Precedence (highest first):
    1. Real process environment  — e.g. Environment= lines in the systemd unit
    2. .env in the backend root
    3. The legacy config.INI, if it is still present on the device
    4. Hardcoded defaults, which match the legacy Jetson values

(1) beating (2) matters: the systemd unit pins USE_MOCK_CAMERA=false, and a stale
`USE_MOCK_CAMERA=true` left in a developer's .env must never be able to put the
real device into mock mode. This is why load_dotenv is called with override=False.
"""

import configparser
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_backend_root = Path(__file__).resolve().parent.parent.parent

# override=False → anything already exported (systemd, shell) wins over .env.
load_dotenv(_backend_root / ".env", override=False)


# --------------------------------------------------------------------------
# Legacy config.INI fallback
# --------------------------------------------------------------------------

_ini = configparser.RawConfigParser()
_ini_path = os.getenv("CONFIG_INI_PATH", "/home/nvidia/eye_compass/config.INI")
try:
    if os.path.exists(_ini_path):
        _ini.read(_ini_path)
        logger.info("Loaded legacy config fallback from %s", _ini_path)
except Exception as exc:  # a malformed INI must not stop the service
    logger.warning("Could not read %s: %s", _ini_path, exc)


def _ini_get(section: str, key: str, default=None):
    try:
        return _ini.get(section, key)
    except Exception:
        return default


def _env(name: str, *fallback_names: str, section: str = None, key: str = None, default=None):
    """Env var (trying several aliases), then config.INI, then the default."""
    for candidate in (name,) + fallback_names:
        value = os.getenv(candidate)
        if value not in (None, ""):
            return value
    if section and key:
        value = _ini_get(section, key)
        if value not in (None, ""):
            return value
    return default


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Settings:
    """Configuration for the whole backend. Instantiated once, at the bottom."""

    # ---------------- Runtime mode ----------------
    USE_MOCK_CAMERA: bool = _as_bool(os.getenv("USE_MOCK_CAMERA"), False)

    # ---------------- Paths ----------------
    CONFIG_INI_PATH: str = _ini_path
    EYE_COMPASS_SRC: str = _env("EYE_COMPASS_SRC", default="/home/nvidia/eye_compass")
    MODEL_DIR: str = _env("MODEL_DIR", default="/home/nvidia/eye_compass/models")
    MVS_SDK_PATH: str = _env(
        "MVS_SDK_PATH", default="/home/nvidia/MVS/Samples/aarch64/Python/MvImport"
    )
    # Camera calibration file the legacy app loads via MV_CC_FeatureLoad
    # (GrabImage.py:679). Without it the camera runs on factory white balance.
    CAMERA_FEATURE_FILE: str = _env(
        "CAMERA_FEATURE_FILE", default="/home/nvidia/eye_compass/FeatureFile_new.ini"
    )
    # Where scan crops / frames are written, and what the S3 worker uploads.
    OUTPUT_DIR: str = _env(
        "OUTPUT_DIR", section="_PATH_", key="parent", default=str(_backend_root / "output")
    )
    XAI_MODEL_PATH: str = _env(
        "XAI_MODEL_PATH", default="/home/nvidia/eye_compass/xai_models/v6_best.pt"
    )

    # ---------------- Camera ----------------
    CAMERA_INDEX: int = _as_int(_env("CAMERA_INDEX", section="CAMERA", key="camera_index"), 0)
    # Legacy hardcodes these at GrabImage.py:677-678, and they are what the
    # models were tuned against. The [CAMERA] runtime_* keys in config.INI are
    # NOT read by any legacy code path, so they are deliberately not used here.
    CAMERA_EXPOSURE_TIME: float = _as_float(os.getenv("CAMERA_EXPOSURE_TIME"), 600.0)
    CAMERA_GAIN: float = _as_float(os.getenv("CAMERA_GAIN"), 0.0)
    CAMERA_FRAME_QUEUE_SIZE: int = _as_int(os.getenv("CAMERA_FRAME_QUEUE_SIZE"), 32)
    # Legacy processed every 2nd frame (GrabImage.py:117).
    CAMERA_FRAME_DECIMATION: int = _as_int(os.getenv("CAMERA_FRAME_DECIMATION"), 2)

    # ---------------- Streaming ----------------
    STREAM_FPS: int = _as_int(os.getenv("STREAM_FPS"), 20)
    STREAM_JPEG_QUALITY: int = _as_int(os.getenv("STREAM_JPEG_QUALITY"), 70)
    STREAM_MAX_WIDTH: int = _as_int(os.getenv("STREAM_MAX_WIDTH"), 1280)

    # ---------------- Serial / conveyor ----------------
    SERIAL_PORT: str = _env("EYE_COMPASS_SERIAL_PORT", "SERIAL_PORT", default="")
    SERIAL_BAUD: int = _as_int(_env("EYE_COMPASS_SERIAL_BAUD", "SERIAL_BAUD"), 9600)
    SERIAL_MAX_RETRIES: int = _as_int(os.getenv("SERIAL_MAX_RETRIES"), 3)

    # ---------------- Database ----------------
    DATABASE_URL: str = _env(
        "DATABASE_URL",
        default="postgresql://postgres:password@localhost:5432/eye_compass",
    )

    # ---------------- Qualix ----------------
    QUALIX_RUN_ENV: str = _env("QUALIX_RUN_ENV", section="CONFIG_SETTINGS", key="run_env", default="prod")
    QUALIX_API_URL: str = _env(
        "QUALIX_API_URL", section="API_ENV", key="prod", default="https://assaying.qualix.ai/"
    )
    QUALIX_USERNAME: str = _env(
        "QUALIX_USERNAME", section="CONFIG_SETTINGS", key="username", default=""
    )
    QUALIX_PASSWORD: str = _env(
        "QUALIX_PASSWORD", section="CONFIG_SETTINGS", key="password", default=""
    )
    # Legacy appends this to the bare username before authenticating (main.py:598).
    QUALIX_USER_DOMAIN: str = _env("QUALIX_USER_DOMAIN", default="@agnext.in")

    OAUTH_URI_GET: str = _env("OAUTH_URI_GET", section="API_URI", key="oauth_uri_get", default="portal/oauth/authorize")
    OAUTH_URI_POST: str = _env("OAUTH_URI_POST", section="API_URI", key="oauth_uri_post", default="portal/login")
    CONFIG_URI: str = _env("CONFIG_URI", section="API_URI", key="config", default="portal/api/icompass/v1/config")
    ANALYSIS_POST_URI: str = _env(
        "ANALYSIS_POST_URI", section="API_URI", key="analysis_post_uri", default="portal/api/scan/v2/post-visio"
    )

    DEVICE_ID: str = _env("DEVICE_ID", section="CONFIG_SETTINGS", key="device_id", default="")
    LOCATION: str = _env("LOCATION", section="CONFIG_SETTINGS", key="location", default="")

    # ---------------- S3 ----------------
    # Accepts both the AWS_* names used in .env and the S3_*/COGNITO_* names the
    # code originally read, so neither spelling silently resolves to nothing.
    S3_REGION: str = _env("AWS_REGION", "S3_REGION", section="S3", key="region", default="us-east-2")
    S3_BUCKET: str = _env("AWS_S3_BUCKET", "S3_BUCKET", section="S3", key="bucket", default="agnext-cognito")
    S3_IDENTITY_POOL: str = _env(
        "AWS_IDENTITY_POOL_ID", "COGNITO_IDENTITY_POOL", section="S3", key="pool_id", default=""
    )
    S3_BUCKET_FOLDER: str = _env("S3_BUCKET_FOLDER", section="S3", key="bucket_folder", default="")
    S3_CLIENT: str = _env("S3_CLIENT", section="S3", key="client", default="")
    S3_UPLOAD_INTERVAL_SECONDS: int = _as_int(os.getenv("S3_UPLOAD_INTERVAL_SECONDS"), 60)
    S3_ENABLED: bool = _as_bool(os.getenv("S3_ENABLED"), True)

    # ---------------- Google Sheets ----------------
    SHEETS_ENABLED: bool = _as_bool(
        _env("SHEETS_ENABLED", section="GOOGLE_SHEETS", key="enabled"), False
    )
    SHEETS_SPREADSHEET_ID: str = _env(
        "SHEETS_SPREADSHEET_ID", section="GOOGLE_SHEETS", key="spreadsheet_id", default=""
    )
    SHEETS_CREDENTIALS_FILE: str = _env(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SHEETS_CREDENTIALS_FILE",
        section="GOOGLE_SHEETS",
        key="service_account_file",
        default="",
    )

    # ---------------- Background workers ----------------
    # Legacy retried unsynced records every 15 minutes (main.py:2828).
    SYNC_RETRY_INTERVAL_MINUTES: int = _as_int(os.getenv("SYNC_RETRY_INTERVAL_MINUTES"), 15)
    SYNC_WORKER_ENABLED: bool = _as_bool(os.getenv("SYNC_WORKER_ENABLED"), True)

    # ---------------- CORS ----------------
    # Comma-separated. Defaults to the kiosk + dev origins rather than "*",
    # because this service drives physical hardware.
    CORS_ORIGINS: str = _env(
        "CORS_ORIGINS",
        default="http://localhost:5143,http://localhost:5173,http://127.0.0.1:5143,http://127.0.0.1:5173",
    )

    @property
    def cors_origin_list(self):
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def resolve_sheets_credentials(self):
        """Sheets credentials as an absolute path, or None if unusable.

        Legacy ran with CWD=/home/nvidia/eye_compass, so a bare filename resolved
        against the legacy tree. The backend has a different working directory,
        so a relative name is resolved against the backend root and then the
        legacy source tree before giving up.
        """
        name = self.SHEETS_CREDENTIALS_FILE
        if not name:
            return None
        candidates = [Path(name)] if os.path.isabs(name) else [
            _backend_root / name,
            Path(self.EYE_COMPASS_SRC) / name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None


settings = Settings()
