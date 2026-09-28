"""Stop, settle, look — one review screen per group of objects.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_settle.py

Reproduces the 28 Sep case where five objects sitting together on the belt
reached the operator as 5 boxes, then 1, then 4 across three screens: the model
does not find the same set in every frame, and the first frame was being frozen
immediately.
"""
import sys, tempfile, time
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.DETECTION_SETTLE_SECONDS = 0.0      # no real waiting in a test
settings.DETECTION_SAMPLE_FRAMES = 3
from app.services.scan_session import ScanSession
from app.services.conveyor_service import conveyor_service

frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
box = lambda x, y: [float(x), float(y), float(x + 40), float(y + 40), 0.5, 2]
LANES = [200, 500, 800, 1100, 1400]

s = ScanSession()
s.start("T1SETTLE", "toor", "", analysis_parameters=["Stones"])

# Frame 1 — the model finds only three of the five. Belt is told to stop.
st = s.process_frame(frame, [box(l, 400) for l in LANES[:3]])
assert st["fm_detected"] is False, "showed a screen before the belt had stopped"
assert s.review_phase == "settling", s.review_phase
assert conveyor_service.machine_start_locked, "belt was not stopped on sighting"
print("1. sighted 3 objects -> belt stopping, nothing shown yet")

# Stationary frames, each finding a different subset, as the model really does.
s.process_frame(frame, [box(l, 400) for l in LANES[:4]])
s.process_frame(frame, [box(l, 400) for l in (LANES[0], LANES[4])])
st = s.process_frame(frame, [box(l, 400) for l in LANES[1:]])

assert st["fm_detected"] is True, "no review screen after settling"
assert len(s.pending) == 5, (
    "expected all 5 on one screen, got %s" % len(s.pending))
assert s.capture_paused is True, "stream not frozen on the review frame"
assert len(s.detection_queue) == 0, "objects were split across screens again"
print("2. combined 3 stationary frames -> all 5 on ONE screen, queue empty")

# Boxes run down the belt, furthest along first.
tops = [p["box"][1] for p in s.pending]
assert tops == sorted(tops, reverse=True), tops

# Two boxes per object: the padded one the crop is cut from, and the detection
# as the model reported it, which is what the review overlay draws. Drawing the
# padded box makes a 40px object look 80px wide and manufactures overlap
# between objects that never touched.
for p in s.pending:
    bw = p["box"][2] - p["box"][0]
    rw = p["raw_box"][2] - p["raw_box"][0]
    assert round(rw) == 40, "raw_box is not the unpadded detection: %s" % rw
    assert round(bw) == 80, "box is not padded by 20 a side: %s" % bw
print("2b. every object carries both a padded crop box and its true box")

# Submitting releases back to watching, not to another screen.
s.resume()
assert s.pending == [] and s.review_phase == "idle", s.review_phase
assert not conveyor_service.machine_start_locked
print("3. submit -> released, back to watching the belt")

# Nothing found once stopped falls back to the sighting rather than dropping it.
s2 = ScanSession()
s2.start("T1FALLBACK", "toor", "", analysis_parameters=["Stones"])
s2.process_frame(frame, [box(600, 400)])
for _ in range(3):
    st = s2.process_frame(frame, [])
assert len(s2.pending) == 1, "the sighting was dropped when it stopped being found"
print("4. nothing found once stopped -> falls back to the sighting, not dropped")

# Raw frames are written off the loop, and flush waits for them.
s2.flush_raw_frames()
import os
written = os.listdir(s2.output_frame_folder)
assert written, "no raw frame reached disk"
print("5. raw frames written in the background, flush waits: %s" % written)
# An object that leaves the bottom of the frame while the belt is still
# stopping cannot be in any of the stationary frames, so it must be carried
# forward from where it was last seen rather than lost.
s3 = ScanSession()
s3.start("T1ESCAPE", "toor", "", analysis_parameters=["Stones"])
settings.DETECTION_SETTLE_SECONDS = 0.05
s3.process_frame(frame, [box(300, 400), box(900, 1000)])   # two sighted
assert s3.review_phase == "settling"
# The second one reaches the exit zone and is dropped by the tracker...
s3.process_frame(frame, [box(300, 420), box(900, 1160)])
assert s3._escaped, "the departing object was not held on to"
time.sleep(0.06)
# ...and the stationary frames only ever see the one that stayed.
for _ in range(3):
    st = s3.process_frame(frame, [box(300, 430)])

assert len(s3.pending) == 1 and s3.pending[0]["box"][0] < 400, s3.pending
assert len(s3.detection_queue) == 1, (
    "the object that left the view was dropped: queue=%s" % len(s3.detection_queue))
print("6. object that left the view while stopping is held, queued behind")

s3.resume()
assert len(s3.pending) == 1 and s3.pending[0]["box"][0] > 800, (
    "the escaped object was not shown after the main screen: %s" % s3.pending)
print("7. submit -> the escaped object is shown from where it was last seen")
# Including the ones carried forward and the stop-sighting fallback, both of
# which reach the screen by a different route than the stationary frames.
esc = s3.pending[0]
assert round(esc["raw_box"][2] - esc["raw_box"][0]) == 40, esc
assert round(esc["box"][2] - esc["box"][0]) == 80, esc
fb = s2.pending[0]
assert round(fb["raw_box"][2] - fb["raw_box"][0]) == 40, fb
assert round(fb["box"][2] - fb["box"][0]) == 80, fb
print("8. escaped and fallback screens carry both boxes too")
print("ALL SETTLE CHECKS PASSED")
