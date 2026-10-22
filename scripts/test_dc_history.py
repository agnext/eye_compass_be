"""Data Collection history: records, cleanup, S3 status and the read API.

    USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_dc_history.py

Throwaway SQLite database and output folder; conveyor commands are stubbed,
and the test refuses to run without USE_MOCK_CAMERA=true.
"""
import base64
import hashlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.environ.get("USE_MOCK_CAMERA", "").lower() not in ("1", "true", "yes"):
    sys.exit("Refusing to run: set USE_MOCK_CAMERA=true so the REAL belt is not driven.")

os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'dc.db')}"

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from app.core.config import settings  # noqa: E402

settings.OUTPUT_DIR = tempfile.mkdtemp()

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models.schema import CommodityDetails, DataCollectionSession  # noqa: E402

assert str(engine.url).startswith("sqlite"), f"refusing to test against {engine.url}"
Base.metadata.create_all(bind=engine)

from app.services.conveyor_service import conveyor_service  # noqa: E402

sent = []
conveyor_service.send = lambda cmd, max_retries=None: sent.append(cmd) or True

import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.api import camera  # noqa: E402
from app.main import app  # noqa: E402
from app.services import dc_history  # noqa: E402
from app.services.s3_worker import S3UploaderTask  # noqa: E402

camera._ensure_services = lambda: True
camera._dc_writer = object()           # a preview stream "is connected"


class Client:
    """The app over httpx's ASGI transport. No lifespan, so the belt and
    camera start-up never run. (starlette's TestClient does not work with the
    httpx installed on the device.)"""

    def _call(self, method, url, **kw):
        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(go())

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)


client = Client()
DC = os.path.join(settings.OUTPUT_DIR, "Data_Collection")
FRAME = np.full((1200, 1920, 3), 90, dtype=np.uint8)


def rows():
    db = SessionLocal()
    try:
        return db.query(DataCollectionSession).order_by(DataCollectionSession.id).all()
    finally:
        db.close()


def save_frames(n):
    """What the stream loop does per kept frame: write it, count it."""
    for _ in range(n):
        name = f"frame_{camera._dc_frame_count:05d}.png"
        cv2.imwrite(os.path.join(camera._dc_folder, name), FRAME)
        camera._dc_frame_count += 1
        camera._dc_frames_seen += 3


def prepare(sample="S1", commodity="Toor dal", variety="PU2-TD04-GN0"):
    r = client.post("/api/camera/data_collection/prepare",
                    json={"sample_id": sample, "commodity": commodity, "variety": variety})
    assert r.status_code == 200, r.text
    return camera._dc_folder


# 1. Opening the page creates nothing ----------------------------------------
folder = prepare()
assert not os.path.exists(folder), "prepare created a folder"
assert rows() == [], "prepare created a record"
print("1. prepare -> no folder, no record")

# 2. A real run: folder and record on Start, counts on Stop, completed on Submit
assert client.post("/api/camera/data_collection/start").status_code == 200
assert os.path.isdir(folder) and rows()[0].status == "recording"
save_frames(4)
assert client.post("/api/camera/data_collection/stop").status_code == 200
assert rows()[0].frames_saved == 4 and rows()[0].frames_seen == 12
r = client.post("/api/camera/data_collection/finish").json()
row = rows()[0]
assert row.status == "completed" and row.frames_saved == 4 and r["frame_count"] == 4, row.status
assert row.capture_settings["image_format"] == camera._dc_settings["image_format"]
assert "all_stop" in sent and "machine_start" in sent
print("2. start/stop/submit -> folder + record, counts saved, completed")

# 3. A run that saves nothing: folder and record removed ----------------------
folder = prepare("S2")
client.post("/api/camera/data_collection/start")
assert os.path.isdir(folder) and len(rows()) == 2
client.post("/api/camera/data_collection/finish")
assert not os.path.exists(folder) and len(rows()) == 1
print("3. zero-frame run -> empty folder and record removed")

# 4. A file the counters missed still protects the folder ----------------------
folder = prepare("S3")
client.post("/api/camera/data_collection/start")
cv2.imwrite(os.path.join(folder, "late.png"), FRAME)       # landed, not counted
client.post("/api/camera/data_collection/finish")
assert os.path.isfile(os.path.join(folder, "late.png")), "a file was deleted"
assert rows()[-1].frames_saved == 1 and rows()[-1].status == "completed"
print("4. uncounted file present -> folder kept, record counts it")

# 5. Writes still queued at Submit: nothing is treated as empty ----------------
folder = prepare("S4")
client.post("/api/camera/data_collection/start")
real_drain = camera._dc_drain_writes
camera._dc_drain_writes = lambda timeout=10.0: False
client.post("/api/camera/data_collection/finish")
camera._dc_drain_writes = real_drain
assert os.path.isdir(folder), "folder removed while writes were pending"
assert rows()[-1].status == "recording", "pending run should stay open"
print("5. writes pending at Submit -> empty folder and record left alone")

# 6. Back, then a new run: the old one stops recording and closes -------------
folder6 = prepare("S5")
client.post("/api/camera/data_collection/start")
save_frames(2)
folder7 = prepare("S6")                       # Back was pressed, no Submit
assert not camera._dc_recording, "old run still recording into the new folder"
old = [r for r in rows() if r.sample_id == "S5"][0]
assert old.status == "interrupted" and old.frames_saved == 2, (old.status, old.frames_saved)
assert not os.path.exists(folder7)
print("6. new run while the last one recorded -> old run closed as interrupted")

# 7. Startup: open runs closed, empty ones removed, old folders imported ------
db = SessionLocal()
db.add(CommodityDetails(commodity="Toor dal", variety=[{"variety_code": "PU2-TD04-GN0"}]))
db.commit()
db.close()
legacy = os.path.join(DC, "toor_dal", "pu2-td04-gn0", "1792063189_665")
os.makedirs(legacy)
for i in range(3):
    cv2.imwrite(os.path.join(legacy, f"img_{i}.png"), FRAME)
empty_old = os.path.join(DC, "toor_dal", "pu2-td04-gn0", "1792000000_77")
os.makedirs(empty_old)
odd = os.path.join(DC, "toor_dal", "pu2-td04-gn0", "1792000001_78")
os.makedirs(odd)
open(os.path.join(odd, "notes.txt"), "w").write("not an image")
# A run that was recording with frames when the power went
camera._dc_drain_writes = lambda timeout=10.0: True
folder8 = prepare("S7")
client.post("/api/camera/data_collection/start")
save_frames(3)
camera._dc_run_open = False                   # the process "died" here

dc_history.reconcile_on_startup()
by = {r.sample_id: r for r in rows()}
assert "S4" not in by and not os.path.exists(folder), "pending empty run not cleaned at startup"
assert by["S7"].status == "interrupted" and by["S7"].frames_saved == 3
imp = by["665"]
assert imp.source == "imported" and imp.frames_saved == 3
assert imp.commodity == "Toor dal" and imp.variety == "PU2-TD04-GN0", (imp.commodity, imp.variety)
assert imp.started_at.timestamp() == 1792063189
assert not os.path.exists(empty_old), "empty old folder kept"
assert os.path.isfile(os.path.join(odd, "notes.txt")), "non-image file deleted"
print("7. startup -> interrupted closed, empty removed, old folders imported, nothing else touched")

# 8. API: list, filters, detail, images -------------------------------------
lst = client.get("/api/data_collection/sessions").json()
assert lst["total"] == len(rows())
times = [d["started_at"] for d in lst["data"]]
assert times == sorted(times, reverse=True), "not latest first"
f = client.get("/api/data_collection/sessions/filters").json()
assert {"value": "toor_dal", "label": "Toor dal"} in f["commodities"]
assert client.get("/api/data_collection/sessions?commodity=Toor dal").json()["total"] == lst["total"]
assert client.get("/api/data_collection/sessions?commodity=toor_dal&variety=pu2-td04-gn0").json()["total"] == lst["total"]
assert client.get("/api/data_collection/sessions?commodity=rice").json()["total"] == 0
assert client.get("/api/data_collection/sessions?date_from=2099-01-01").json()["total"] == 0
assert client.get("/api/data_collection/sessions?date_to=2000-01-01").json()["total"] == 0
assert client.get("/api/data_collection/sessions?date_from=bad").status_code == 422

sid = by["S1"].id
d = client.get(f"/api/data_collection/sessions/{sid}").json()
assert d["frames_saved"] == 4 and d["images_on_device"] == 4 and d["storage"] == "on_device"
imgs = client.get(f"/api/data_collection/sessions/{sid}/images?limit=2&offset=1").json()
assert imgs["total"] == 4 and len(imgs["images"]) == 2
th = client.get(imgs["images"][0]["thumb_url"])
assert th.status_code == 200 and th.headers["content-type"] == "image/jpeg"
assert cv2.imdecode(np.frombuffer(th.content, np.uint8), cv2.IMREAD_COLOR).shape[1] == dc_history.THUMB_WIDTH
assert len(th.content) < 60_000, len(th.content)
full = client.get(imgs["images"][0]["url"])
assert full.status_code == 200 and full.headers["content-type"] == "image/png"
for bad in ("..%2F..%2Fapp%2Fmain.py", "nope.png"):
    assert client.get(f"/api/data_collection/sessions/{sid}/images/{bad}").status_code == 404, bad
# A literal "../" never reaches the route (the URL is resolved first), so the
# lookup itself is checked: only names that are files in the run's folder.
from fastapi import HTTPException  # noqa: E402

from app.api.data_collection import _image_path  # noqa: E402

_db = SessionLocal()
for bad in ("../../app/main.py", "..", "frame_00000.png/../../x"):
    try:
        _image_path(_db, sid, bad)
        raise AssertionError(f"{bad} was accepted")
    except HTTPException as exc:
        assert exc.status_code == 404
_db.close()
assert client.get("/api/data_collection/sessions/99999").status_code == 404
print("8. API -> list latest first, filters, detail, paged images, thumbnails, no traversal")

# 9. Live run shows live counts --------------------------------------------
camera._dc_writer = object()
folder9 = prepare("S8")
client.post("/api/camera/data_collection/start")
save_frames(5)
live = [x for x in client.get("/api/data_collection/sessions").json()["data"] if x["sample_id"] == "S8"][0]
assert live["status"] == "recording" and live["frames_saved"] == 5, live
client.post("/api/camera/data_collection/finish")
print("9. run in progress -> list shows its live counts")


# 10. S3 worker: verified upload marks the run; a deleted folder reads right ---
def sha(b):
    return base64.b64encode(hashlib.sha256(b).digest()).decode()


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        data = self.objects[Key]
        return {"ContentLength": len(data), "ChecksumSHA256": sha(data)}

    def put_object(self, Bucket, Key, Body, ChecksumAlgorithm=None, ChecksumSHA256=None):
        self.objects[Key] = Body.read()


old_t = time.time() - 7200
for dirpath, dirs, files in os.walk(settings.OUTPUT_DIR):
    for n in dirs + files:
        os.utime(os.path.join(dirpath, n), (old_t, old_t))
settings.S3_DELETE_AFTER_UPLOAD = True
settings.S3_RETENTION_DAYS = 0
settings.S3_MIN_AGE_MINUTES = 30
camera._dc_folder = None
w = S3UploaderTask()
w._client = FakeS3()
w._key_prefix = lambda: "p/"
summary = w.run_once()
assert summary["failed"] == 0, summary
d = client.get(f"/api/data_collection/sessions/{sid}").json()
assert d["storage"] == "uploaded_cleaned" and d["images_on_device"] == 0, d["storage"]
assert d["cloud_uploaded_at"] and d["cloud_files_verified"] == 4
assert client.get(f"/api/data_collection/sessions/{sid}/images").json()["total"] == 0
print("10. S3 upload + delete -> record marked uploaded, badge 'uploaded & cleaned'")

# 11. Gone without a confirmed upload reads as missing; partial as partial ----
db = SessionLocal()
r = DataCollectionSession(rel_path="rice/x/1792000002_M", sample_id="M", commodity="rice",
                          variety="x", status="completed", frames_saved=5)
db.add(r)
db.commit()
assert dc_history.storage_state(r, None) == "missing_from_device"
r.cloud_files_verified = 2
assert dc_history.storage_state(r, "device") == "partially_uploaded"
db.close()
print("11. frames gone with no confirmed upload -> 'missing_from_device'; part uploaded -> "
      "'partially_uploaded'")

# 12. Archive SSD: a run moved there is still shown, read from the SSD ---------
from pathlib import Path  # noqa: E402

from app.services.ssd_archive import target as ssd_target, worker as ssd_worker  # noqa: E402
from app.services.ssd_archive.config import ArchiveConfig  # noqa: E402

mounts = Path(tempfile.mkdtemp())
ssd = mounts / "nvidia" / "ARCHIVE"
ssd.mkdir(parents=True)
(ssd / ".eyecompass_archive").write_text("{}")
cfg = ArchiveConfig(data_root=Path(settings.OUTPUT_DIR), device_retention_days=30,
                    min_file_age_seconds=60, ssd_reserve_bytes=0,
                    marker_name=".eyecompass_archive", mount_roots=(mounts,),
                    require_separate_disk=False)
ArchiveConfig.from_settings = classmethod(lambda cls: cfg)
ssd_worker._busy = lambda: None
ssd_worker._protected_dirs = lambda: set()


def fresh(url):
    ssd_target._read_cache = (0.0, None)
    return client.get(url).json()


old = time.time() - 40 * 86400
moved = os.path.join(DC, "rice", "y", f"{int(old)}_K")
os.makedirs(moved)
cv2.imwrite(os.path.join(moved, "k.png"), FRAME)
os.utime(os.path.join(moved, "k.png"), (old, old))
db = SessionLocal()
db.add(DataCollectionSession(rel_path=f"rice/y/{int(old)}_K", sample_id="K", commodity="rice",
                             variety="y", status="completed", frames_saved=1))
db.commit()
kid = db.query(DataCollectionSession).filter_by(sample_id="K").first().id
db.close()

summary = ssd_worker.run_once(cfg)
assert summary["result"] == "ok" and summary["deleted_samples"] >= 1, summary
assert not os.path.exists(moved), "old run not moved off the device"
k = fresh(f"/api/data_collection/sessions/{kid}")
assert k["storage"] == "on_ssd" and k["on_ssd"] and k["ssd_connected"], k
assert k["ssd"]["ssd"] == "ARCHIVE" and k["ssd"]["removed_at"], k["ssd"]
imgs = client.get(f"/api/data_collection/sessions/{kid}/images").json()
assert imgs["source"] == "ssd" and imgs["total"] == 1, imgs
assert client.get(imgs["images"][0]["thumb_url"]).headers["content-type"] == "image/jpeg"
full = client.get(imgs["images"][0]["url"])
assert full.status_code == 200 and full.content[:4] == b"\x89PNG"

(ssd / ".eyecompass_archive").rename(ssd / "unplugged")
k = fresh(f"/api/data_collection/sessions/{kid}")
assert k["storage"] == "missing_from_ssd" and not k["ssd_connected"], k
assert client.get(f"/api/data_collection/sessions/{kid}/images").json()["total"] == 0
assert client.get(imgs["images"][0]["url"]).status_code == 404
(ssd / "unplugged").rename(ssd / ".eyecompass_archive")
assert fresh("/api/data_collection/sessions")["ssd_connected"] is True

mid = [r for r in rows() if r.sample_id == "M"][0].id
assert fresh(f"/api/data_collection/sessions/{mid}")["storage"] == "missing_from_device"
assert client.post("/api/data_collection/archive-mark", json=[]).status_code in (404, 405)
print("12. run moved to the SSD -> 'on_ssd', images served from the SSD; unplugged -> "
      "'missing_from_ssd'; never archived -> 'missing_from_device'; no archive-mark endpoint")

print("\nALL DATA COLLECTION HISTORY TESTS PASSED")
