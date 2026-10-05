"""One object crossing the frame is reviewed once, not again as it leaves.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_edge_duplicate.py

Reported live 29 Sep: "the last 4 objects I marked as paper were actually two
but shown twice, once in the frame and once while leaving the frame (half in
the frame)". An object is reviewed while whole in the middle of the frame, the
operator submits, the belt resumes, and the same object reaches the bottom edge
where only half of it is still visible. That clipped box used to be minted a
brand new id, which made it a brand new object to everything downstream.
"""
import sys, time
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
from app.services.scan_session import ScanSession

H, W = 1200, 1920
frame = np.zeros((H, W, 3), np.uint8)
def box(y, x=300, w=40, h=40):
    return (x, y, x + w, y + h, 0.9, 0)

settings.DETECTION_SETTLE_SECONDS = 0.01
# The belt-start grace would otherwise suppress detection for the first
# seconds of every run started here; these tests feed frames immediately.
settings.DETECTION_START_GRACE_SECONDS = 0.0
s = ScanSession()
s.start("T1EDGE", "toor", "", analysis_parameters=["Stones"])

# Whole, in the middle of the frame: gets reviewed.
for _ in range(6):
    s.process_frame(frame, [box(600)])
    time.sleep(0.01)
assert len(s.pending) == 1, "the object was not reviewed while whole: %s" % s.pending
print("1. object reviewed once while whole in the frame")

s.resume()                                  # operator submits
s.start("T1EDGE", "toor", "")               # operator presses Start again

# The belt carries it on down. The last steps are clipped by the bottom edge —
# only the top of the object is still in view.
for y in (900, 1000, 1100, 1150, 1170, 1180, 1185):
    height = min(40, H - y)
    s.process_frame(frame, [box(y, h=height)])
    time.sleep(0.01)

shown = len(s.pending) + sum(len(q["boxes"]) for q in s.detection_queue)
assert shown == 0, (
    "the object was shown again on its way out: pending=%s queued=%s"
    % (len(s.pending), len(s.detection_queue)))
print("2. not shown again as a half box while leaving")

# A genuinely new object arriving afterwards, in a different lane, is still
# reviewed — the rule must not have made the bottom of the frame a dead zone.
for _ in range(6):
    s.process_frame(frame, [box(500, x=1000)])
    time.sleep(0.01)
assert len(s.pending) == 1, "a genuinely new object was suppressed: %s" % s.pending
print("3. a genuinely new object is still reviewed")
print("OK")
