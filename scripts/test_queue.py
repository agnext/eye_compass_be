"""The detection backlog (ScanSession.detection_queue).

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_queue.py

Since the stop/settle/look cycle was added this is a safety net rather than
the normal path: a sighting stops the belt and everything found once it is
stationary goes on ONE screen, so in ordinary running the backlog stays at 0.
It still has to behave if anything does reach it, which is what this covers —
driven through _on_foreign_matter directly, because process_frame no longer
gets there on its own. `test_settle.py` covers the normal path.
"""
import sys, tempfile
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
from app.services.scan_session import ScanSession
from app.services.conveyor_service import conveyor_service

frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
box = lambda x, y: [float(x), float(y), float(x + 40), float(y + 40), 0.9, 1]

s = ScanSession()
s.start("T1QUEUE", "toor", "", analysis_parameters=["Stones"])

# First detection goes straight to the screen; the next waits behind it.
s._on_foreign_matter(frame, [box(300, 400)])
assert len(s.pending) == 1 and len(s.detection_queue) == 0
first_seq = s.frozen_frame_seq
s._on_foreign_matter(frame, [box(900, 350)])
assert len(s.detection_queue) == 1, len(s.detection_queue)
assert len(s.pending) == 1, "the on-screen detection was overwritten"
assert s.frozen_frame_seq == first_seq, "frozen frame changed while under review"
print("1. second detection waits behind the first, does not replace it")

# Submit promotes the queued one, staying frozen and interlocked.
res = s.resume()
assert res["queue_depth"] == 0 and len(s.pending) == 1
assert s.pending[0]["box"][0] > 800, "the queued detection was not promoted"
assert s.frozen_frame_seq == first_seq + 1, "stream not told the frame changed"
assert s.capture_paused is True, "live feed returned mid-backlog"
assert conveyor_service.machine_start_locked is True, "interlock released early"
assert [p["index"] for p in s.pending] == [1], s.pending
print("2. submit -> queued one shown, still frozen, indices do not restart")

res = s.resume()
assert res["queue_depth"] == 0 and s.pending == [] and s.pending_frame is None
assert s.capture_paused is False and s.review_phase == "idle"
assert conveyor_service.machine_start_locked is False
print("3. submit on an empty backlog -> live view restored")

# Drained oldest-first.
s2 = ScanSession()
s2.start("T1ORDER", "toor", "", analysis_parameters=["Stones"])
lanes = [300, 900, 1500]
for lane in lanes:
    s2._on_foreign_matter(frame, [box(lane, 300)])
seen = [s2.pending[0]["box"][0]]
while s2.detection_queue:
    s2.resume()
    seen.append(s2.pending[0]["box"][0])
assert seen == [float(l) for l in lanes], "not drained in the order found: %s" % seen
print("4. drained oldest-first: lanes %s" % [int(x) for x in seen])

# Bounded, and nothing is lost when it fills.
s3 = ScanSession()
s3.start("T1CAP", "toor", "", analysis_parameters=["Stones"])
settings.DETECTION_QUEUE_MAX = 3
for lane in range(10):
    if len(s3.detection_queue) >= settings.DETECTION_QUEUE_MAX:
        break
    s3._on_foreign_matter(frame, [box(200 + lane * 150, 300)])
assert len(s3.detection_queue) == settings.DETECTION_QUEUE_MAX
print("5. backlog bounded at %d" % settings.DETECTION_QUEUE_MAX)
print("ALL QUEUE CHECKS PASSED")
