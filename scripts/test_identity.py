"""Each physical object is put in front of the operator exactly once.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_identity.py

Reproduces the sequence logged on 28 Sep: one object reviewed and submitted,
then a second, different object arriving a moment later. The first object was
still being tracked the whole time, but was shown again alongside the new one
because by then nothing was awaiting review to compare its box against.
"""
import sys, tempfile
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.DETECTION_SETTLE_SECONDS = 0.0
# The belt-start grace would otherwise suppress detection for the first
# seconds of every run started here; these tests feed frames immediately.
settings.DETECTION_START_GRACE_SECONDS = 0.0
settings.DETECTION_SAMPLE_FRAMES = 1
from app.services.scan_session import ScanSession

frame = np.zeros((1200, 1920, 3), dtype=np.uint8)

def review_cycle(session, detections, limit=6):
    """Drive one sighting through stop -> settle -> look, as the stream loop
    would, and return the state once a screen appears."""
    for _ in range(limit):
        state = session.process_frame(frame, list(detections))
        if state["fm_detected"]:
            return state
    return state

big = [1131.0, 0.0, 1272.0, 119.0, 0.885, 0]        # the real box from the log
big_again = [1132.0, 0.0, 1272.0, 120.0, 0.884, 0]  # same object, next look
other = [874.0, 1057.0, 931.0, 1120.0, 0.267, 3]    # a genuinely different one

s = ScanSession()
s.start("T1IDENT", "toor", "", analysis_parameters=["Stones"])

st = review_cycle(s, [big])
assert st["fm_detected"] and len(s.pending) == 1, s.pending
print("1. big object shown for review")

s.resume()                                           # operator submits
s.start("T1IDENT", "toor", "")                       # operator presses Start
st = review_cycle(s, [big_again, other])
shown = [p["box"] for p in s.pending]
assert st["fm_detected"], "the genuinely new object was not reported"
assert len(shown) == 1, "the already-reviewed object was shown again: %s" % shown
assert shown[0][0] < 900, "wrong object shown: %s" % shown
print("2. only the NEW object is shown; the reviewed one is not repeated")

# An object hard against the left edge must still get an identity, so it is
# both counted and protected from being re-shown.
s2 = ScanSession()
s2.start("T1EDGE", "toor", "", analysis_parameters=["Stones"])
edge = [0.0, 438.0, 34.0, 487.0, 0.21, 3]
st = review_cycle(s2, [edge])
assert st["fm_detected"], "an object at the left edge was never reported"
s2.resume()
s2.start("T1EDGE", "toor", "")
st = review_cycle(s2, [edge])
assert not st["fm_detected"], "the left-edge object was reported twice"
print("3. left-edge object is reported once, then not again")
print("ALL IDENTITY CHECKS PASSED")
