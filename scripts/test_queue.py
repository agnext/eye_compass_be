"""Detection queue drain (ScanSession.detection_queue).

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_queue.py

Walks the four states the review screen moves through: first detection shown,
second one queued behind it rather than overwriting it, Submit promoting the
queued one while staying frozen and interlocked, and Submit on an empty queue
handing back the live view.
"""
import sys, tempfile
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
from app.services.scan_session import ScanSession
from app.services.conveyor_service import conveyor_service

s = ScanSession()
s.start("T1TEST", "toor", "", analysis_parameters=["Stones"])
frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
box = lambda x, y: [x, y, x + 40, y + 40, 0.9, 1]

st = s.process_frame(frame, [box(300, 400)])
assert st["fm_detected"] is True and st["queue_depth"] == 0 and len(s.pending) == 1, st
first_seq = s.frozen_frame_seq
print("1. first detection on screen: pending=%d queue=%d" % (len(s.pending), st["queue_depth"]))

st = s.process_frame(frame, [box(300, 430), box(900, 350)])
assert st["queue_depth"] == 1, st
assert len(s.pending) == 1, "the on-screen detection was overwritten"
assert s.frozen_frame_seq == first_seq, "frozen frame changed while still under review"
print("2. second detection queued behind it: pending=%d queue=%d" % (len(s.pending), st["queue_depth"]))

res = s.resume()
assert res["queue_depth"] == 0 and len(s.pending) == 1, res
assert s.pending[0]["box"][0] > 800, "the queued box is not the second object"
assert s.frozen_frame_seq == first_seq + 1, "stream was not told the frame changed"
assert s.capture_paused is True, "live feed resumed while a queued detection is up"
assert conveyor_service.machine_start_locked is True, "interlock released mid-backlog"
assert s.detection_suspended is False, "detection suspended while backlog remains"
assert [p["index"] for p in s.pending] == [1], s.pending
print("3. submit -> next queued shown: pending=%d queue=%d indices=%s"
      % (len(s.pending), res["queue_depth"], [p["index"] for p in s.pending]))

res = s.resume()
assert res["queue_depth"] == 0 and s.pending == [] and s.pending_frame is None
assert s.capture_paused is False, "live feed did not return"
assert conveyor_service.machine_start_locked is False, "interlock still engaged"
assert s.detection_suspended is True
print("4. submit -> backlog drained, live view restored")

# --- the backlog is bounded, and nothing is lost when it fills --------------
from app.core.config import settings as _s
s2 = ScanSession()
s2.start("T1CAP", "toor", "", analysis_parameters=["Stones"])
_s.DETECTION_QUEUE_MAX = 3
# Each object in its own lane so none is filtered as a duplicate of another.
for lane in range(10):
    s2.process_frame(frame, [box(200 + lane * 150, 300)])
assert len(s2.detection_queue) <= _s.DETECTION_QUEUE_MAX, len(s2.detection_queue)
held = 10 - (len(s2.detection_queue) + 1)          # +1 for the one on screen
assert held > 0, "the cap never engaged, so this proves nothing"
# The objects that could not be queued must NOT be recorded as counted, or
# they could never be detected again.
assert len(s2.counted_track_ids) == len(s2.detection_queue) + 1, (
    "objects refused entry to the backlog were counted anyway: %s counted vs "
    "%s shown" % (len(s2.counted_track_ids), len(s2.detection_queue) + 1))
print("5. backlog capped at %d; %d object(s) held back and left uncounted"
      % (_s.DETECTION_QUEUE_MAX, held))

# --- the backlog is drained oldest-first ------------------------------------
# Three objects found in turn, each in its own lane so none filters another.
# The operator must meet them in the order they were found, not in reverse.
s3 = ScanSession()
s3.start("T1ORDER", "toor", "", analysis_parameters=["Stones"])
_s.DETECTION_QUEUE_MAX = 20
lanes = [300, 900, 1500]
for lane in lanes:
    s3.process_frame(frame, [box(lane, 300)])
assert len(s3.detection_queue) == 2, len(s3.detection_queue)

seen = [s3.pending[0]["box"][0]]
while s3.detection_queue:
    s3.resume()
    s3.start("T1ORDER", "toor", "")
    seen.append(s3.pending[0]["box"][0])
# Boxes are padded by 20px, so compare the lane each one came from.
assert seen == [lane - 20 for lane in lanes], (
    "backlog was not drained in the order the objects were found: %s" % seen)
print("6. backlog drained oldest-first: lanes %s" % [int(x) + 20 for x in seen])

# --- boxes within one frozen frame follow the belt, not model confidence ----
s4 = ScanSession()
s4.start("T1BOXORDER", "toor", "", analysis_parameters=["Stones"])
# Emitted low-confidence-last, as NMS does; the middle one is furthest along.
s4.process_frame(frame, [
    box(300, 200) + [],          # near the top, entered view most recently
    box(900, 900),               # furthest down the belt, entered first
    box(1500, 550),              # in between
])
tops = [p["box"][1] for p in s4.pending]
assert tops == sorted(tops, reverse=True), (
    "boxes are not ordered down the belt: %s" % tops)
print("7. boxes within a frame ordered furthest-along-first: y = %s"
      % [int(t) for t in tops])
print("ALL QUEUE CHECKS PASSED")
