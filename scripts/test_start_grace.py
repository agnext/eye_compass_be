"""The belt-start grace — nothing is detected while the belt is still at rest.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_start_grace.py

Reproduces the 1 Oct case: Start is pressed on a new batch, the belt has not
physically moved yet, and an object already lying under the camera is detected
where it sits. The belt is stopped before it ever got going; the stationary
re-look finds nothing (so the trigger box is shown as a fallback); the operator
dismisses it and presses Start; the belt finally moves and the SAME object is
detected again as a new one, because its track id was evicted while stopped.

The fix is to not look at all until the belt has had a moment to get moving.
"""
import sys, tempfile, time
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.DETECTION_SETTLE_SECONDS = 0.0
settings.DETECTION_SAMPLE_FRAMES = 3
settings.RAW_FRAME_EVERY = 1
settings.DETECTION_START_GRACE_SECONDS = 0.4   # short enough for a test
from app.services.scan_session import ScanSession
from app.services.conveyor_service import conveyor_service

frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
box = lambda x, y: [float(x), float(y), float(x + 40), float(y + 40), 0.9, 2]

checks = 0


def check(cond, msg):
    global checks
    assert cond, msg
    checks += 1


# ---------------------------------------------------------------- the bug
s = ScanSession()
conveyor_service.unlock_machine_start(reason="test setup")
s.start("T1GRACE", "toor", "", analysis_parameters=["Stones"])

# An object is sitting under the camera at the instant Start is pressed. The
# belt has been told to start but has not moved.
for _ in range(5):
    st = s.process_frame(frame, [box(900, 400)])
    check(st["fm_detected"] is False,
          "detected an object while the belt was still at rest")

check(s.review_phase == "idle",
      "the stop/settle/look cycle was entered during the start grace")
check(not conveyor_service.machine_start_locked,
      "the belt was stopped before it had even started moving")
check(s.conveyor_stop_count["fm_count"] == 0,
      "an at-rest sighting was counted as a foreign-matter stop")
print("1. nothing detected, nothing stopped while the belt was at rest")

# ------------------------------------------------- the belt is now moving
time.sleep(settings.DETECTION_START_GRACE_SECONDS)

# The same object, now actually travelling down the belt, IS detected — the
# grace suppresses the at-rest sighting, it must not suppress the real one.
st = s.process_frame(frame, [box(900, 500)])
check(st["fm_detected"] is False, "showed a screen before the belt had stopped")
check(s.review_phase == "settling",
      "the object was not detected once the belt was moving: %s" % s.review_phase)
check(conveyor_service.machine_start_locked, "belt was not stopped on sighting")
check(s.conveyor_stop_count["fm_count"] == 1,
      "wrong stop count: %s" % s.conveyor_stop_count["fm_count"])
print("2. the same object IS detected once the belt is moving")

for _ in range(settings.DETECTION_SAMPLE_FRAMES):
    st = s.process_frame(frame, [box(900, 500)])
check(st["fm_detected"] is True, "no review screen after settling")
check(len(s.pending) == 1, "expected 1 object on screen, got %s" % len(s.pending))
print("3. one review screen, one object — counted exactly once")

# ------------------------------------- a mid-batch resume keeps detecting
# The grace is the FIRST Start of a batch only. A resume after a review must
# not open a window in which an object can cross the view unseen.
s.label_detection(s.pending[0]["index"], "Stones")
s.resume()
s.start("T1GRACE", "toor", "", analysis_parameters=["Stones"])
check(s._start_grace_deadline == 0.0,
      "a mid-batch resume re-armed the start grace — objects could pass unseen")

st = s.process_frame(frame, [box(300, 700)])
check(s.review_phase == "settling",
      "detection was suppressed after a mid-batch resume: %s" % s.review_phase)
print("4. a mid-batch resume detects immediately — no blind window")

# --------------------------------------------------------- the off switch
settings.DETECTION_START_GRACE_SECONDS = 0.0
s2 = ScanSession()
conveyor_service.unlock_machine_start(reason="test setup")
s2.start("T2GRACE", "toor", "", analysis_parameters=["Stones"])
check(s2._start_grace_deadline == 0.0, "grace armed when set to 0")
s2.process_frame(frame, [box(900, 400)])
check(s2.review_phase == "settling",
      "DETECTION_START_GRACE_SECONDS=0 did not restore the old behaviour")
print("5. DETECTION_START_GRACE_SECONDS=0 restores the previous behaviour")

conveyor_service.unlock_machine_start(reason="test teardown")
print("\nAll %d checks passed." % checks)
