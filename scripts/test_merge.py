"""Duplicate-box merging (ScanSession._merge_overlapping_detections).

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_merge.py

The two identical-coordinate pairs below are real, taken from the 17:41 and
17:43 detections of batch T11790338159, where the model returned one object
under two different classes and it was reviewed and counted as two.
"""
import sys; sys.path.insert(0, '.')
from app.services.scan_session import ScanSession

s = ScanSession()

# Same object, two classes, identical pixels — the live case.
dets = [
    [1412.0, 759.0, 1459.0, 813.0, 0.228, 3],
    [1412.0, 759.0, 1459.0, 813.0, 0.273, 2],
    [994.0, 1096.0, 1072.0, 1174.0, 0.519, 2],
]
kept = s._merge_overlapping_detections(dets)
assert len(kept) == 2, kept
assert kept[0][4] == 0.273, "the lower-confidence duplicate won"
assert kept[1][0] == 994.0, "input order was not preserved"

# The other live pair.
kept = s._merge_overlapping_detections([
    [1623.0, 821.0, 1670.0, 873.0, 0.305, 1],
    [1623.0, 821.0, 1670.0, 873.0, 0.343, 2],
])
assert len(kept) == 1 and kept[0][5] == 2, kept

# Two genuinely separate objects close together must BOTH survive.
kept = s._merge_overlapping_detections([
    [300.0, 400.0, 340.0, 440.0, 0.5, 2],
    [300.0, 445.0, 340.0, 485.0, 0.5, 2],   # touching, zero overlap
])
assert len(kept) == 2, kept

# Partial overlap below the threshold survives as two.
kept = s._merge_overlapping_detections([
    [300.0, 400.0, 340.0, 440.0, 0.5, 2],
    [320.0, 400.0, 360.0, 440.0, 0.4, 2],   # IoU = 1/3
])
assert len(kept) == 2, kept

# Degenerate inputs
assert s._merge_overlapping_detections([]) == []
assert len(s._merge_overlapping_detections([[0.0, 0.0, 10.0, 10.0, 0.5, 1]])) == 1
print("ALL MERGE CHECKS PASSED")
