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
from app.services.scan_session import ScanSession

s = ScanSession()
s.start("T1IDENT", "toor", "", analysis_parameters=["Stones"])
frame = np.zeros((1200, 1920, 3), dtype=np.uint8)

big = [1131.0, 0.0, 1272.0, 119.0, 0.885, 0]        # the real box from the log
big_again = [1132.0, 0.0, 1272.0, 120.0, 0.884, 0]  # same object, next look
other = [874.0, 1057.0, 931.0, 1120.0, 0.267, 3]    # a genuinely different one

st = s.process_frame(frame, [big])
assert st["fm_detected"] and len(s.pending) == 1
print("1. big object shown for review")

s.resume()                                           # operator submits
s.start("T1IDENT", "toor", "")                       # operator presses Start
st = s.process_frame(frame, [big_again, other])
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
st = s2.process_frame(frame, [edge])
assert st["fm_detected"], "an object at the left edge was never reported"
s2.resume()
s2.start("T1EDGE", "toor", "")
st = s2.process_frame(frame, [edge])
assert not st["fm_detected"], "the left-edge object was reported twice"
print("3. left-edge object is reported once, then not again")
print("ALL IDENTITY CHECKS PASSED")
