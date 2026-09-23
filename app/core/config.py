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
    # Simple and direct: QUALIX_API_URL is whatever is written here, full
    # stop — no environment selection, no per-env variable indirection. Set
    # it to whichever Qualix host (dev/qa/prod) you actually want data to go
    # to. QUALIX_RUN_ENV is kept only because config.INI has it
    # (CONFIG_SETTINGS.run_env) and it still selects the ini fallback used
    # when QUALIX_API_URL itself isn't set at all.
    QUALIX_RUN_ENV: str = _env("QUALIX_RUN_ENV", section="CONFIG_SETTINGS", key="run_env", default="prod")
    QUALIX_API_URL: str = _env(
        "QUALIX_API_URL", section="API_ENV", key=QUALIX_RUN_ENV, default="https://assaying.qualix.ai/"
    )
    # Legacy appends this to the bare username before authenticating (main.py:598).
    QUALIX_USER_DOMAIN: str = _env("QUALIX_USER_DOMAIN", default="@agnext.in")

    OAUTH_URI_GET: str = _env("OAUTH_URI_GET", section="API_URI", key="oauth_uri_get", default="portal/oauth/authorize")
    OAUTH_URI_POST: str = _env("OAUTH_URI_POST", section="API_URI", key="oauth_uri_post", default="portal/login")
    CONFIG_URI: str = _env("CONFIG_URI", section="API_URI", key="config", default="portal/api/icompass/v1/config")
    ANALYSIS_POST_URI: str = _env(
        "ANALYSIS_POST_URI", section="API_URI", key="analysis_post_uri", default="portal/api/scan/v2/post-visio"
    )

    # Fixed 2-character namespace prefixed to every batch number. This is the
    # ONLY thing keeping batch numbers distinct between devices — the rest of
    # the id is a timestamp, which two devices can produce identically. A
    # blank, wrong-length, or duplicated code silently reintroduces
    # cross-device collisions that only surface later in Qualix, so batch
    # creation refuses to run until it's set (see _device_code in
    # api/batch.py). Must be unique per physical device. Unrelated to
    # DEVICE_CODE below — neither is derived from the other.
    DEVICE_ID: str = _env("DEVICE_ID", section="CONFIG_SETTINGS", key="device_id", default="").strip().upper()
    LOCATION: str = _env("LOCATION", section="CONFIG_SETTINGS", key="location", default="")

    # Sent as `device_serial_no` on every scan datagram, alongside operator_id
    # and warehouse_name below — this trio is what lets Qualix map location
    # explicitly from the payload instead of inferring it from whichever
    # account authenticated the post. Fixed per physical device, same as
    # DEVICE_ID.
    DEVICE_CODE: str = _env("DEVICE_CODE", section="CONFIG_SETTINGS", key="device_code", default="").strip().upper()

    WAREHOUSE_NAME: str = _env("WAREHOUSE_NAME", default="")

    # ---------------- Keycloak / Assurance ----------------
    # "legacy" keeps today's direct-Qualix login untouched; "keycloak" switches
    # operator login to Keycloak and routes syncing through the Assurance
    # gateway. Defaults to legacy so this ships dormant and is enabled per
    # device without a different build.
    AUTH_PROVIDER: str = _env("AUTH_PROVIDER", default="legacy").strip().lower()

    KEYCLOAK_URL: str = _env("KEYCLOAK_URL", default="https://dev.perfeqtfoods.com/keycloak")
    KEYCLOAK_REALM: str = _env("KEYCLOAK_REALM", default="CentralIAM")
    # Reusing Qualix's own backend client: creating a dedicated one would also
    # require an Assurance-side change. Direct Access Grants (ROPC) is already
    # enabled on it, which is what this integration needs.
    KEYCLOAK_CLIENT_ID: str = _env("KEYCLOAK_CLIENT_ID", default="qualix-backend")
    KEYCLOAK_CLIENT_SECRET: str = _env("KEYCLOAK_CLIENT_SECRET", default="")

    # Space-separated scopes requested at login. Two are load-bearing:
    #
    #   offline_access
    #     The realm caps a NORMAL refresh token at SSO Session Max (1 day),
    #     which would force a fresh login daily regardless of how long a
    #     session is otherwise allowed to stay unverified. This yields an
    #     offline token governed by the Offline Session settings instead
    #     (30-day idle, no absolute max), which is what makes staying logged
    #     in indefinitely actually achievable.
    #
    #   qualix-application-permissions
    #     Carries the audience mappers that put the Assurance gateway
    #     (gateway-client / asu-be) into the token's `aud`. Without it the
    #     gateway rejects every request with "Client is not within the token
    #     audience", even though the login itself succeeded — the token is
    #     simply not addressed to it. Only needed while borrowing a shared
    #     client where this scope is attached as Optional; a dedicated client
    #     would carry it as a Default scope and need nothing requested here.
    #
    # Requesting a scope the client does not have attached makes Keycloak
    # reject the whole login with invalid_scope, so this must match what is
    # actually configured on KEYCLOAK_CLIENT_ID.
    KEYCLOAK_SCOPE: str = _env(
        "KEYCLOAK_SCOPE",
        "KEYCLOAK_OFFLINE_SCOPE",  # previous name
        default="offline_access",
    )

    # ---- Two accounts, two jobs, deliberately not shared ----------------
    #
    # These were both QUALIX_USERNAME/PASSWORD, one pair doing two unrelated
    # things: authenticating every outbound sync, AND unlocking the device at
    # tier 3. That is wrong on both sides. It made the emergency door key and
    # the sync identity the same secret, so an operator password that has to
    # be shared for syncing also lets anyone into the device; and it caused a
    # real outage, when the account here existed in Qualix but not in
    # Keycloak and every sync failed with "Invalid user credentials".

    # WHO SYNCS. The fixed account every outbound delivery authenticates as —
    # the scan POST and the config fetch, under BOTH auth providers. Never the
    # logged-in operator: someone who signed in offline has no token at all,
    # so syncing could never depend on one.
    #
    # No fallback, on purpose. A blank value here fails loudly and says what
    # to set; quietly borrowing some other account is exactly how the outage
    # above went unnoticed. Under AUTH_PROVIDER=keycloak this must be a real
    # KEYCLOAK account, which the tier-3/emergency login below need not be.
    SYNC_SERVICE_USERNAME: str = _env("SYNC_SERVICE_USERNAME", default="")
    SYNC_SERVICE_PASSWORD: str = _env("SYNC_SERVICE_PASSWORD", default="")

    # WHO CAN GET IN WHEN NOTHING ELSE WORKS. Tier 3: the credentials that
    # unlock the device when Keycloak is unreachable AND no cached password
    # exists — a brand-new device, or one whose operator has never logged in
    # online here. Checked entirely locally (api/auth.py), against this value;
    # no network call, and no bearing on who anything is sent as.
    #
    # Resolved in three steps, and the last one is why this is not simply an
    # env var: on a real device the credentials live in legacy's config.INI
    # (CONFIG_SETTINGS username/password), not necessarily in .env at all.
    #
    #   1. EMERGENCY_LOGIN_USERNAME  — what to set from now on
    #   2. QUALIX_USERNAME           — DEPRECATED alias, for devices already
    #                                  deployed with the old name
    #   3. config.INI                — legacy's own location
    #
    # Steps 2 and 3 exist so that deploying this change cannot silently take
    # away a device's emergency login — the one path whose whole purpose is to
    # work when everything else has failed. _resolved_from_deprecated_source()
    # below warns at startup while either is still doing the work, so it is
    # visible when they can be cleaned up.
    EMERGENCY_LOGIN_USERNAME: str = _env(
        "EMERGENCY_LOGIN_USERNAME",
        "QUALIX_USERNAME",
        section="CONFIG_SETTINGS",
        key="username",
        default="",
    )
    EMERGENCY_LOGIN_PASSWORD: str = _env(
        "EMERGENCY_LOGIN_PASSWORD",
        "QUALIX_PASSWORD",
        section="CONFIG_SETTINGS",
        key="password",
        default="",
    )

    # Assurance fronts the same Qualix endpoints and accepts a Keycloak token,
    # adding whatever headers Qualix itself needs.
    ASSURANCE_API_URL: str = _env("ASSURANCE_API_URL", default="")

    # The gateway exposes those endpoints WITHOUT the "portal/" prefix that
    # direct Qualix uses — verified against the live gateway, where
    # portal/api/icompass/v1/config 404s and api/icompass/v1/config returns the
    # real config. Kept as their own settings rather than stripping the prefix
    # in code, so the two path sets stay independently correctable if either
    # side ever moves.
    ASSURANCE_CONFIG_URI: str = _env(
        "ASSURANCE_CONFIG_URI", default="api/icompass/v1/config"
    )
    ASSURANCE_ANALYSIS_POST_URI: str = _env(
        "ASSURANCE_ANALYSIS_POST_URI", default="api/scan/v2/post-visio"
    )

    # Called right after a successful Keycloak login, with the operator's own
    # access token — the same endpoint Qualix's own web login calls to look up
    # the account. This is the ONLY thing that confirms the person is an
    # actual Qualix operator, as opposed to merely someone with valid
    # credentials somewhere on the same shared Keycloak realm. See
    # app/services/keycloak_service.py:fetch_qualix_profile.
    ASSURANCE_KEYCLOAK_PROFILE_URI: str = _env(
        "ASSURANCE_KEYCLOAK_PROFILE_URI", default="api/user/keycloak-profile"
    )

    # ---------------- Sessions ----------------
    # Sessions never expire on their own — there is deliberately no lifetime
    # setting here. A session ends only when Keycloak says the account is no
    # longer good, which is what the worker below goes looking for. An operator
    # on a device that is offline for months stays logged in, because nothing
    # about the passage of time says anything about their account.
    SESSION_REVALIDATION_ENABLED: bool = _as_bool(
        os.getenv("SESSION_REVALIDATION_ENABLED"), True
    )
    # How often the worker WAKES UP, not how often it talks to Keycloak. A
    # session that has already been confirmed today is skipped, so in practice
    # each session is verified once a day; the extra wake-ups exist so that a
    # device which was offline at the first attempt gets another chance the
    # same day rather than waiting a full 24 hours.
    SESSION_REVALIDATION_INTERVAL_HOURS: int = _as_int(
        os.getenv("SESSION_REVALIDATION_INTERVAL_HOURS"), 6
    )

    # Frontend login-screen presentation only — no backend logic keys off this.
    # "single_form" is today's Login.jsx. Future: "redirect" (Keycloak's hosted
    # page online, our form offline) or "combined" (both on one screen).
    LOGIN_UI_MODE: str = _env("LOGIN_UI_MODE", default="single_form")

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
    # Simple and direct, same as QUALIX_API_URL above: SHEETS_SPREADSHEET_ID
    # is whatever is written here, full stop — no per-environment variable
    # indirection. Set it to whichever sheet you actually want data to land
    # in. Legacy always writes to one hardcoded sheet regardless of
    # environment (sheet_update.py:7); this at least makes the sheet
    # explicitly configurable rather than hardcoded in source.
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
    SYNC_RETRY_INTERVAL_MINUTES: int = _as_int(os.getenv("SYNC_RETRY_INTERVAL_MINUTES"), 30)
    SYNC_WORKER_ENABLED: bool = _as_bool(os.getenv("SYNC_WORKER_ENABLED"), True)

    # Legacy logged CPU/memory/disk every 300s (logger.py's ResourceMonitor).
    RESOURCE_MONITOR_INTERVAL_SECONDS: int = _as_int(
        os.getenv("RESOURCE_MONITOR_INTERVAL_SECONDS"), 300
    )
    RESOURCE_MONITOR_ENABLED: bool = _as_bool(os.getenv("RESOURCE_MONITOR_ENABLED"), True)

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


def _warn_if_emergency_login_is_deprecated():
    """Say so, at startup, while the emergency login still comes from an old
    source — so nobody has to guess whether QUALIX_* is safe to delete.

    Silent when EMERGENCY_LOGIN_* is set properly, and silent when no
    emergency login is configured at all (that is a separate concern).
    """
    if os.getenv("EMERGENCY_LOGIN_USERNAME"):
        return
    if os.getenv("QUALIX_USERNAME"):
        source = "the deprecated QUALIX_USERNAME/PASSWORD environment variables"
    elif settings.EMERGENCY_LOGIN_USERNAME:
        source = f"legacy config.INI ({_ini_path})"
    else:
        return
    logger.warning(
        "The tier-3 emergency login is still being read from %s. Set "
        "EMERGENCY_LOGIN_USERNAME / EMERGENCY_LOGIN_PASSWORD instead — they are "
        "the device's recovery key and should not be the account that syncs "
        "(SYNC_SERVICE_USERNAME).",
        source,
    )


_warn_if_emergency_login_is_deprecated()
