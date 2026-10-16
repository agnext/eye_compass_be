"""Stop delay: every object that crosses the view while the belt runs on is shown.

    USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_stop_delay_escapes.py

USE_MOCK_CAMERA=true matters: it swaps in the mock conveyor. Without it this
opens the real /dev/ttyTHS0 and sends FM_detected to the production belt.

Reproduces 16 Oct 13:27. With STOP_COMMAND_DELAY_SECONDS the belt keeps
running after the first sighting, so objects travel ~120px per inferred frame
and usually jump past the tracker's 50px exit band. They were evicted as
"unseen" instead, which _note_escapes ignored — three objects went past and
only the one that stopped the belt was shown (twice: once as the escape, once
as the trigger-frame fallback).
"""
import os, sys, tempfile, time
sys.path.insert(0, '.')
if os.getenv("USE_MOCK_CAMERA", "").lower() != "true":
    sys.exit("refusing to run: set USE_MOCK_CAMERA=true (real conveyor otherwise)")
import numpy as np
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.DETECTION_START_GRACE_SECONDS = 0.0
settings.DETECTION_SAMPLE_FRAMES = 3
settings.STOP_COMMAND_DELAY_SECONDS = 1.0   # as on the device
settings.DETECTION_SETTLE_SECONDS = 0.2
from app.services.scan_session import ScanSession

H, W = 1200, 1920
frame = np.zeros((H, W, 3), dtype=np.uint8)
STEP = 120          # px per inferred frame, as measured in the 13:27 log
TICK = 0.05         # ~20 inferred frames per second
LAST_Y = 1060       # last top-y the model still finds it at — never reaches the exit band

def box(x, y):
    return [float(x), float(y), float(x + 40), float(y + 40), 0.9, 2]

def run(name, objects):
    """Belt runs on for the stop delay, then freezes where it is.

    objects: {label: (lane x, frame it enters)}. Returns the lane x of every
    box shown, across every screen, in the order shown.
    """
    def visible(i):
        out = []
        for x, first in objects.values():
            y = 10 + (i - first) * STEP
            if i >= first and y <= LAST_Y:
                out.append(box(x, y))
        return out

    s = ScanSession()
    s.start("T1" + name, "toor", "", analysis_parameters=["Stones"])
    s.process_frame(frame, visible(0))
    assert s.review_phase == "settling", s.review_phase
    stop_at = time.monotonic() + settings.STOP_COMMAND_DELAY_SECONDS
    i, frozen = 1, None
    deadline = time.monotonic() + 6.0
    while not s.pending and time.monotonic() < deadline:
        time.sleep(TICK)
        if frozen is None and time.monotonic() >= stop_at:
            frozen = i          # the stop has landed: nothing moves now
        s.process_frame(frame, visible(frozen if frozen is not None else i))
        i += 1
    assert s.pending, "%s: no review screen at all" % name
    screens = []
    while s.pending:
        screens.append(sorted(int(p["raw_box"][0]) for p in s.pending))
        s.resume()
    return screens


# 13:27 replayed: all three cross the whole view before the stop lands.
screens = run("GONE", {"A": (200, 0), "B": (600, 4), "C": (1000, 8)})
print("1. all three leave before the stop — screens by lane x:", screens)
flat = sorted(x for sc in screens for x in sc)
assert flat == [200, 600, 1000], (
    "expected each of A, B, C exactly once, got %s" % flat)

# One still under the camera when the belt stops: it must be on the main
# screen from the stationary frames, the other two queued, nobody twice.
screens = run("PARKED", {"A": (200, 0), "B": (600, 6), "C": (1000, 19)})
print("2. one still in view when it stops — screens by lane x:", screens)
flat = sorted(x for sc in screens for x in sc)
assert flat == [200, 600, 1000], (
    "expected each of A, B, C exactly once, got %s" % flat)
assert 1000 in screens[0], "the parked object should be on the main screen"
print("ALL STOP-DELAY ESCAPE CHECKS PASSED")
