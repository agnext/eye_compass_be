"""Duplicate-detection filtering (ScanSession._novel_boxes).

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_dup.py

The belt travels in +y, so an object following another shares its x-centre;
these cases pin down that a trailing object is kept while the same object seen
a frame later is dropped, and that the whole queued backlog is matched against,
not just the detection currently on screen.
"""
import sys; sys.path.insert(0, '.')
from app.services.scan_session import ScanSession

s = ScanSession()
s.pending = [{"index": 0, "box": [300.0, 400.0, 340.0, 440.0]}]
box = lambda x, y: [x, y, x + 40, y + 40, 0.9, 1]
novel = lambda bs: [b[:2] for b in s._novel_boxes(bs)]

assert novel([box(300, 430)]) == []                      # same object, later frame
assert novel([box(300, 700)]) == []                      # same object, far along the coast
assert novel([box(300, 340)]) == [[300, 340]]            # object trailing behind it
assert novel([box(600, 430)]) == [[600, 430]]            # object in another lane
assert novel([box(300, 430), box(900, 350)]) == [[900, 350]]   # mixed frame
s.pending = []
assert novel([box(300, 430)]) == [[300, 430]]            # nothing awaiting review
s.detection_queue = [{"frame": None, "boxes": [box(300, 400)]}]
assert novel([box(300, 430)]) == []                      # matched against the QUEUE too
assert novel([box(900, 430)]) == [[900, 430]]
print("ALL SUPPRESSION CHECKS PASSED")

# --- the live regression from batch T11790579022 ----------------------------
# One large object reviewed twice, 392px apart down the belt. Its box centre
# moved 12px sideways between the two looks (1759 -> 1747), which a flat 10px
# threshold called a different object.
s2 = ScanSession()
s2.pending = [{"index": 0, "box": [1606.0, 511.0, 1912.0, 745.0]}]
again = [1598.0, 903.0, 1896.0, 1129.0, 0.939, 0]
assert s2._novel_boxes([again]) == [], "the same large object was shown twice"

# A genuinely separate object of the same size in another lane still gets through.
assert s2._novel_boxes([[900.0, 903.0, 1198.0, 1129.0, 0.9, 0]]) != []
# And a small object is still held to a tight threshold, so two small objects
# 15px apart are not collapsed into one.
s2.pending = [{"index": 0, "box": [300.0, 400.0, 347.0, 449.0]}]
assert s2._novel_boxes([[315.0, 400.0, 362.0, 449.0, 0.5, 2]]) != []
print("live duplicate regression passed")
