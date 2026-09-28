"""Data Collection details validation (/api/camera/data_collection/prepare).

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_data_collection.py

The sample id ends up as part of a directory name, so the cases below cover
both the ordinary "operator left a field blank" path and the characters that
would move that directory somewhere it does not belong.
"""
import sys, os, tempfile
sys.path.insert(0, '.')
from app.core.config import settings
settings.OUTPUT_DIR = tempfile.mkdtemp()
from fastapi import HTTPException
from app.api.camera import prepare_data_collection, DataCollectionRequest as R

def rejected(label, **kw):
    try:
        prepare_data_collection(R(**kw))
    except HTTPException as exc:
        print("  rejected %-26s %s" % (label + ":", exc.detail))
        return
    raise AssertionError("accepted " + label)

print("missing fields")
rejected("blank sample id", sample_id="", commodity="toor", variety="x")
rejected("whitespace sample id", sample_id="   ", commodity="toor", variety="x")
rejected("blank commodity", sample_id="S1", commodity="", variety="x")
rejected("blank variety", sample_id="S1", commodity="toor", variety="")

print("special characters in sample id")
for bad, label in [
    ("../../etc", "path traversal"),
    ("a/b", "slash"),
    ("S 1", "space"),
    ("S#1", "hash"),
    ("S.1", "dot"),
    ("S:1", "colon"),
    ("S*1", "asterisk"),
    ("S\\1", "backslash"),
]:
    rejected(label, sample_id=bad, commodity="toor", variety="x")

print("accepted")
for good in ("S1", "SAMPLE-123", "sample_123", "abc-DEF_9"):
    res = prepare_data_collection(R(sample_id=good, commodity="toor", variety="x"))
    assert res["folder"].endswith("_" + good), res
    root = os.path.realpath(os.path.join(settings.OUTPUT_DIR, "Data_Collection"))
    assert os.path.realpath(res["folder"]).startswith(root + os.sep)
    print("  %-12s -> %s" % (good, res["folder"].rsplit("/", 1)[-1]))

# Surrounding whitespace is trimmed rather than rejected.
res = prepare_data_collection(R(sample_id="  S9  ", commodity="toor", variety="x"))
assert res["folder"].endswith("_S9"), res
print("  trimmed      -> %s" % res["folder"].rsplit("/", 1)[-1])
print("ALL DATA COLLECTION CHECKS PASSED")
