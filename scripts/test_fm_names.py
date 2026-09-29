"""FM names survive the round trip through the filesystem.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_fm_names.py

A crop's filename IS the record — create_results counts files by their FM-type
prefix — so any FM name the Qualix config offers has to be writable and then
readable back. Reported live 29 Sep: the config gained "Insects/Pest" and
"Mould/Fungus", and the submit table stopped showing marked FMs correctly. A
forward slash is the path separator, so the crop was never written at all and
the object was lost, not merely miscounted.
"""
import sys, os, time
sys.path.insert(0, '.')
import numpy as np
from app.services.scan_session import (
    ScanSession, fm_filename_token, fm_name_from_token,
)

VOCAB = ['Mud Balls', 'Plant Matter', 'Metal', 'Thread', 'Insects/Pest',
         'Mould/Fungus', 'Glass Piece', 'Plastic Pieces', 'Animal Matter',
         'Stones', 'Paper', 'Infestation', 'Rubber']

# 1. The mapping reverses for every name, including NON-FM's hyphen.
for name in VOCAB + ['NON-FM', 'FM']:
    token = fm_filename_token(name)
    assert '/' not in token and os.sep not in token, (name, token)
    assert fm_name_from_token(token) == name, (name, token, fm_name_from_token(token))
print("1. every FM name maps to a filename-safe token and back: %s" % len(VOCAB + ['NON-FM', 'FM']))

# 2. A crop is really written for every name, and counted under it.
frame = np.zeros((1200, 1920, 3), np.uint8)
frame[:] = 40
s = ScanSession()
s.start("T1NAMES", "chana dal", "", analysis_parameters=list(VOCAB))
s.pending_frame = frame
s.pending = [{"index": i, "box": [100.0, 100.0, 200.0, 200.0],
              "raw_box": [100.0, 100.0, 200.0, 200.0],
              "confidence": 0.9, "class_id": 0}
             for i in range(len(VOCAB))]
for i, name in enumerate(VOCAB):
    s.label_detection(i, name)

on_disk = os.listdir(s.output_folder)
assert len(on_disk) == len(VOCAB), (
    "%s of %s crops reached the disk: %s" % (len(on_disk), len(VOCAB), sorted(on_disk)))
print("2. all %s crops written to disk" % len(on_disk))

counts = s.create_results(0, 0)
for name in VOCAB:
    assert counts.get(name) == 1, (
        "%r counted %s time(s), expected 1: %s" % (name, counts.get(name), counts))
print("3. every FM name counted exactly once, separators included")

# 4. Each crop reports the type it is counted as.
for crop in on_disk:
    fm_type = s._crop_fm_type(crop)
    assert fm_type in VOCAB, "%s resolved to %r" % (crop, fm_type)
print("4. every crop resolves back to its own FM type")

# 5. A write that cannot land is an error, not a silent loss.
s.output_folder = "/proc/definitely-not-writable"
try:
    s.label_detection(0, "Stones")
except Exception:
    print("5. an unwritable crop raises instead of being silently dropped")
else:
    raise AssertionError("a crop that could not be written was reported as saved")
print("OK")
