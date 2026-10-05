"""Hold a batch, continue it later, and survive a power cut.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_held_batches.py

Drives the real API endpoint functions and the real scan session against a
throwaway SQLite database and output folder — nothing touches the device's
Postgres, its data folders or the belt (conveyor commands are stubbed).
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.mkdtemp(), "held.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

import numpy as np  # noqa: E402

from app.core.config import settings  # noqa: E402

settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.DETECTION_SETTLE_SECONDS = 0.0
# The belt-start grace would otherwise suppress detection for the first
# seconds of every run started here; these tests feed frames immediately.
settings.DETECTION_START_GRACE_SECONDS = 0.0
settings.DETECTION_SAMPLE_FRAMES = 3
settings.RAW_FRAME_EVERY = 1
settings.FM_FRAMES_ENABLED = True
settings.FM_FULL_FRAMES_ENABLED = True
settings.REJECTED_SAVE_ENABLED = True

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
            analysis_parameters=["Stones", "Paper"]), db)
    finally:
        db.close()


def review(lanes, names):
    """Show `lanes` as one review screen, label each, then submit the review."""
    s.process_frame(frame, [box(x, 400) for x in lanes])
    for _ in range(3):
        s.process_frame(frame, [box(x, 400) for x in lanes])
    assert len(s.pending) == len(lanes), (len(s.pending), s.review_phase)
    indices = [p["index"] for p in s.pending]
    for p, name in zip(list(s.pending), names):
        api.label_detection(api.LabelRequest(index=p["index"], fm_name=name))
    api.resume_scan()
    return indices


def clean_frames(n):
    # After a review is submitted detection waits for Start (existing
    # behaviour); pressing Start is what lets clean-belt frames be kept.
    s.detection_suspended = False
    for _ in range(n):
        s.process_frame(frame, [])
    s.flush_raw_frames()


def submit():
    """What /submit does, minus the datagram (not under test here)."""
    s.stop_belt_manually()
    status_before = s.status()
    payload = s.finish(0, 0)
    api._checkpoint(awaiting_save=True)
    api._pending_submission = {"status_before": status_before, "result_payload": payload}
    return payload


def crops(folder):
    return sorted(n for n in os.listdir(folder) if n.endswith(".png"))


def row(folder):
    db = SessionLocal()
    try:
        return db.query(ScanProgress).filter(ScanProgress.folder_name == folder).first()
    finally:
        db.close()


# 1. A batch: two objects reviewed, some clean belt, Submit, then Hold.
api.reset_scan()
start("B1")
b1_folder, b1_out, b1_frames = s.folder_name, s.output_folder, s.output_frame_folder
first = review([200, 500], ["Stones", "Paper"])
clean_frames(3)
r_before = sorted(n for n in os.listdir(b1_frames) if n.startswith("r_frame_"))
check("checkpoint: row exists while scanning", row(b1_folder).status == "active")
p1 = submit()
check("first submit counts 2", p1["total_fo_detected"] == 2, p1)
api.hold_pending()
check("hold: row is held and marked submitted",
      row(b1_folder).status == "held" and row(b1_folder).awaiting_save)
check("hold: session freed for the next batch", s.folder_name == "" and not s.active)
check("hold: results-page submission cleared", api._pending_submission is None)
check("hold: nothing moved", crops(b1_out) and len(crops(b1_out)) == 2)
listing = api.list_held()["batches"]
check("held list shows it", len(listing) == 1 and listing[0]["sample_id"] == "B1"
      and listing[0]["fm_count"] == 2 and listing[0]["submitted"], listing)

# 2. Another batch runs and is cancelled in between — B1 is untouched.
api.reset_scan()
start("B2")
b2_folder, b2_out = s.folder_name, s.output_folder
review([300], ["Stones"])
api.cancel_scan()
check("other batch: cancelled one is discarded", row(b2_folder).status == "discarded")
check("other batch: held one still held", row(b1_folder).status == "held")

# 3. Power cut mid-scan: B3 is active when the process dies.
api.reset_scan()
start("B3")
b3_folder, b3_out = s.folder_name, s.output_folder
review([700], ["Paper"])
s.reset()                                   # the process is gone; nothing else ran
scan_progress.interrupt_all_active()        # what startup does
check("power cut: active batch becomes interrupted", row(b3_folder).status == "interrupted")
check("power cut: its hold starts at the last checkpoint, not at boot",
      row(b3_folder).held_at is not None)
open_ids = {b["sample_id"]: b["id"] for b in api.list_held()["batches"]}
check("held list: held + interrupted", set(open_ids) == {"B1", "B3"}, open_ids)

# 4. S3 cleanup must leave open batches alone.
protected = set(scan_progress.open_folders())
check("S3: held and interrupted folders protected",
      {b1_out, b1_frames, b3_out} <= protected, protected)
check("S3: discarded batch not protected", b2_out not in protected)

# 5. Pretend B1 sat on hold for two days, then continue it.
db = SessionLocal()
r = db.query(ScanProgress).filter(ScanProgress.folder_name == b1_folder).first()
r.held_at = datetime.now() - timedelta(days=2)
db.commit()
db.close()

res = api.resume_held(open_ids["B1"])
check("continue: session restored", s.active and s.folder_name == b1_folder)
check("continue: live FM count carries on", res["total_fo_detected"] == 2, res)
check("continue: numbering continues past existing crops",
      s._next_pending_index >= max(first) + 1, s._next_pending_index)
check("continue: detection waits for Start", s.detection_suspended)
check("continue: row active again", row(b1_folder).status == "active")
check("continue: two days on hold recorded",
      row(b1_folder).total_held_seconds >= 2 * 86400, row(b1_folder).total_held_seconds)

try:
    api.resume_held(open_ids["B3"])
    check("continue: refused while another batch is open", False)
except Exception as exc:
    check("continue: refused while another batch is open", getattr(exc, "status_code", 0) == 409)

# 6. Start again and scan more into the same batch.
start("B1")
check("start continues the same batch", s.folder_name == b1_folder and not s.detection_suspended)
second = review([1100], ["Stones"])
check("new object gets a fresh index", second[0] not in first, (first, second))
check("earlier crops still there", len(crops(b1_out)) == 3, crops(b1_out))
clean_frames(3)
r_after = sorted(n for n in os.listdir(b1_frames) if n.startswith("r_frame_"))
check("r_frame files added, none overwritten",
      len(r_after) == len(r_before) + 3 and set(r_before) <= set(r_after), (r_before, r_after))

# 7. Final Submit counts everything, once.
p2 = submit()
check("final submit counts all 3", p2["total_fo_detected"] == 3, p2)
check("final counts by type", p2["result"].get("Stones") == 2 and p2["result"].get("Paper") == 1,
      p2["result"])
check("hold did not add a manual stop", p2["looker_data"]["Manual Stop Count"] == 0,
      p2["looker_data"])
check("two days on hold not counted as stop time",
      p2["looker_data"]["Total Stop Time"] < 60, p2["looker_data"])
scan_progress.mark_saved(b1_folder)                      # what /confirm does
check("saved: row saved and off the list",
      row(b1_folder).status == "saved"
      and "B1" not in {b["sample_id"] for b in api.list_held()["batches"]})

# 8. Discard the interrupted one from the list.
api._pending_submission = None
s.reset()
api.discard_held(open_ids["B3"])
check("discard: crops moved to rejected/", crops(b3_out) == []
      and os.path.isdir(os.path.join(settings.OUTPUT_DIR, "rejected", "Urad White", "V1", b3_folder)))
check("discard: off the list", api.list_held()["batches"] == [])

# 9. Reloading the scan page mid-batch keeps that batch instead of losing it.
api.reset_scan()
start("B4")
b4_folder = s.folder_name
review([900], ["Stones"])
api.reset_scan()
check("reload: unfinished batch kept as interrupted", row(b4_folder).status == "interrupted")

s.stop_raw_frame_writer()
print(f"\nAll {passed} checks passed.")
