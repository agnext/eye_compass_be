"""
Background S3 uploader: every S3_UPLOAD_EVERY_DAYS, upload output/,
output_frame/ and Data_Collection/ older than S3_RETENTION_DAYS, and delete
from the device only what S3 is confirmed to hold.

Two separate periods, easily confused:

  S3_UPLOAD_EVERY_DAYS  how often a run happens          (default 3)
  S3_RETENTION_DAYS     how much data always stays here  (default 3)

`output/` is held for at least HISTORY_WINDOW_DAYS (30) instead, because it is
the one tree the UI reads — see retention_days().

Retention is what decides whether a batch is touched at all; the schedule only
decides when to look. A batch is uploaded and removed once nothing in it has
changed for S3_RETENTION_DAYS, so the most recent few days are always on the
device to look at. Because a run only happens every S3_UPLOAD_EVERY_DAYS, a
batch can be up to the two periods added together old before it actually goes:
shorten S3_UPLOAD_EVERY_DAYS to tighten that, which is safe — retention, not
the schedule, is what protects recent data.

Why it works this way
---------------------

**The schedule lives in the database, not in a sleep.** A plain
`sleep(3 days)` starts counting again on every restart, and this device restarts
far more often than every three days, so it would never run. Instead the worker
wakes every S3_CHECK_INTERVAL_SECONDS (an hour by default), reads when the last
*completed* run finished from the `s3_upload_state` table, and only does real
work once that is S3_UPLOAD_EVERY_DAYS ago. The hourly wake-up also covers the
cases where a run cannot go ahead: no internet, or a scan in progress. It tries
again an hour later, not three days later.

The state is in Postgres rather than a file for the ordinary reason — it is
durable state, and that is where this app's durable state lives — and for one
specific to this worker: the only sensible place for such a file would be
inside OUTPUT_DIR, which is the very tree this worker walks and empties. State
recording the clearing of a directory does not belong inside it.

**It lives inside the backend, not in cron or a systemd timer.** Legacy's own
answer was a cron script (`upload_videos_pool_id.py`) that guessed whether
inference was running from the process list. From inside the backend this is
known exactly: whether a scan or data collection is running, and which folders
they are writing to. Those folders are never touched, and a run that is in
progress stops between files as soon as a scan starts.

**A file is deleted only after its copy on S3 is confirmed identical.** For
each file:

 1. hash it (SHA-256), checking before and after that it did not change;
 2. if S3 already has that key with the same SHA-256 and size, skip the upload;
    otherwise upload it with that SHA-256 attached. S3 checks the bytes it
    received against it and refuses the upload if they differ;
 3. read the object's details back from S3 (`HeadObject`) and require the
    stored SHA-256 and size to match the local file;
 4. check the local file still has the same size and modification time as when
    it was hashed, and only then delete it.

Anything that fails, or cannot be checked, stays on the device and is tried
again next run. Nothing is ever removed with `rmtree`. Files are deleted one at
a time, each after its own check, and a folder is removed only with `rmdir`,
which fails unless the folder is already empty.

This replaces an uploader that ran every 60 seconds, went through the device's
entire history each time with one S3 request per file ever written
(`todos.md`), never deleted anything, and ignored Data_Collection/.

The S3 key layout is unchanged: `<bucket_folder>/<client>/<sub>/<path under
sub>`, with `bucket_folder`/`client` from the Qualix client info cached at
login, else from config.
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

# Uploaded, in this order. The three trees share one layout underneath —
# <sub>/<commodity>/<variety>/<one folder per batch or collection run>/ — which
# is what lets "a session folder" mean the same thing for all of them.
SUBTREES = ("output", "output_frame", "Data_Collection")
SESSION_DEPTH = 3

# Stop a run after this many failures in a row. With no route to S3, every file
# fails after the full connect timeout and retries; without this a run would
# spend hours failing one file at a time. It is tried again at the next check.
MAX_CONSECUTIVE_FAILURES = 5


class Deferred(Exception):
    """A scan or data collection started mid-run. Stop cleanly and retry later."""


class S3UploaderTask:
    def __init__(self):
        self.is_running = False
        self._client = None

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def _init_s3(self) -> bool:
        if not settings.S3_IDENTITY_POOL:
            logger.warning(
                "S3 disabled: no Cognito identity pool configured "
                "(set AWS_IDENTITY_POOL_ID in .env)."
            )
            return False
        try:
            import boto3
            from botocore.config import Config as BotoConfig

            cfg = BotoConfig(
                connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}
            )
            cognito = boto3.client(
                "cognito-identity", region_name=settings.S3_REGION, config=cfg
            )
            identity = cognito.get_id(IdentityPoolId=settings.S3_IDENTITY_POOL)
            creds = cognito.get_credentials_for_identity(
                IdentityId=identity["IdentityId"]
            )["Credentials"]
            self._client = boto3.client(
                "s3",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretKey"],
                aws_session_token=creds["SessionToken"],
                region_name=settings.S3_REGION,
                config=cfg,
            )
            logger.info("S3 credentials initialised for bucket %s", settings.S3_BUCKET)
            return True
        except Exception as exc:
            logger.error("S3 init failed: %s", exc)
            self._client = None
            return False

    def _key_prefix(self) -> str:
        """Prefix from the Qualix client info if we have it, else config."""
        folder = settings.S3_BUCKET_FOLDER
        client = settings.S3_CLIENT
        try:
            from app.core.database import SessionLocal
            from app.models.schema import ClientInfo

            db = SessionLocal()
            try:
                info = db.query(ClientInfo).first()
                if info:
                    folder = info.image_folder_name or folder
                    client = info.client_name or client
            finally:
                db.close()
        except Exception as exc:
            logger.debug("Could not read client info for S3 prefix: %s", exc)

        parts = [p.strip("/") for p in (folder, client) if p]
        return ("/".join(parts) + "/") if parts else ""

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------
    @staticmethod
    def _load_state() -> Dict:
        """The single s3_upload_state row as a plain dict, {} if there is none.

        A dict rather than the ORM object so the row is read and the session
        closed straight away: a run takes minutes to hours, and holding a
        connection open across it would occupy a pool slot for no reason.
        """
        from app.core.database import SessionLocal
        from app.models.schema import S3UploadState

        try:
            db = SessionLocal()
            try:
                row = db.query(S3UploadState).filter(S3UploadState.id == 1).first()
                if row is None:
                    return {}
                return {
                    "first_seen_at": row.first_seen_at,
                    "last_attempt_at": row.last_attempt_at,
                    "last_completed_at": row.last_completed_at,
                    "last_result": row.last_result,
                }
            finally:
                db.close()
        except Exception as exc:
            # A database problem must never read as "nothing scheduled, so run
            # now" — that would upload and delete off an unknown schedule.
            # tick() treats this as "skip this check", so nothing is deleted.
            logger.error("Could not read S3 upload state: %s", exc)
            return {"__unreadable__": True}

    @staticmethod
    def _save_state(**fields) -> None:
        """Create or update the single row in one commit."""
        from app.core.database import SessionLocal
        from app.models.schema import S3UploadState

        db = SessionLocal()
        try:
            row = db.query(S3UploadState).filter(S3UploadState.id == 1).first()
            if row is None:
                row = S3UploadState(id=1, first_seen_at=datetime.now())
                db.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
            db.commit()
        finally:
            db.close()

    def _next_due(self, state: Dict) -> Optional[datetime]:
        """When the next run is due, or None if due now."""
        last = state.get("last_completed_at") or state.get("first_seen_at")
        if not isinstance(last, datetime):
            return None
        now = datetime.now()
        # A clock that jumped backwards (this device's RTC has done it) would
        # otherwise leave the next run in the future for as long as it's off
        # by — possibly years — while the disk fills. Running early is
        # harmless; not running is not.
        if last > now:
            return None
        due = last + timedelta(days=settings.S3_UPLOAD_EVERY_DAYS)
        return None if due <= now else due

    @staticmethod
    def _busy() -> Optional[str]:
        """Why now is a bad time, or None. Checked before a run and between files."""
        try:
            from app.services.scan_session import scan_session
            if scan_session.active:
                return "a batch scan is running"
        except Exception:
            pass
        try:
            from app.api import camera
            if getattr(camera, "_dc_recording", False):
                return "data collection is recording"
        except Exception:
            pass
        return None

    @staticmethod
    def _protected_dirs() -> Set[str]:
        """Folders that are, or may still be, written to: never touched.

        Includes every held or interrupted batch (scan_progress). The scan
        session's own folders are set whenever a batch folder is known to it,
        not only while scanning: after Submit the results page can still reclassify
        crops and rewrite result.json in it, and it stays set until the next
        batch starts.
        """
        dirs = set()
        try:
            from app.services.scan_session import scan_session
            for d in (scan_session.output_folder, scan_session.output_frame_folder):
                if d:
                    dirs.add(os.path.realpath(d))
        except Exception:
            pass
        try:
            from app.api import camera
            d = getattr(camera, "_dc_folder", None)
            if d:
                dirs.add(os.path.realpath(d))
        except Exception:
            pass
        # Every batch that is not finished: the active one, and every held or
        # interrupted one waiting to be continued. A held batch is continued in
        # its own folders, so uploading-and-removing them would take its
        # captured FMs and frames away from under it. Unlike the two above this
        # is deliberately NOT allowed to fail quietly: if the database cannot
        # say which batches are open, the run is abandoned (the exception
        # propagates) rather than treating "unknown" as "nothing protected".
        from app.services.scan_progress import open_folders

        for d in open_folders():
            dirs.add(os.path.realpath(d))
        return dirs

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def start(self):
        self.is_running = True
        logger.info(
            "S3 worker started: every %s day(s), checked every %ss, delete after "
            "upload: %s",
            settings.S3_UPLOAD_EVERY_DAYS, settings.S3_CHECK_INTERVAL_SECONDS,
            settings.S3_DELETE_AFTER_UPLOAD,
        )
        try:
            while self.is_running:
                try:
                    await asyncio.to_thread(self.tick)
                except Exception as exc:
                    logger.error("S3 worker check failed: %s", exc)
                await asyncio.sleep(settings.S3_CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("S3 background worker stopped.")
            raise

    def stop(self):
        self.is_running = False

    def tick(self, force: bool = False) -> Optional[Dict]:
        """Run if due and idle. Returns the run's summary, or None if not run."""
        state = self._load_state()
        if state.get("__unreadable__") and not force:
            return None
        if not state.get("first_seen_at"):
            # First start with this feature. The count starts here rather than
            # running at once, so turning it on does not immediately upload
            # and delete the device's whole backlog while someone is using it.
            self._save_state(first_seen_at=datetime.now())
            if not force:
                return None
            state = self._load_state()
        due = self._next_due(state)
        if due is not None and not force:
            logger.debug("S3 upload not due until %s", due.isoformat(timespec="minutes"))
            return None
        reason = self._busy()
        if reason:
            logger.info("S3 upload due but put off: %s. Will check again.", reason)
            return None

        self._save_state(last_attempt_at=datetime.now())
        summary = self.run_once()
        done = {"last_result": summary}
        if summary.get("complete"):
            done["last_completed_at"] = datetime.now()
        self._save_state(**done)
        return summary

    # ------------------------------------------------------------------
    # One run
    # ------------------------------------------------------------------
    @staticmethod
    def retention_days(sub: str) -> float:
        """How many days of `sub` stay on the device.

        S3_RETENTION_DAYS for most trees — but `output/` is held for at least
        HISTORY_WINDOW_DAYS as well, because `output/` is the one tree the UI
        reads: History's record view loads a batch's crops straight out of it
        (`api/history.py`). Removing a batch's crops while History still lists
        that batch leaves a row the operator can open and find empty. Tying the
        two together means the rule "if History lists it, its images are still
        here" holds by construction, including if HISTORY_WINDOW_DAYS is later
        changed.

        It is close to free: `output/` holds only crops and result.json — 14 MB
        against 2.5 GB across output_frame/ and Data_Collection/ on this device,
        about 0.5% of the data. The two big trees are read by nothing in the UI
        and keep the shorter window.

        HISTORY_WINDOW_DAYS=0 means History shows *everything*, which no finite
        retention can cover, so the plain S3_RETENTION_DAYS applies and older
        rows show the "uploaded to cloud storage" notice instead of crops.
        """
        days = settings.S3_RETENTION_DAYS
        if sub == "output" and settings.HISTORY_WINDOW_DAYS > 0:
            days = max(days, float(settings.HISTORY_WINDOW_DAYS))
        return days

    @classmethod
    def _retention_seconds(cls, sub: str) -> float:
        """The above in seconds, with S3_MIN_AGE_MINUTES as a floor.

        The floor is what still guards a folder written to moments ago when
        S3_RETENTION_DAYS is set to 0.

        Age is the newest modification time anywhere in the folder, not when
        the batch started — so a batch whose crops were reclassified yesterday
        counts as a day old, not as old as the scan. That is the wanted
        behaviour: what the retention window protects is data someone may still
        be working with.
        """
        return max(
            cls.retention_days(sub) * 86400.0,
            settings.S3_MIN_AGE_MINUTES * 60.0,
        )

    def eligible_sessions(self) -> Tuple[List[Tuple[str, str]], Dict[str, int]]:
        """(subtree, session folder) pairs this run may process, plus skip counts."""
        protected = self._protected_dirs()
        now = time.time()
        found, skipped = [], {"in_use": 0, "within_retention": 0}
        for sub in SUBTREES:
            root = os.path.join(settings.OUTPUT_DIR, sub)
            if not os.path.isdir(root):
                continue
            min_age = self._retention_seconds(sub)
            for session in _dirs_at_depth(root, SESSION_DEPTH):
                real = os.path.realpath(session)
                if any(real == p or real.startswith(p + os.sep) or p.startswith(real + os.sep)
                       for p in protected):
                    skipped["in_use"] += 1
                    continue
                if now - _newest_mtime(session) < min_age:
                    # Inside the retention window — kept on the device, and
                    # not uploaded either: it goes up on the run after it ages
                    # out, so a folder is never left half-cleared.
                    skipped["within_retention"] += 1
                    continue
                found.append((sub, session))
        return found, skipped

    def run_once(self, dry_run: bool = False) -> Dict:
        summary = {
            "sessions": 0, "files": 0, "uploaded": 0, "already_there": 0,
            "deleted": 0, "bytes_freed": 0, "failed": 0, "not_verified": 0,
            "folders_removed": 0, "complete": False,
        }
        sessions, skipped = self.eligible_sessions()
        summary.update({f"skipped_{k}": v for k, v in skipped.items()})
        summary["sessions"] = len(sessions)

        if dry_run:
            for sub, session in sessions:
                files = list(_files_under(session))
                summary["files"] += len(files)
                summary["bytes_freed"] += sum(os.path.getsize(f) for f in files)
                logger.info("[dry run] would upload %s file(s) from %s", len(files), session)
            return summary

        if not sessions:
            summary["complete"] = True
            logger.info("S3 upload: nothing to upload (%s)", skipped)
            return summary
        if self._client is None and not self._init_s3():
            return summary

        prefix = self._key_prefix()
        consecutive = 0
        started = time.time()
        try:
            for sub, session in sessions:
                root = os.path.join(settings.OUTPUT_DIR, sub)
                for path in sorted(_files_under(session)):
                    reason = self._busy()
                    if reason:
                        raise Deferred(reason)
                    summary["files"] += 1
                    key = prefix + sub + "/" + os.path.relpath(path, root).replace(os.sep, "/")
                    outcome = self._process_file(path, key, summary)
                    if outcome == "failed":
                        consecutive += 1
                        if consecutive >= MAX_CONSECUTIVE_FAILURES:
                            logger.error(
                                "S3 upload: %s failures in a row — stopping this "
                                "run, will retry at the next check.", consecutive,
                            )
                            return self._finish(summary, started)
                    else:
                        consecutive = 0
                if settings.S3_DELETE_AFTER_UPLOAD:
                    summary["folders_removed"] += _remove_empty_dirs(session)
        except Deferred as why:
            logger.info("S3 upload paused mid-run: %s. Resumes at the next check.", why)
            return self._finish(summary, started)

        summary["complete"] = summary["failed"] == 0 and summary["not_verified"] == 0
        return self._finish(summary, started)

    @staticmethod
    def _finish(summary: Dict, started: float) -> Dict:
        logger.info(
            "S3 upload %s in %.0fs: %s session(s), %s file(s) — %s uploaded, "
            "%s already there, %s deleted (%.1f MB freed), %s failed, %s not "
            "verified (kept); skipped %s in use, %s within retention "
            "(%s days, output/ %s)",
            "complete" if summary["complete"] else "incomplete",
            time.time() - started, summary["sessions"], summary["files"],
            summary["uploaded"], summary["already_there"], summary["deleted"],
            summary["bytes_freed"] / 1048576, summary["failed"],
            summary["not_verified"], summary.get("skipped_in_use", 0),
            summary.get("skipped_within_retention", 0), settings.S3_RETENTION_DAYS,
            S3UploaderTask.retention_days("output"),
        )
        return summary

    def _process_file(self, path: str, key: str, summary: Dict) -> str:
        """Upload one file if needed, verify it, delete it if verified.

        Returns "ok", "kept" (uploaded or present, but not deleted) or "failed".
        """
        try:
            before = os.stat(path)
            digest = _sha256_b64(path)
            after = os.stat(path)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                logger.warning("S3: %s changed while being read — left for next run", path)
                summary["not_verified"] += 1
                return "kept"

            remote = self._head(key)
            if _matches(remote, digest, after.st_size):
                summary["already_there"] += 1
            else:
                self._put(path, key, digest)
                summary["uploaded"] += 1
                remote = self._head(key)

            if not _matches(remote, digest, after.st_size):
                logger.error(
                    "S3: %s uploaded but S3's copy does not read back as identical "
                    "(size/SHA-256) — kept on device", path,
                )
                summary["not_verified"] += 1
                return "kept"

            if not settings.S3_DELETE_AFTER_UPLOAD:
                return "kept"

            # Last check right before deleting: the file is still exactly the
            # one whose copy was just confirmed.
            now = os.stat(path)
            if (now.st_size, now.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                logger.warning("S3: %s changed after upload — kept, re-uploaded next run", path)
                summary["not_verified"] += 1
                return "kept"
            os.remove(path)
            summary["deleted"] += 1
            summary["bytes_freed"] += now.st_size
            return "ok"
        except Exception as exc:
            summary["failed"] += 1
            logger.error("S3 upload failed for %s: %s", path, exc)
            # Cognito credentials last about an hour; a long run outlives them.
            if "ExpiredToken" in str(exc) or "InvalidAccessKeyId" in str(exc):
                self._init_s3()
            return "failed"

    def _head(self, key: str) -> Optional[Dict]:
        from botocore.exceptions import ClientError
        try:
            return self._client.head_object(
                Bucket=settings.S3_BUCKET, Key=key, ChecksumMode="ENABLED"
            )
        except ClientError as err:
            code = str(err.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def _put(self, path: str, key: str, digest: str) -> None:
        # Single-request put, never multipart: a multipart object's stored
        # checksum is a checksum of the parts' checksums, which cannot be
        # compared with the file's own SHA-256. put_object takes up to 5 GB;
        # the largest thing written here is a few MB.
        with open(path, "rb") as fh:
            self._client.put_object(
                Bucket=settings.S3_BUCKET, Key=key, Body=fh,
                ChecksumAlgorithm="SHA256", ChecksumSHA256=digest,
            )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _matches(head: Optional[Dict], digest: str, size: int) -> bool:
    """S3's stored copy has exactly this SHA-256 and size.

    False when S3 did not return a SHA-256 at all: an object uploaded by legacy
    or by the previous uploader has none, and "same size" alone is not proof it
    is the same file. It gets uploaded again with one.
    """
    return bool(
        head
        and head.get("ChecksumSHA256") == digest
        and int(head.get("ContentLength", -1)) == size
    )


def _sha256_b64(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode("ascii")


def _dirs_at_depth(root: str, depth: int):
    level = [root]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    nxt.extend(e.path for e in it if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
        level = nxt
    return sorted(level)


def _files_under(folder: str):
    for dirpath, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path) and not os.path.islink(path):
                yield path


def _newest_mtime(folder: str) -> float:
    newest = os.path.getmtime(folder)
    for dirpath, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return newest


def _remove_empty_dirs(session: str) -> int:
    """rmdir the session folder and its sub-folders, deepest first, if empty.

    Only ever os.rmdir, which refuses a folder with anything left in it, so a
    file that was kept (failed, unverified, or written after the listing) keeps
    its folder too. Parents above the session folder are left alone: a scan
    starting at that moment may be creating its own folder under them.
    """
    removed = 0
    for dirpath, _dirs, _files in os.walk(session, topdown=False):
        try:
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            pass
    return removed
