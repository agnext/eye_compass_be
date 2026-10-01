#!/usr/bin/env python3
"""Tests for the S3 upload/verify/delete worker.

No network and no real bucket: FakeS3 implements just head_object/put_object,
and checks a supplied ChecksumSHA256 against the body the way S3 does.

The schedule state is real, in a throwaway SQLite database per case, using the
same S3UploadState model the device uses — so the scheduling tests exercise the
actual queries rather than a stand-in.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the app at a throwaway SQLite file before app.core.database builds its
# engine, so nothing here can touch the device's Postgres.
_DB_FILE = os.path.join(tempfile.mkdtemp(), "s3_state_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"

from botocore.exceptions import ClientError  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models.schema import S3UploadState  # noqa: E402
from app.services import s3_worker  # noqa: E402
from app.services.s3_worker import S3UploaderTask  # noqa: E402

assert str(engine.url).startswith("sqlite"), f"refusing to test against {engine.url}"
Base.metadata.create_all(bind=engine)


def set_state(**fields):
    """Write the schedule row directly, as if a previous run had left it."""
    db = SessionLocal()
    try:
        db.query(S3UploadState).delete()
        if fields:
            db.add(S3UploadState(id=1, **fields))
        db.commit()
    finally:
        db.close()


def get_state():
    db = SessionLocal()
    try:
        return db.query(S3UploadState).filter(S3UploadState.id == 1).first()
    finally:
        db.close()


def sha(b):
    return base64.b64encode(hashlib.sha256(b).digest()).decode()


class FakeS3:
    def __init__(self):
        self.objects = {}          # key -> (bytes, checksum or None)
        self.fail_puts = False     # network down
        self.corrupt = False       # store different bytes than received
        self.puts = 0

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        data, cs = self.objects[Key]
        out = {"ContentLength": len(data)}
        if ChecksumMode == "ENABLED" and cs:
            out["ChecksumSHA256"] = cs
        return out

    def put_object(self, Bucket, Key, Body, ChecksumAlgorithm=None, ChecksumSHA256=None):
        if self.fail_puts:
            raise ConnectionError("Could not connect to the endpoint URL")
        data = Body.read()
        if ChecksumSHA256 and sha(data) != ChecksumSHA256:
            raise ClientError({"Error": {"Code": "BadDigest"}}, "PutObject")
        self.puts += 1
        stored = data + b"x" if self.corrupt else data
        self.objects[Key] = (stored, sha(stored))


def make_tree(root, age_minutes=120):
    """Two batches in output/, one in output_frame/ (with fm/), one DC run."""
    files = {
        "output/urad/v1/B1_1/NON-FM_1_0.png": b"crop-a",
        "output/urad/v1/B1_1/result.json": b"{}",
        "output/urad/v1/B2_2/FM_2_0.png": b"crop-b",
        "output_frame/urad/v1/B1_1/r_frame_0.jpg": b"frame0",
        "output_frame/urad/v1/B1_1/fm/frame_3.png": b"fm3",
        "output_frame/urad/v1/B1_1/fm/frame_3.txt": b"1 0.5 0.5 0.1 0.1\n",
        "Data_Collection/urad/v1/170_S1/S1_a.png": b"dc-a",
    }
    old = time.time() - age_minutes * 60
    for rel, data in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    for dirpath, dirs, fs in os.walk(root):
        for n in dirs + fs:
            os.utime(os.path.join(dirpath, n), (old, old))
    return files


def fresh(age_minutes=120):
    root = tempfile.mkdtemp()
    settings.OUTPUT_DIR = root
    settings.S3_DELETE_AFTER_UPLOAD = True
    settings.S3_MIN_AGE_MINUTES = 30
    settings.S3_RETENTION_DAYS = 0        # per-case; the retention tests set their own
    settings.HISTORY_WINDOW_DAYS = 0      # ditto: 0 = output/ gets no extra window
    settings.S3_UPLOAD_EVERY_DAYS = 3
    files = make_tree(root, age_minutes)
    set_state()
    task = S3UploaderTask()
    task._client = FakeS3()
    task._key_prefix = lambda: "pre/"
    S3UploaderTask._busy = staticmethod(lambda: None)
    S3UploaderTask._protected_dirs = staticmethod(lambda: set())
    return root, files, task


def remaining(root):
    return sorted(
        os.path.relpath(os.path.join(d, f), root)
        for d, _, fs in os.walk(root) for f in fs
    )


passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


# 1. Happy path: everything uploaded, verified byte-for-byte, deleted, folders gone.
root, files, task = fresh()
s = task.run_once()
check("happy: complete", s["complete"], s)
check("happy: all uploaded", s["uploaded"] == len(files), s)
check("happy: every S3 copy identical to the original",
      all(task._client.objects["pre/" + k][0] == v for k, v in files.items()))
check("happy: device emptied", remaining(root) == [], remaining(root))
check("happy: session folders removed, parents kept",
      not os.path.exists(os.path.join(root, "output/urad/v1/B1_1"))
      and os.path.isdir(os.path.join(root, "output/urad/v1")))
check("happy: Data_Collection uploaded",
      "pre/Data_Collection/urad/v1/170_S1/S1_a.png" in task._client.objects)

# 2. Network down: nothing deleted, run marked incomplete, stops early.
root, files, task = fresh()
task._client.fail_puts = True
s = task.run_once()
check("offline: nothing deleted", len(remaining(root)) == len(files), remaining(root))
check("offline: incomplete", not s["complete"])
check("offline: stopped after consecutive failures",
      s["failed"] == s3_worker.MAX_CONSECUTIVE_FAILURES, s)

# 3. S3 stores something different from what was sent: read-back check keeps the file.
root, files, task = fresh()
task._client.corrupt = True
s = task.run_once()
check("corrupt store: nothing deleted", len(remaining(root)) == len(files), s)
check("corrupt store: all flagged not verified", s["not_verified"] == len(files), s)

# 4. Already on S3 with matching checksum: no re-upload, still deleted.
root, files, task = fresh()
for rel, data in files.items():
    task._client.objects["pre/" + rel] = (data, sha(data))
s = task.run_once()
check("already there: no upload", task._client.puts == 0 and s["already_there"] == len(files), s)
check("already there: deleted", remaining(root) == [])

# 5. On S3 from the old uploader (same size, no checksum): re-uploaded, not trusted by size.
root, files, task = fresh()
for rel, data in files.items():
    task._client.objects["pre/" + rel] = (data, None)
s = task.run_once()
check("legacy object: re-uploaded with checksum", task._client.puts == len(files), s)

# 5b. On S3 with same size but different content: re-uploaded, never deleted on size alone.
root, files, task = fresh()
for rel, data in files.items():
    wrong = bytes(len(data))
    task._client.objects["pre/" + rel] = (wrong, sha(wrong))
s = task.run_once()
check("same-size different content: overwritten with the real file",
      all(task._client.objects["pre/" + k][0] == v for k, v in files.items()))

# 6. Delete disabled: uploaded, verified, all kept.
root, files, task = fresh()
settings.S3_DELETE_AFTER_UPLOAD = False
s = task.run_once()
check("keep: uploaded", s["uploaded"] == len(files))
check("keep: nothing deleted", len(remaining(root)) == len(files))

# 7. Folder in use (active batch) is skipped entirely.
root, files, task = fresh()
active = os.path.realpath(os.path.join(root, "output_frame/urad/v1/B1_1"))
S3UploaderTask._protected_dirs = staticmethod(lambda: {active})
s = task.run_once()
left = remaining(root)
check("in use: active folder untouched",
      left == sorted(k for k in files if k.startswith("output_frame/urad/v1/B1_1")), left)
check("in use: counted", s["skipped_in_use"] == 1, s)

# 8. Folder written to in the last S3_MIN_AGE_MINUTES is skipped.
root, files, task = fresh(age_minutes=5)
s = task.run_once()
check("min age: nothing touched",
      len(remaining(root)) == len(files) and s["skipped_within_retention"] == 4, s)

# 8b. Retention window: the last N days always stay; older data goes.
root, files, task = fresh(age_minutes=60 * 24 * 2)     # everything 2 days old
settings.S3_RETENTION_DAYS = 3
s = task.run_once()
check("retention: 2-day-old data kept with a 3-day window",
      len(remaining(root)) == len(files) and s["skipped_within_retention"] == 4, s)
check("retention: nothing uploaded either", s["uploaded"] == 0, s)

root, files, task = fresh(age_minutes=60 * 24 * 4)     # everything 4 days old
settings.S3_RETENTION_DAYS = 3
s = task.run_once()
check("retention: 4-day-old data uploaded and removed",
      s["complete"] and remaining(root) == [], s)

# 8c. A mix: only what is past the window moves.
root, files, task = fresh(age_minutes=60 * 24 * 10)    # all old...
settings.S3_RETENTION_DAYS = 3
recent = os.path.join(root, "output/urad/v1/B2_2")     # ...except this batch
os.utime(os.path.join(recent, "FM_2_0.png"), None)
os.utime(recent, None)
s = task.run_once()
check("retention mix: only the recent batch is left",
      remaining(root) == ["output/urad/v1/B2_2/FM_2_0.png"], remaining(root))
check("retention mix: the rest uploaded", s["uploaded"] == len(files) - 1, s)

# 8e. output/ follows HISTORY_WINDOW_DAYS, the other trees do not — History
#     must never list a batch whose crops have already been removed.
root, files, task = fresh(age_minutes=60 * 24 * 10)    # everything 10 days old
settings.S3_RETENTION_DAYS = 3
settings.HISTORY_WINDOW_DAYS = 30
s = task.run_once()
left = remaining(root)
check("history window: output/ crops kept for the whole 30 days",
      sorted(k for k in files if k.startswith("output/")) == left, left)
check("history window: output_frame/ and Data_Collection/ still went",
      not any(k.startswith(("output_frame/", "Data_Collection/")) for k in left), left)

root, files, task = fresh(age_minutes=60 * 24 * 40)    # older than both windows
settings.S3_RETENTION_DAYS = 3
settings.HISTORY_WINDOW_DAYS = 30
s = task.run_once()
check("history window: past 30 days, output/ goes too", remaining(root) == [], s)

# 8f. HISTORY_WINDOW_DAYS=0 means History shows everything, which no window can
#     cover — output/ falls back to the plain retention rather than staying for ever.
root, files, task = fresh(age_minutes=60 * 24 * 10)
settings.S3_RETENTION_DAYS = 3
settings.HISTORY_WINDOW_DAYS = 0
check("history window off: output/ uses the plain retention",
      S3UploaderTask.retention_days("output") == 3)
s = task.run_once()
check("history window off: everything goes", remaining(root) == [], s)
settings.HISTORY_WINDOW_DAYS = 0

# 8d. Retention 0 with the minute floor still guards a folder in progress.
root, files, task = fresh(age_minutes=1)
settings.S3_RETENTION_DAYS = 0
s = task.run_once()
check("retention 0: min-age floor still applies", len(remaining(root)) == len(files), s)
settings.S3_RETENTION_DAYS = 0

# 9. Scan starts mid-run: stops between files, keeps the rest, incomplete.
root, files, task = fresh()
calls = {"n": 0}
def busy_after_two():
    calls["n"] += 1
    return "a batch scan is running" if calls["n"] > 2 else None
S3UploaderTask._busy = staticmethod(busy_after_two)
s = task.run_once()
check("deferred: stopped after 2 files", s["uploaded"] == 2 and not s["complete"], s)
check("deferred: rest kept", len(remaining(root)) == len(files) - 2)

# 10. File changes between upload and delete: kept.
root, files, task = fresh()
orig_put = task._client.put_object
target = os.path.join(root, "output/urad/v1/B2_2/FM_2_0.png")
def put_then_touch(**kw):
    orig_put(**kw)
    if kw["Key"].endswith("FM_2_0.png"):
        with open(target, "ab") as fh:
            fh.write(b"more")
task._client.put_object = put_then_touch
s = task.run_once()
check("changed after upload: kept", os.path.exists(target), s)

# 11. Schedule: first start only records; due after 3 days; clock going backwards runs.
root, files, task = fresh()
check("schedule: first tick does not run", task.tick() is None and len(remaining(root)) == len(files))
check("schedule: first_seen row created", get_state() is not None and get_state().first_seen_at is not None)
check("schedule: second tick still not due", task.tick() is None)
set_state(first_seen_at=datetime.now() - timedelta(days=3, minutes=1))
s_ = task.tick()
check("schedule: runs after 3 days", s_ is not None and s_["complete"], s_)
check("schedule: last_completed recorded", get_state().last_completed_at is not None)
check("schedule: summary kept in the row", get_state().last_result["deleted"] == len(files))
check("schedule: not due right after", task.tick() is None)
set_state(first_seen_at=datetime.now(), last_completed_at=datetime.now() + timedelta(days=400))
check("schedule: clock jumped back -> runs", task.tick() is not None)

# 12. Incomplete run does not reset the 3-day clock; retried at next check.
root, files, task = fresh()
past = datetime.now() - timedelta(days=4)
set_state(first_seen_at=past, last_completed_at=past)
task._client.fail_puts = True
task.tick()
check("retry: failed run leaves the clock alone", get_state().last_completed_at == past)
check("retry: attempt still recorded", get_state().last_attempt_at is not None)
task._client.fail_puts = False
s_ = task.tick()
check("retry: next check runs and completes", s_ is not None and s_["complete"] and remaining(root) == [])
check("retry: clock moved on", get_state().last_completed_at > past)

# 13. Busy at the scheduled time: put off, nothing touched.
root, files, task = fresh()
set_state(first_seen_at=past)
S3UploaderTask._busy = staticmethod(lambda: "data collection is recording")
check("busy: put off", task.tick() is None and len(remaining(root)) == len(files))
S3UploaderTask._busy = staticmethod(lambda: None)

# 14. Database unreadable: skip the check entirely, never treat it as "due".
root, files, task = fresh()
set_state(first_seen_at=past)
real_load = S3UploaderTask._load_state
S3UploaderTask._load_state = staticmethod(lambda: {"__unreadable__": True})
check("db down: does not run, nothing deleted",
      task.tick() is None and len(remaining(root)) == len(files))
S3UploaderTask._load_state = staticmethod(real_load)
check("db back: runs", task.tick() is not None and remaining(root) == [])

print(f"\nAll {passed} checks passed.")
