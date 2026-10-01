"""
SQLAlchemy models mapping the legacy SQLite schema (eye_compass/database.py:23-69)
onto PostgreSQL.

Table and column names are kept identical to the legacy ones so that
scripts/migrate_sqlite_to_postgres.py can move data across without renaming.

Three columns that the legacy code stored as stringified Python literals
(`str(list)` / `str(dict)`, read back with ast.literal_eval) are JSONB here:
    com_details.analysis, com_details.variety, result.result
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from app.core.database import Base

# JSONB on PostgreSQL, plain JSON everywhere else (so a SQLite fallback still works).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Creds(Base):
    """Cached operator credentials for offline login (legacy `creds`).

    One row, replaced whole on every successful online login — this is a
    single shared kiosk device, not a per-operator store, so it only ever
    remembers whoever most recently confirmed online.

    keycloak_user_id and refresh_token ride along with the password hash for
    exactly the same reason the hash itself is here: so a later offline
    (cached-hash) login isn't treated as a total unknown. Without this, every
    logout+offline-login cycle created a session with nothing to check against
    Keycloak at all, even though this exact account had been confirmed online
    just hours or days before — that got the same hard "sign in again" wall as
    a genuinely disabled account, which was wrong. Carrying these forward lets
    a cached-hash login attach the same still-good refresh token, so the daily
    worker can revalidate it exactly like any online session.
    """

    __tablename__ = "creds"

    id = Column(Integer, primary_key=True)
    user = Column(String(150), index=True)
    password = Column(String(255))
    keycloak_user_id = Column(String(64), default="")
    refresh_token = Column(Text)
    # Qualix's own user_id (the `user.user_id` field of /user/keycloak-profile,
    # or the equivalent from legacy's direct Qualix login) — what Qualix's
    # location-mapping actually keys off, sent as `operator_id` on every scan
    # datagram. Rides along with the rest of this row for exactly the same
    # reason: refreshed on every online login, carried forward unchanged on a
    # later offline (cached-hash) one.
    operator_id = Column(String(64), default="")


class Session(Base):
    """An issued login session token.

    No legacy counterpart — the legacy app was a single-process kiosk whose
    login screen physically gated the page change, so there was nothing to
    persist. This backend listens on a socket and issues bearer tokens.

    These were held in a process-local dict until sessions had to survive a
    restart: an operator was meant to stay logged in for weeks, but every
    `systemctl restart` silently logged everyone back out. Persisting them here
    makes the promise real.

    There is no expiry column, and that is the point. A session is valid while
    its row exists; the only thing that ends one is Keycloak saying the account
    is no longer good, which sets needs_relogin. Elapsed time is never a
    reason — a device that has been out of signal for months knows exactly as
    much about its operator's account as it did on day one, so logging them out
    would punish them for the network.

    Two earlier designs are worth recording so they are not reintroduced. The
    first stored an `expires_at` that the daily worker rewrote to "now + 45
    days" on every check — a date that silently moved every day, so the row
    could not tell you whether a session was an hour or a year old. The second
    kept last_verified_at but still lapsed sessions after a fixed number of
    unconfirmed days, which signed out offline operators who had done nothing
    wrong and could not possibly have been checked.

    last_verified_at survives both, as a record of when Keycloak last confirmed
    the account. It is for diagnostics and log-reading only and decides nothing.

    needs_relogin is set when Keycloak reports the account disabled or revoked,
    and also when an operator who signed in offline can finally be checked
    because connectivity has returned. It deliberately does NOT invalidate the
    token: the operator may be mid-batch, and cutting them off there would lose
    the scan. The frontend acts on it at a safe checkpoint instead (Home screen
    only), where it prompts rather than yanking them out.
    """

    __tablename__ = "sessions"

    token = Column(String(64), primary_key=True)
    username = Column(String(150), index=True)
    mode = Column(String(30))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_verified_at = Column(DateTime, index=True)
    refresh_token = Column(Text)
    needs_relogin = Column(Boolean, default=False, nullable=False)
    # The softer sibling of needs_relogin, and deliberately a separate column
    # rather than another value in one field: the two mean different things to
    # the operator and get different screens. needs_relogin means Keycloak
    # actively refused this account (disabled, deleted, or the password was
    # changed) — a blocking dialog, because the credentials on this device are
    # genuinely wrong now. relogin_suggested means only that the stored offline
    # token reached the realm's idle limit while the device was out of contact;
    # the account itself is confirmed fine, so this is a banner they can
    # ignore, not a wall.
    relogin_suggested = Column(Boolean, default=False, nullable=False)
    first_name = Column(String(150), default="")
    email = Column(String(255), default="")
    roles = Column(JSONType, default=list)

    # Keycloak's own internal user id (the JWT's "sub" claim) — captured for
    # free at login, no extra network call. Lets the daily revalidation
    # worker ask Keycloak's Admin API about this exact account later, without
    # ever needing the operator's password again.
    keycloak_user_id = Column(String(64), default="")
    # Qualix's own user_id — see Creds.operator_id for what it's for and where
    # it comes from. Copied onto every session (online or offline) the same
    # way keycloak_user_id/refresh_token already are, so /scan/submit can read
    # it straight off the caller's session without a fresh Qualix call.
    operator_id = Column(String(64), default="")
    # The account's password credential's own createdDate, as Keycloak's Admin
    # API reports it. A refresh-token grant proves the account/session is
    # still valid, but not that the password is still the one the operator
    # typed in — confirmed by direct test, an offline refresh token happily
    # survives a password change. Comparing this timestamp on each daily
    # check is what actually catches a change, without ever storing or
    # resubmitting the password itself. Null until the worker's first
    # successful Admin API check establishes a baseline.
    password_credential_created_at = Column(DateTime, nullable=True)


class ClientInfo(Base):
    """Client name + image folder, fetched from Qualix at login (legacy `clientinfo`).

    image_folder_name drives the S3 key prefix — see services/s3_worker.py.
    """

    __tablename__ = "clientinfo"

    id = Column(Integer, primary_key=True)
    client_name = Column(String(100))
    image_folder_name = Column(String(150))


class S3UploadState(Base):
    """When the S3 uploader last ran. One row, id=1 (legacy has no equivalent).

    This is the whole schedule: the worker uploads every S3_UPLOAD_EVERY_DAYS,
    and it decides whether a run is due by comparing `last_completed_at` against
    now. It has to outlive the process — a plain in-memory timer restarts its
    count on every reboot, and this device reboots far more often than every
    three days, so the run would never come due.

    Only a *completed* run sets `last_completed_at`. One that failed or stopped
    early (no internet, a scan started) leaves it alone, so the next check
    retries rather than waiting out another full period.
    """

    __tablename__ = "s3_upload_state"

    id = Column(Integer, primary_key=True)
    # When the device first had this feature enabled. The first upload is due
    # one period after this, not immediately: switching it on should not start
    # clearing the existing backlog off the disk while someone is working.
    first_seen_at = Column(DateTime, default=datetime.now)
    last_attempt_at = Column(DateTime, nullable=True)
    last_completed_at = Column(DateTime, nullable=True)
    # The last run's summary (counts of uploaded/deleted/failed), same dict the
    # worker logs. Kept for support: "when did this device last clear down, and
    # did anything fail?" without needing the journal, which is not persistent.
    last_result = Column(JSONType, nullable=True)


class SurveyorDetails(Base):
    """Populates the Sorter Name dropdown (legacy `surveyordetails`)."""

    __tablename__ = "surveyordetails"

    id = Column(Integer, primary_key=True)
    surveyor_id = Column(String(50))
    name = Column(String(150))


class BrandDetails(Base):
    """Populates the Brand dropdown (legacy `branddetails`)."""

    __tablename__ = "branddetails"

    id = Column(Integer, primary_key=True)
    brand_name = Column(String(150))

    __table_args__ = (UniqueConstraint("brand_name", name="uq_branddetails_brand_name"),)


class VendorDetails(Base):
    """Populates the Vendor Name dropdown and the vendor_code auto-fill
    (legacy `vendordetails`, consumed at main.py:1177-1184)."""

    __tablename__ = "vendordetails"

    id = Column(Integer, primary_key=True)
    vendor_name = Column(String(150))
    vendor_code = Column(String(50))


class CommodityDetails(Base):
    """Commodity / variety / foreign-matter config from Qualix (legacy `com_details`).

    `analysis` is the list of FM analysis names for the commodity.
    `variety`  is the list of variety objects ({variety_code, variety_name, ...}).
    Legacy stored both as str(list); they are JSON here.
    """

    __tablename__ = "com_details"

    id = Column(Integer, primary_key=True)
    commodity = Column(String(150), index=True)
    commodity_id = Column(String(50))
    analysis = Column(JSONType, default=list)
    variety = Column(JSONType, default=list)


class Result(Base):
    """A completed scan (legacy `result`).

    `result` holds the full Qualix datagram ({"scan_data": {...}, "analysis": [...]}).
    Legacy stored str(dict) capped at varchar(400) — unenforced by SQLite, and the
    longest live payload is 1587 chars — so this is JSONB with no length cap.

    sync_status keeps the legacy three-valued contract:
        '1' = Qualix accepted (HTTP 200)
        '2' = Qualix rejected the payload (HTTP 400) — terminal, do not retry
        '0' = not yet delivered — the retry worker picks these up
    """

    __tablename__ = "result"

    id = Column(Integer, primary_key=True)
    sample_id = Column(String(150), index=True)
    commodity = Column(String(150))
    variety = Column(String(150))
    result = Column(JSONType)
    date = Column(String(50), index=True)
    start_time = Column(String(50))
    stop_time = Column(String(50))
    sync_status = Column(String(20), index=True, default="0")
    # Why the last delivery attempt failed, verbatim from Qualix where it said
    # so (e.g. {"error-code":"12092","error-message":"Device does not exist"}).
    # Without this a '2' was a dead end on screen: the operator saw "Rejected"
    # with no way to find out why, and the reason existed only in the backend
    # log. Cleared on a successful sync so a stale reason can't outlive it.
    sync_error = Column(Text, default="")
    # Idempotency key: the UUID the client minted for this save, before it ever
    # sent the request. On a flaky link the browser can time out waiting for
    # /confirm that in fact succeeded, and the operator retries; the retry
    # carries the SAME uuid, so it is recognised as a replay and answered with
    # the id already stored instead of saving the scan a second time.
    #
    # Nullable and unique together: rows written before this existed (and any
    # future non-client writer) carry NULL, and Postgres permits any number of
    # NULLs under a unique index while still rejecting a repeated real value.
    client_request_id = Column(String(64), nullable=True)

    __table_args__ = (
        # Legacy identified a record by this 4-tuple (database.py:433-445).
        Index("ix_result_identity", "sample_id", "date", "start_time", "stop_time"),
        # The constraint — not the SELECT in confirm_scan — is what actually
        # makes a replay impossible: two retries arriving at once both find no
        # existing row, and this turns the second INSERT into an IntegrityError
        # that confirm_scan resolves by returning the winner's id.
        Index("uq_result_client_request_id", "client_request_id", unique=True),
    )


class BatchDetails(Base):
    """The 12-field batch form (legacy main.py:1663-1710 read these straight off the UI).

    Legacy never persisted them separately — they were embedded in the result
    datagram. We persist them so the scan submission can reference a batch by id
    and rebuild the full datagram server-side.
    """

    __tablename__ = "batch_details"

    id = Column(Integer, primary_key=True)
    # unique: the id is generated from a clock, and a clock can repeat itself
    # (no battery-backed RTC on these devices). api/batch.py already guards
    # against that when generating; this is the backstop that makes a
    # duplicate impossible rather than merely unlikely.
    batch_number = Column(String(150), index=True, unique=True)
    po_number = Column(String(150))
    manufacturing_date = Column(String(50))
    vendor_name = Column(String(150))
    receiving_date = Column(String(50))
    sorting_quantity = Column(String(50))
    product_name = Column(String(150))
    brand = Column(String(150))
    vendor_code = Column(String(50))
    site_code = Column(String(50))
    product_code = Column(String(150))
    sorter_name = Column(String(150))
    created_at = Column(String(50))


class ScanProgress(Base):
    """One row per batch scan run: the state needed to continue it later.

    Not a legacy table. Legacy kept a running scan only in memory, so leaving
    the scan — on purpose, or by losing power — lost it. A run's *results* are
    mostly on disk already (create_results counts the crop files in its output
    folder; Frame Count counts its r_frame files), so this row only has to hold
    what is otherwise in memory: who the batch is, when it started, its folders,
    the stop counters, and how far its file numbering has got.

    status:
        active       the batch currently loaded in the scan session
        held         the operator put it on hold from the results page
        interrupted  it was active when the backend stopped (power cut,
                     restart) or was abandoned for another batch
        saved        confirmed — it is in History now
        discarded    cancelled; its crops went to rejected/

    held and interrupted are the "open" states: listed under Held Batches on
    Home, continuable, and protected from the S3 cleanup.

    Written at operator actions (Start, Stop, each review, Submit, Hold) and
    never from the per-frame detection loop, so it costs the scan nothing.
    """

    __tablename__ = "scan_progress"

    id = Column(Integer, primary_key=True)
    # The run's unique folder name, <sample_id>_<YYYYmmddHHMMSS> — the same
    # value as image_unique_id in the Qualix datagram.
    folder_name = Column(String(200), unique=True, nullable=False)
    sample_id = Column(String(100), index=True)
    commodity = Column(String(100))
    variety = Column(String(100))
    batch_id = Column(Integer, nullable=True)
    analysis_parameters = Column(JSONType, default=list)
    start_date = Column(String(20))
    start_time = Column(String(20))
    output_folder = Column(Text)
    output_frame_folder = Column(Text)
    status = Column(String(20), index=True, default="active")
    # True once the run has been through Submit and is waiting on the results
    # page (or was held from there). Submit counts an implicit stop of its own,
    # so a run that is submitted, held and submitted again would count two;
    # restore uses this to take the first one back out.
    awaiting_save = Column(Boolean, default=False)

    # Counters the in-memory session carries between frames. On restore these
    # are a floor, not the answer: the files on disk are checked as well, since
    # a power cut can land after a file was written but before this row was.
    frame_count = Column(Integer, default=0)
    saved_frame_count = Column(Integer, default=0)
    clean_frame_count = Column(Integer, default=0)
    next_pending_index = Column(Integer, default=0)
    # ScanSession.conveyor_stop_count, verbatim.
    conveyor_stop_count = Column(JSONType, default=dict)

    # Time spent on hold. Kept for a possible later requirement; deliberately
    # NOT counted in any stop metric and not shown in the UI.
    hold_count = Column(Integer, default=0)
    total_held_seconds = Column(Integer, default=0)
    # Start of the current hold, while held or interrupted.
    held_at = Column(DateTime, nullable=True)
    # Every hold, as [{"held_at", "resumed_at", "reason"}], reason "held" or
    # "interrupted".
    hold_history = Column(JSONType, default=list)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    closed_at = Column(DateTime, nullable=True)
