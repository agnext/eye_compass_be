"""The startup prompt for a batch cut off by a power cut.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_interrupt_notice.py

A batch that was still being scanned when the device lost power is marked
interrupted at the next startup and listed under Held Batches. On top of that,
Home asks once whether to continue it now — this covers that prompt: who it is
raised for, who it is NOT raised for, and that it is only ever raised once.

Drives the real API endpoint functions and the real scan session against a
throwaway SQLite database and output folder — nothing touches the device's
Postgres, its data folders or the belt (conveyor commands are stubbed).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.mkdtemp(), "notice.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

import numpy as np  # noqa: E402

from app.core.config import settings  # noqa: E402

settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.DETECTION_SETTLE_SECONDS = 0.0
settings.DETECTION_START_GRACE_SECONDS = 0.0
settings.DETECTION_SAMPLE_FRAMES = 3
settings.RAW_FRAME_EVERY = 1

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models import schema  # noqa: E402,F401
from app.models.schema import ScanProgress  # noqa: E402

assert str(engine.url).startswith("sqlite"), f"refusing to test against {engine.url}"
Base.metadata.create_all(bind=engine)

from app.services.conveyor_service import conveyor_service  # noqa: E402

conveyor_service.send = lambda cmd, max_retries=None: True   # no serial port in a test

from app.api import scan as api  # noqa: E402
from app.services import scan_progress  # noqa: E402
from app.services.scan_session import scan_session as s  # noqa: E402

frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
box = lambda x, y: [float(x), float(y), float(x + 40), float(y + 40), 0.6, 2]

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


def start(sample):
    db = SessionLocal()
    try:
        return api.start_scan(api.ScanStartRequest(
            sample_id=sample, commodity="Urad White", variety="V1",
            analysis_parameters=["Stones"]), db)
    finally:
        db.close()


def power_cut():
    """The backend dies and comes back: the session is gone, the row is not."""
    s.reset()
    s.active = False
    return scan_progress.interrupt_all_active()


def notice():
    return api.interrupted_notice()["batch"]


# ------------------------------------------------------------------ nothing
check("no prompt on a clean device", notice() is None)

# --------------------------------------------------- a batch is cut off
start("T1NOTICE")
s.process_frame(frame, [box(500, 400)])
for _ in range(3):
    s.process_frame(frame, [box(500, 400)])
api.label_detection(api.LabelRequest(index=s.pending[0]["index"], fm_name="Stones"))
api.resume_scan()
folder_one = s.folder_name

check("cut off while scanning -> one row interrupted", power_cut() == 1)

n = notice()
check("the interrupted batch is announced", n is not None)
check("it is the right batch", n["sample_id"] == "T1NOTICE", n)
check("it is marked interrupted", n["status"] == "interrupted", n["status"])
check("it carries what was already saved", n["fm_count"] == 1, n["fm_count"])
check("it knows nothing else is waiting", n["others"] == 0, n["others"])
check("its images are still on the device", n["files_present"] is True)

# ------------------------------------------------------- announced only once
check("still announced before it is acknowledged", notice() is not None)
api.ack_interrupted_notice(n["id"])
check("acknowledged -> no longer announced", notice() is None)
check("but still listed under Held Batches",
      [b["id"] for b in scan_progress.list_open()] == [n["id"]])

# A second restart must not reopen a prompt the operator has already dealt
# with — interrupt_all_active only touches rows that are still `active`.
check("a later restart does not re-announce it", power_cut() == 0 and notice() is None)

# -------------------------------------------- a held batch is never announced
start("T2HELD")
s.process_frame(frame, [box(600, 400)])
for _ in range(3):
    s.process_frame(frame, [box(600, 400)])
api.label_detection(api.LabelRequest(index=s.pending[0]["index"], fm_name="Stones"))
api.resume_scan()
s.stop_belt_manually()
status_before = s.status()
payload = s.finish(0, 0)
api._checkpoint(awaiting_save=True)
api._pending_submission = {"status_before": status_before, "result_payload": payload}
api.hold_pending()
check("a deliberately held batch is listed",
      any(b["status"] == "held" for b in scan_progress.list_open()))
check("a deliberately held batch is NOT announced", notice() is None)

# ------------------------- abandoning a batch for another is not announced
start("T3ABANDON")
s.process_frame(frame, [box(700, 400)])
for _ in range(3):
    s.process_frame(frame, [box(700, 400)])
api.label_detection(api.LabelRequest(index=s.pending[0]["index"], fm_name="Stones"))
api.resume_scan()
abandoned = s.folder_name
s.reset()
s.active = False
start("T4NEXT")       # checkpoint marks T3 interrupted — the operator did this
check("abandoning a batch marks it interrupted",
      any(b["sample_id"] == "T3ABANDON" and b["status"] == "interrupted"
          for b in scan_progress.list_open()))
check("abandoning a batch is NOT announced — the operator was there",
      notice() is None, notice())

# ------------------------------------ suppressed while a scan is in progress
# T4NEXT is live. Cut it off, then ask while a scan happens to be active.
s.process_frame(frame, [box(800, 400)])
for _ in range(3):
    s.process_frame(frame, [box(800, 400)])
api.label_detection(api.LabelRequest(index=s.pending[0]["index"], fm_name="Stones"))
api.resume_scan()
check("cut off again -> announced", power_cut() == 1 and notice() is not None)

start("T5LIVE")
check("nothing is announced while a scan is in progress",
      api.interrupted_notice()["batch"] is None)
s.reset()
s.active = False
check("announced again once that scan is over", notice() is not None)

# --------------------------------------------- the latest one, and only one
# Two interrupted batches waiting. The prompt names the most recent; the rest
# are a count, because a prompt listing several is a prompt nobody reads.
n = notice()
check("the latest cut-off batch is the one announced",
      n["sample_id"] == "T4NEXT", n["sample_id"])
check("the others are counted, not listed", n["others"] >= 1, n["others"])
check("every open batch is in the list, announced or not",
      len(scan_progress.list_open()) == n["others"] + 1)

# ---------------------------------- continuing it counts as being told
resumed = api.resume_held(n["id"])
check("continuing loads the batch back in", s.active and s.sample_id == "T4NEXT")
check("continuing acknowledges the prompt", notice() is None)
db = SessionLocal()
try:
    row = db.query(ScanProgress).filter(ScanProgress.id == n["id"]).first()
    check("the acknowledgement is recorded in the database",
          row.interrupt_notified is True)
finally:
    db.close()

# ------------------------------- a batch whose images are gone is not offered
s.reset()
s.active = False
start("T6GONE")
s.process_frame(frame, [box(900, 400)])
for _ in range(3):
    s.process_frame(frame, [box(900, 400)])
api.label_detection(api.LabelRequest(index=s.pending[0]["index"], fm_name="Stones"))
api.resume_scan()
gone_folder = s.output_folder
power_cut()
check("it would be announced while its images are there",
      notice()["sample_id"] == "T6GONE")

import shutil  # noqa: E402
shutil.rmtree(gone_folder)
n = notice()
check("a batch whose images are gone is not offered for continuing",
      n is None or n["sample_id"] != "T6GONE", n)
check("it is still listed, so it can be discarded",
      any(b["sample_id"] == "T6GONE" for b in scan_progress.list_open()))

print(f"\nAll {passed} checks passed.")
