"""An object parked half out of the bottom of the frame is shown once.

Reported live 29 Sep: one such object produced three review screens. The
tracker's exit rule takes its id the moment it reaches the bottom 50px, but
the half still in view keeps being detected, so it was minted a fresh id and
exit-evicted again on every single frame. Each of those one-frame ids looked
to _note_escapes like an object that had just left, so each one queued its own
review screen — and the per-id guard there could never collapse them, because
the id was different every time. On top of that the object is still visible,
so it also appears on the main review screen built from the stationary frames.
"""
import sys, time
sys.path.insert(0, '.')
import numpy as np
from app.core.config import settings
from app.services.scan_session import ScanSession

frame = np.zeros((1200, 1920, 3), np.uint8)
def box(x, y, w=40):
    return (x, y, x + w, y + w, 0.9, 0)

settings.DETECTION_SETTLE_SECONDS = 0.01
s = ScanSession()
s.start("T1PARKED", "toor", "", analysis_parameters=["Stones"])

# One object sitting half out of the bottom, detected on every frame while the
# belt is stopped, plus one well inside the frame.
for _ in range(12):
    s.process_frame(frame, [box(300, 400), box(900, 1165)])
    time.sleep(0.01)

screens = ([1] if s.pending else []) + [1] * len(s.detection_queue)
total_boxes = len(s.pending) + sum(len(q["boxes"]) for q in s.detection_queue)
assert s.pending, "nothing was shown at all"
assert len(s.detection_queue) == 0, (
    "the parked object queued %s extra screen(s): it is still in view, so it "
    "belongs on the main screen only" % len(s.detection_queue))
assert total_boxes == 2, "expected the two objects once each, got %s" % total_boxes
print("1. parked half out of the bottom -> one screen, two boxes: %s" % total_boxes)

# Draining must not turn up the same object again.
s.resume()
assert not s.detection_queue and not s.pending, (
    "the parked object came back after submit: pending=%s queue=%s"
    % (len(s.pending), len(s.detection_queue)))
print("2. after submit -> nothing comes back")
print("OK")
