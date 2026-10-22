"""Archive SSD: copy, verify, device clean-up and the history rows it marks.

    USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_ssd_archive.py

Every test builds its own fake data folder and fake SSD in a temp dir, with a
throwaway SQLite database. Nothing touches a real drive or the real belt.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.environ.get("USE_MOCK_CAMERA", "").lower() not in ("1", "true", "yes"):
    sys.exit("Refusing to run: set USE_MOCK_CAMERA=true so the REAL belt is not driven.")

os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'ssd.db')}"

import logging  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models.schema import DataCollectionSession, FolderCopy, Result, SsdArchiveState  # noqa: E402

assert str(engine.url).startswith("sqlite"), f"refusing to test against {engine.url}"
Base.metadata.create_all(bind=engine)

from app.services import dc_history, folder_copies  # noqa: E402
from app.services.ssd_archive import copier, layout, target, worker  # noqa: E402
from app.services.ssd_archive.config import ArchiveConfig  # noqa: E402

logging.disable(logging.CRITICAL)
DAY = 86400


def _epoch_name(dt, suffix="sample"):
    return f"{int(dt.timestamp())}_{suffix}"


def _dt_name(dt, prefix="G11790000000"):
    return f"{prefix}_{dt:%Y%m%d%H%M%S}"


class Env:
    """A fake OUTPUT_DIR, a fake mount root holding one marked SSD, and a
    clean database. The backend's settings point at them for the test."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ssd-archive-test-"))
        self.data = self.tmp / "output"
        self.mounts = self.tmp / "media"
        self.ssd = self.mounts / "nvidia" / "ARCHIVE"
        self.ssd.mkdir(parents=True)
        (self.ssd / ".eyecompass_archive").write_text("{}")
        self.data.mkdir(parents=True)
        self.cfg = ArchiveConfig(
            data_root=self.data, device_retention_days=30, min_file_age_seconds=60,
            ssd_reserve_bytes=0, marker_name=".eyecompass_archive",
            mount_roots=(self.mounts,), require_separate_disk=False,
            # The tests below that use 40-day-old samples are about the device;
            # SsdRetentionTests turns the SSD clean-up on.
            ssd_retention_days=0)
        settings.OUTPUT_DIR = str(self.data)
        settings.SSD_ARCHIVE_ENABLED = True
        settings.S3_DELETE_AFTER_UPLOAD = False
        folder_copies.LOCK_PATH = str(self.tmp / ".device_files.lock")
        ArchiveConfig.from_settings = classmethod(lambda cls: self.cfg)
        target._read_cache = (0.0, None)
        worker._busy = lambda: None
        worker._protected_dirs = lambda: set()
        worker._stopping.clear()              # an earlier test's task.stop()
        db = SessionLocal()
        db.query(DataCollectionSession).delete()
        db.query(FolderCopy).delete()
        db.query(Result).delete()
        db.query(SsdArchiveState).delete()
        db.commit()
        db.close()

    def add(self, source, commodity, variety, name, files, age_days=0):
        folder = self.data / source / commodity / variety / name
        for rel, content in files.items():
            p = folder / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
            t = time.time() - age_days * DAY
            os.utime(p, (t, t))
        # The folders too: the S3 uploader ages a folder by everything in it
        t = time.time() - age_days * DAY
        for d in (folder, folder.parent, folder.parent.parent):
            if d.is_dir():
                os.utime(d, (t, t))
        return folder

    def add_run(self, name, files, age_days, commodity="rice", variety="sella"):
        """A Data Collection run with its history row."""
        folder = self.add("Data_Collection", commodity, variety, name, files, age_days)
        db = SessionLocal()
        db.add(DataCollectionSession(
            rel_path=f"{commodity}/{variety}/{name}", sample_id=name.split("_", 1)[1],
            commodity=commodity, variety=variety, status="completed",
            started_at=datetime.now() - timedelta(days=age_days), frames_saved=len(files)))
        db.commit()
        db.close()
        return folder

    def copy(self, tree, name):
        db = SessionLocal()
        try:
            return db.query(FolderCopy).filter(
                FolderCopy.tree == tree, FolderCopy.rel_path.endswith("/" + name)).first()
        finally:
            db.close()

    def row(self, name):
        db = SessionLocal()
        try:
            return db.query(DataCollectionSession).filter(
                DataCollectionSession.rel_path.endswith("/" + name)).first()
        finally:
            db.close()

    def run(self, **kw):
        return worker.run_once(self.cfg, **kw)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Case(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()


class ParserTests(unittest.TestCase):
    def test_epoch_prefix(self):
        self.assertEqual(layout.time_from_epoch_prefix("1789387342_testentry"),
                         datetime.fromtimestamp(1789387342))
        self.assertIsNone(layout.time_from_epoch_prefix("abc_1789387342"))
        self.assertIsNone(layout.time_from_epoch_prefix("17893873_x"))

    def test_datetime_suffix(self):
        self.assertEqual(layout.time_from_datetime_suffix("G11791445078_20261008131152"),
                         datetime(2026, 10, 8, 13, 11, 52))
        self.assertIsNone(layout.time_from_datetime_suffix("G1_20261340000000"))
        self.assertIsNone(layout.time_from_datetime_suffix("nodate"))

    def test_safe_component(self):
        self.assertEqual(copier.safe_component("Toor dal"), "Toor dal")
        self.assertEqual(copier.safe_component('a:b?c.'), "a_b_c")
        self.assertEqual(copier.safe_component("..."), "_")


class DiscoveryTests(Case):
    def test_normal_and_misplaced_samples(self):
        dt = datetime(2026, 10, 1, 12, 0, 0)
        self.env.add("output_frame", "chana_dal", "pu2", _dt_name(dt), {"r_frame_0.jpg": b"a"},
                     age_days=1)
        stray = self.env.data / "output_frame" / "_20260910025931"
        stray.mkdir(parents=True)
        (stray / "r_frame_0.jpg").write_bytes(b"b")
        os.utime(stray / "r_frame_0.jpg", (time.time() - DAY, time.time() - DAY))
        found = {s.name: s for s in layout.discover(layout.SOURCES[1], self.env.data,
                                                    time.time(), 60)}
        self.assertEqual(len(found), 2)
        self.assertEqual(found[_dt_name(dt)].commodity, "chana_dal")
        self.assertEqual(found["_20260910025931"].commodity, layout.UNSORTED)

    def test_young_files_held_back(self):
        folder = self.env.add("Data_Collection", "c", "v", _epoch_name(datetime.now()),
                              {"old.png": b"x"}, age_days=1)
        (folder / "new.png").write_bytes(b"y")
        (sample,) = layout.discover(layout.SOURCES[0], self.env.data, time.time(), 60)
        self.assertEqual([f.name for f in sample.files], ["old.png"])
        self.assertEqual(sample.too_new, 1)


class CopierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src.jpg"
        self.src.write_bytes(b"original")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_then_present(self):
        dest = self.tmp / "out" / "src.jpg"
        self.assertEqual(copier.archive_file(self.src, dest).status, "copied")
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertFalse(dest.with_name("src.jpg.part").exists())
        self.assertEqual(copier.archive_file(self.src, dest).status, "present")

    def test_never_overwrites_different_file(self):
        dest = self.tmp / "out" / "src.jpg"
        dest.parent.mkdir()
        dest.write_bytes(b"older archived version")
        out = copier.archive_file(self.src, dest)
        self.assertEqual(out.status, "conflict")
        self.assertEqual(dest.read_bytes(), b"older archived version")
        self.assertEqual(copier.verified_copy(self.src, dest), out.dest)

    def test_verified_copy_detects_corruption(self):
        dest = self.tmp / "out" / "src.jpg"
        copier.archive_file(self.src, dest)
        dest.write_bytes(b"originaL")
        self.assertIsNone(copier.verified_copy(self.src, dest))


class RunTests(Case):
    def test_copies_into_date_layout_and_keeps_recent_on_device(self):
        dt = datetime.now() - timedelta(days=2)
        src = self.env.add("output_frame", "chana_dal", "pu2", _dt_name(dt),
                           {"r_frame_0.jpg": b"a", "fm_full_frames/frame_0.png": b"b"}, 2)
        self.assertEqual(self.env.run()["result"], "ok")
        day = self.env.ssd / dt.strftime("%Y-%m-%d")
        self.assertEqual((day / "output_frame/chana_dal/pu2" / src.name
                          / "fm_full_frames/frame_0.png").read_bytes(), b"b")
        self.assertTrue((src / "r_frame_0.jpg").exists())

    def test_old_sample_deleted_only_after_verification(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        self.assertEqual(self.env.run()["result"], "ok")
        self.assertFalse(src.exists())
        self.assertTrue((self.env.data / "output_frame").is_dir())
        self.assertFalse((self.env.data / "output_frame" / "c").exists())

    def test_silently_corrupted_ssd_copy_blocks_deletion_until_repaired(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"aaaa"}, 40)
        dest = self.env.ssd / dt.strftime("%Y-%m-%d") / "output_frame/c/v" / src.name / "r.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"aaab")
        st = (src / "r.jpg").stat()
        os.utime(dest, (st.st_atime, st.st_mtime))
        self.env.run()
        self.assertEqual(dest.read_bytes(), b"aaab")
        good = [p for p in dest.parent.iterdir() if ".conflict-" in p.name]
        self.assertEqual([p.read_bytes() for p in good], [b"aaaa"])
        self.assertFalse(src.exists())

    def test_unrepairable_copy_keeps_device_data(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        original = copier.archive_file
        copier.archive_file = lambda *a, **k: copier.CopyOutcome("failed", a[1], "io error")
        try:
            summary = self.env.run()
        finally:
            copier.archive_file = original
        self.assertTrue((src / "r.jpg").exists())
        self.assertEqual(summary["result"], "errors")
        self.assertEqual(summary["kept_unverified"], 1)

    def test_no_ssd_or_two_ssds_change_nothing(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        other = self.env.mounts / "nvidia" / "SECOND"
        other.mkdir()
        (other / ".eyecompass_archive").write_text("{}")
        self.assertEqual(self.env.run()["result"], "ssd-not-connected")
        (other / ".eyecompass_archive").unlink()
        (self.env.ssd / ".eyecompass_archive").unlink()
        self.assertEqual(self.env.run()["result"], "ssd-not-connected")
        self.assertTrue((src / "r.jpg").exists())

    def test_not_enough_space_copies_nothing(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        self.env.cfg = ArchiveConfig(**{**self.env.cfg.__dict__, "ssd_reserve_bytes": 10 ** 18})
        self.assertEqual(self.env.run()["result"], "ssd-full")
        self.assertTrue((src / "r.jpg").exists())
        self.assertFalse(any(self.env.ssd.rglob("r.jpg")))

    def test_dry_run_changes_nothing(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "D"), {"a.png": b"a"}, 40)
        self.assertEqual(self.env.run(dry_run=True)["result"], "ok")
        self.assertTrue((src / "a.png").exists())
        self.assertFalse(any(self.env.ssd.rglob("a.png")))
        self.assertIsNone(self.env.copy("Data_Collection", src.name))

    def test_in_use_folder_not_copied(self):
        dt = datetime.now() - timedelta(days=2)
        src = self.env.add("output", "c", "v", _dt_name(dt, "HELD"), {"Stones_1.png": b"s"}, 2)
        worker._protected_dirs = lambda: {os.path.realpath(src)}
        self.env.run()
        self.assertFalse(any(self.env.ssd.rglob("Stones_1.png")), "held batch copied")
        worker._protected_dirs = lambda: set()
        self.env.run()
        self.assertTrue(any(self.env.ssd.rglob("Stones_1.png")))

    def test_in_use_folder_kept(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        worker._protected_dirs = lambda: {os.path.realpath(src)}
        summary = self.env.run()
        self.assertTrue((src / "r.jpg").exists())
        self.assertEqual(summary["kept_in_use"], 1)

    def test_scan_starting_stops_the_run(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        worker._busy = lambda: "a batch scan is running"
        self.assertEqual(self.env.run()["result"], "deferred")
        self.assertTrue((src / "r.jpg").exists())

    def test_shutdown_stops_a_run_between_files(self):
        dt = datetime.now() - timedelta(days=2)
        for i in range(30):
            self.env.add("output_frame", "c", "v", _dt_name(dt, f"S{i}"), {"r.jpg": b"x" * 10}, 2)
        task = worker.SsdArchiveTask()
        calls = []
        original = copier.archive_file

        def slow(*a, **k):
            calls.append(1)
            if len(calls) == 3:
                task.stop()                         # the backend shutting down
            return original(*a, **k)
        copier.archive_file = slow
        try:
            summary = worker.run_once(self.env.cfg)
        finally:
            copier.archive_file = original
            worker._stopping.clear()
        self.assertEqual(summary["result"], "deferred")
        self.assertEqual(len(calls), 3, "kept copying after shutdown")

    def test_once_per_day_and_waits_for_s3(self):
        dt = datetime.now() - timedelta(days=1)
        self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 1)
        task = worker.SsdArchiveTask()
        try:
            self.assertEqual(task.tick()["result"], "ok")
            self.assertEqual(worker.load_state()["last_success_date"],
                             datetime.now().date().isoformat())
            self.env.add("output_frame", "c", "v", _dt_name(dt, "G2"), {"r.jpg": b"z"}, 1)
            self.assertIsNone(task.tick())          # done for today
            with folder_copies.device_files_lock():  # the S3 upload is running
                self.assertIsNone(task.tick(force=True))
            self.assertFalse(any(self.env.ssd.rglob("G2_*")))
            task.tick(force=True)
            self.assertTrue(any(self.env.ssd.rglob("G2_*")))
        finally:
            task.stop()


class RecordTests(Case):
    """Where each folder went is recorded in the same run that copies and
    removes it."""

    def test_recent_run_recorded_on_ssd_and_kept(self):
        dt = datetime.now() - timedelta(days=2)
        src = self.env.add_run(_epoch_name(dt, "S1"), {"a.png": b"a"}, 2)
        self.env.run()
        c = self.env.copy("Data_Collection", src.name)
        self.assertTrue(c.ssd_copied_at and not c.removed_at)
        self.assertEqual(c.ssd_name, "ARCHIVE")
        self.assertTrue((self.env.ssd / c.ssd_path / "a.png").is_file())
        self.assertTrue((src / "a.png").exists())

    def test_old_run_recorded_then_removed(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "OLD"), {"a.png": b"a"}, 40)
        self.assertEqual(self.env.run()["result"], "ok")
        self.assertFalse(src.exists())
        c = self.env.copy("Data_Collection", src.name)
        self.assertTrue(c.ssd_copied_at and c.removed_at)
        self.assertEqual(c.removed_by, "ssd_archive")

    def test_database_failure_keeps_folder_on_device(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "DB"), {"a.png": b"a"}, 40)
        original = folder_copies.record_ssd
        folder_copies.record_ssd = lambda *a, **k: False
        try:
            summary = self.env.run()
        finally:
            folder_copies.record_ssd = original
        self.assertTrue((src / "a.png").exists(), "removed without the database recording it")
        self.assertEqual(summary["kept_unrecorded"], 1)
        self.assertEqual(summary["result"], "errors")   # not done for the day

    def test_run_without_row_gets_one(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("Data_Collection", "rice", "raw", _epoch_name(dt, "NOROW"),
                           {"a.png": b"a"}, 40)
        self.env.run()
        self.assertFalse(src.exists())
        row = self.env.row(src.name)
        self.assertEqual((row.sample_id, row.source, row.frames_saved), ("NOROW", "imported", 1))

    def test_crops_copied_and_configurable(self):
        dt = datetime.now() - timedelta(days=2)
        crops = self.env.add("output", "toor_dal", "pu2", _dt_name(dt, "B1"),
                             {"Stones_1.png": b"s", "result.json": b"{}"}, 2)
        self.env.cfg = ArchiveConfig(**{**self.env.cfg.__dict__,
                                        "sources": ("Data_Collection", "output_frame")})
        self.env.run()
        self.assertIsNone(self.env.copy("output", crops.name))
        self.env.cfg = ArchiveConfig(**{**self.env.cfg.__dict__,
                                        "sources": ("Data_Collection", "output_frame", "output")})
        self.env.run()
        c = self.env.copy("output", crops.name)
        self.assertTrue((self.env.ssd / c.ssd_path / "Stones_1.png").is_file())


class CloudTests(Case):
    """SSD_ARCHIVE_REQUIRE_CLOUD, and the S3 uploader with the SSD archive on."""

    def test_require_cloud_waits_for_s3(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        self.env.cfg = ArchiveConfig(**{**self.env.cfg.__dict__, "require_cloud": True})
        summary = self.env.run()
        self.assertTrue((src / "r.jpg").exists())
        self.assertEqual(summary["kept_for_cloud"], 1)
        folder_copies.record_cloud("output_frame", f"c/v/{src.name}", 1, True)
        self.env.run()
        self.assertFalse(src.exists())

    def test_cloud_copy_older_than_the_folder_does_not_count(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt), {"r.jpg": b"a"}, 40)
        folder_copies.record_cloud("output_frame", f"c/v/{src.name}", 1, True)
        db = SessionLocal()
        db.query(FolderCopy).update({FolderCopy.cloud_uploaded_at: datetime.now() - timedelta(days=50)})
        db.commit()
        db.close()
        self.env.cfg = ArchiveConfig(**{**self.env.cfg.__dict__, "require_cloud": True})
        self.env.run()
        self.assertTrue((src / "r.jpg").exists())

    def _s3(self):
        from app.services.s3_worker import S3UploaderTask

        class FakeS3:
            def __init__(self):
                self.objects, self.heads = {}, 0

            def head_object(self, Bucket, Key, ChecksumMode=None):
                import base64
                import hashlib

                from botocore.exceptions import ClientError
                self.heads += 1
                if Key not in self.objects:
                    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
                data = self.objects[Key]
                return {"ContentLength": len(data), "ChecksumSHA256":
                        base64.b64encode(hashlib.sha256(data).digest()).decode()}

            def put_object(self, Bucket, Key, Body, ChecksumAlgorithm=None, ChecksumSHA256=None):
                self.objects[Key] = Body.read()

        settings.S3_RETENTION_DAYS = 0
        settings.S3_MIN_AGE_MINUTES = 30
        w = S3UploaderTask()
        w._client = FakeS3()
        w._key_prefix = lambda: "p/"
        w._ensure_credentials = lambda: True
        S3UploaderTask._busy = staticmethod(lambda: None)
        S3UploaderTask._protected_dirs = staticmethod(lambda: set())
        return w

    def test_s3_removes_only_what_the_ssd_has(self):
        dt = datetime.now() - timedelta(days=2)
        a = self.env.add("output_frame", "c", "v", _dt_name(dt, "A"), {"r.jpg": b"a"}, 2)
        self.env.run()                       # A is on the SSD
        b = self.env.add("output_frame", "c", "v", _dt_name(dt, "B"), {"r.jpg": b"b"}, 2)
        settings.S3_DELETE_AFTER_UPLOAD = True
        w = self._s3()
        summary = w.run_once()
        self.assertFalse(a.exists(), "on the SSD and in S3: should be removed")
        self.assertTrue((b / "r.jpg").exists(), "not on the SSD: must stay")
        self.assertEqual(summary["kept_for_ssd"], 1)
        self.assertEqual(self.env.copy("output_frame", a.name).removed_by, "s3")
        self.assertTrue(self.env.copy("output_frame", b.name).cloud_uploaded_at)
        # Cloud only: the SSD archive off, S3 removes what it uploaded
        settings.SSD_ARCHIVE_ENABLED = False
        w.run_once()
        self.assertFalse(b.exists())

    def test_s3_does_not_recheck_an_uploaded_folder(self):
        dt = datetime.now() - timedelta(days=2)
        self.env.add("output_frame", "c", "v", _dt_name(dt, "A"), {"r.jpg": b"a", "s.jpg": b"s"}, 2)
        w = self._s3()
        w.run_once()
        heads = w._client.heads
        summary = w.run_once()
        self.assertEqual(w._client.heads, heads)
        self.assertEqual(summary["already_uploaded"], 1)

    def test_lock_is_shared_across_processes(self):
        import subprocess

        code = ("import fcntl,sys,time; f=open(sys.argv[1],'w'); "
                "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(3)")
        p = subprocess.Popen([sys.executable, "-c", code, folder_copies.LOCK_PATH],
                             stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")
            with folder_copies.device_files_lock() as held:
                self.assertFalse(held)
        finally:
            p.kill()
            p.wait()
        with folder_copies.device_files_lock() as held:
            self.assertTrue(held)


class SsdRetentionTests(Case):
    """Copy every day, remove from the device at once, delete from the SSD
    after SSD_ARCHIVE_SSD_RETENTION_DAYS."""

    def cfg(self, **kw):
        base = {**self.env.cfg.__dict__, "device_retention_days": 0, "ssd_retention_days": 30}
        base.update(kw)
        self.env.cfg = ArchiveConfig(**base)

    def test_device_copy_removed_as_soon_as_verified(self):
        self.cfg()
        dt = datetime.now() - timedelta(hours=2)
        done = self.env.add("output_frame", "c", "v", _dt_name(dt, "DONE"), {"r.jpg": b"a"}, 0.05)
        held = self.env.add("output_frame", "c", "v", _dt_name(dt, "HELD"), {"r.jpg": b"h"}, 0.05)
        fresh = self.env.add("output_frame", "c", "v", _dt_name(dt, "NEW"), {"r.jpg": b"n"}, 0)
        worker._protected_dirs = lambda: {os.path.realpath(held)}
        self.assertEqual(self.env.run()["result"], "ok")
        self.assertFalse(done.exists(), "verified on the SSD: should leave the device")
        self.assertTrue((held / "r.jpg").exists(), "in use: must stay")
        self.assertTrue((fresh / "r.jpg").exists(), "written moments ago: must stay")
        c = self.env.copy("output_frame", done.name)
        self.assertTrue((self.env.ssd / c.ssd_path / "r.jpg").is_file())

    def _on_ssd(self, days_old, name, tree="Data_Collection", record=True):
        day = (datetime.now() - timedelta(days=days_old)).strftime("%Y-%m-%d")
        rel = f"rice/sella/{name}"
        folder = self.env.ssd / day / tree / "rice" / "sella" / name
        folder.mkdir(parents=True)
        (folder / "a.png").write_bytes(b"x" * 100)
        if record:
            folder_copies.record_ssd(tree, rel, "ARCHIVE", folder.relative_to(self.env.ssd).as_posix())
        return folder, rel

    def test_ssd_deletes_after_retention_whether_in_s3_or_not(self):
        self.cfg()
        old, old_rel = self._on_ssd(40, "1700000000_OLD")
        unknown, _ = self._on_ssd(40, "1700000001_NOREC", record=False)
        recent, _ = self._on_ssd(10, "1700000002_NEW")
        other = self.env.ssd / (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d") / "my_notes"
        other.mkdir(parents=True)
        (self.env.ssd / "2020-01-01-backup").mkdir()
        summary = self.env.run()
        self.assertFalse(old.exists())
        self.assertFalse(unknown.exists())
        self.assertTrue(recent.exists(), "within 30 days: kept")
        self.assertTrue(other.exists(), "not the archiver's layout: untouched")
        self.assertTrue((self.env.ssd / "2020-01-01-backup").exists())
        self.assertEqual(summary["ssd_deleted_samples"], 2)
        c = folder_copies.get("Data_Collection", old_rel)
        self.assertTrue(c.ssd_removed_at)
        self.assertFalse(folder_copies.on_ssd(c))
        self.assertEqual(folder_copies.storage_state(c, None), "removed_from_ssd")

    def test_ssd_delete_can_require_s3(self):
        self.cfg(ssd_delete_require_cloud=True)
        kept, _ = self._on_ssd(40, "1700000000_K")
        gone, gone_rel = self._on_ssd(40, "1700000001_G")
        folder_copies.record_cloud("Data_Collection", gone_rel, 1, True)
        summary = self.env.run()
        self.assertTrue(kept.exists(), "not in S3: must stay on the SSD")
        self.assertFalse(gone.exists())
        self.assertEqual(summary["ssd_kept_for_cloud"], 1)
        self.assertEqual(folder_copies.storage_state(
            folder_copies.get("Data_Collection", gone_rel), None), "in_cloud")

    def test_device_sample_older_than_ssd_keeps_is_left_alone(self):
        self.cfg()
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add("output_frame", "c", "v", _dt_name(dt, "BACKLOG"), {"r.jpg": b"a"}, 40)
        summary = self.env.run()
        self.assertTrue((src / "r.jpg").exists())
        self.assertFalse(any(self.env.ssd.rglob("r.jpg")))
        self.assertEqual(summary["kept_past_ssd_retention"], 1)

    def test_zero_keeps_the_ssd_forever(self):
        self.cfg(ssd_retention_days=0)
        old, _ = self._on_ssd(400, "1600000000_X")
        self.env.run()
        self.assertTrue(old.exists())

    def test_dry_run_deletes_nothing_from_the_ssd(self):
        self.cfg()
        old, _ = self._on_ssd(40, "1700000000_D")
        summary = self.env.run(dry_run=True)
        self.assertTrue(old.exists())
        self.assertEqual(summary["ssd_deleted_samples"], 1)


class LowDiskTests(Case):
    def test_archive_off_runs_only_the_safety_net(self):
        old = datetime.now() - timedelta(days=40)
        a = self.env.add("output_frame", "c", "v", _dt_name(old, "A"), {"r.jpg": b"a"}, 40)
        folder_copies.record_cloud("output_frame", f"c/v/{a.name}", 1, True)
        settings.SSD_ARCHIVE_ENABLED = False
        settings.S3_DELETE_AFTER_UPLOAD = True     # cloud only
        task = worker.SsdArchiveTask()
        try:
            self.assertIsNone(task.tick())
            self.assertTrue(a.exists())                     # not low: nothing
            self.assertFalse(any(self.env.ssd.rglob("r.jpg")), "copied with the archive off")
            worker._free_percent = lambda path: 5.0 if a.exists() else 20.0
            task.tick()
            self.assertFalse(a.exists())                    # low: in S3, so removed
        finally:
            task.stop()

    def _low(self, percents):
        it = iter(percents)
        worker._free_percent = lambda path: next(it, 50.0)

    def tearDown(self):
        worker._free_percent = ORIGINAL_FREE_PERCENT
        super().tearDown()

    def test_removes_oldest_with_a_copy_never_one_without(self):
        old = datetime.now() - timedelta(days=10)
        newer = datetime.now() - timedelta(days=5)
        a = self.env.add("output_frame", "c", "v", _dt_name(old, "A"), {"r.jpg": b"a"}, 10)
        b = self.env.add("output_frame", "c", "v", _dt_name(newer, "B"), {"r.jpg": b"b"}, 5)
        self.env.run()                                   # both on the SSD, within retention
        n = self.env.add("output_frame", "c", "v", _dt_name(old, "NOCOPY"), {"r.jpg": b"n"}, 10)
        # Low until both A and B are gone
        worker._free_percent = lambda path: 5.0 if (a.exists() or b.exists()) else 20.0
        summary = worker.free_space_if_low(self.env.cfg)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertTrue((n / "r.jpg").exists(), "no copy anywhere: must stay")
        self.assertEqual(summary["removed_samples"], 2)
        self.assertEqual(self.env.copy("output_frame", a.name).removed_by, "low_disk")

    def test_ssd_unplugged_uses_the_record_or_s3(self):
        old = datetime.now() - timedelta(days=10)
        a = self.env.add("output_frame", "c", "v", _dt_name(old, "A"), {"r.jpg": b"a"}, 10)
        self.env.run()
        c = self.env.add("output_frame", "c", "v", _dt_name(old, "C"), {"r.jpg": b"c"}, 10)
        folder_copies.record_cloud("output_frame", f"c/v/{c.name}", 1, True)
        (self.env.ssd / ".eyecompass_archive").unlink()
        self._low([5.0] * 10)
        worker.free_space_if_low(self.env.cfg)
        self.assertFalse(a.exists())
        self.assertFalse(c.exists())

    def test_nothing_removed_when_settings_never_remove(self):
        old = datetime.now() - timedelta(days=40)
        a = self.env.add("output_frame", "c", "v", _dt_name(old, "A"), {"r.jpg": b"a"}, 40)
        folder_copies.record_cloud("output_frame", f"c/v/{a.name}", 1, True)
        settings.SSD_ARCHIVE_ENABLED = False
        settings.S3_DELETE_AFTER_UPLOAD = False    # keep everything
        worker._free_percent = lambda path: 1.0
        task = worker.SsdArchiveTask()
        try:
            task.tick()
        finally:
            task.stop()
        self.assertTrue(a.exists())

    def test_crops_need_an_ssd_copy_not_just_s3(self):
        old = datetime.now() - timedelta(days=40)
        crops = self.env.add("output", "c", "v", _dt_name(old, "B"), {"Stones_1.png": b"s"}, 40)
        folder_copies.record_cloud("output", f"c/v/{crops.name}", 1, True)
        (self.env.ssd / ".eyecompass_archive").unlink()
        worker._free_percent = lambda path: 1.0
        worker.free_space_if_low(self.env.cfg)
        self.assertTrue((crops / "Stones_1.png").exists())

    def test_not_low_does_nothing(self):
        old = datetime.now() - timedelta(days=10)
        a = self.env.add("output_frame", "c", "v", _dt_name(old, "A"), {"r.jpg": b"a"}, 10)
        self.env.run()
        self._low([50.0])
        self.assertIsNone(worker.free_space_if_low(self.env.cfg))
        self.assertTrue(a.exists())


class HistoryTests(Case):
    """Data Collection and batch History find images on the device or the SSD."""

    def test_storage_and_where_images_are_read_from(self):
        dt = datetime.now() - timedelta(days=40)
        moved = self.env.add_run(_epoch_name(dt, "MOVED"), {"a.png": b"a", "b.png": b"b"}, 40)
        here = self.env.add_run(_epoch_name(dt + timedelta(days=38), "HERE"), {"c.png": b"c"}, 2)
        gone = self.env.add_run(_epoch_name(dt, "GONE"), {}, 40)
        self.env.run()

        def state(name):
            target._read_cache = (0.0, None)
            row = self.env.row(name)
            copy = folder_copies.get("Data_Collection", row.rel_path)
            _, where, names = dc_history.image_folder(row, copy)
            return folder_copies.storage_state(copy, where), where, names

        self.assertEqual(state(moved.name), ("on_ssd", "ssd", ["a.png", "b.png"]))
        self.assertEqual(state(here.name)[:2], ("on_device", "device"))
        self.assertEqual(state(gone.name)[0], "missing_from_device")

        (self.env.ssd / ".eyecompass_archive").rename(self.env.ssd / "marker.off")
        self.assertEqual(state(moved.name), ("missing_from_ssd", None, []))
        self.assertFalse(folder_copies.ssd_connected())
        folder_copies.record_cloud("Data_Collection", self.env.row(moved.name).rel_path, 2, True)
        self.assertEqual(state(moved.name)[0], "in_cloud")
        (self.env.ssd / "marker.off").rename(self.env.ssd / ".eyecompass_archive")
        self.assertEqual(state(moved.name)[0], "on_ssd")

    def test_ssd_path_cannot_leave_the_drive(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "T"), {"a.png": b"a"}, 40)
        self.env.run()
        db = SessionLocal()
        db.query(FolderCopy).update({FolderCopy.ssd_path: "../../../.."})
        db.commit()
        db.close()
        target._read_cache = (0.0, None)
        self.assertIsNone(folder_copies.ssd_folder(folder_copies.get(
            "Data_Collection", self.env.row(src.name).rel_path)))

    def test_batch_history_crops_from_ssd(self):
        from app.api.history import get_result_images

        dt = datetime.now() - timedelta(days=40)
        name = _dt_name(dt, "BATCH1")
        self.env.add("output", "toor_dal", "pu2", name,
                     {"Stones_1700000000.png": b"\x89PNG fake", "result.json": b"{}"}, 40)
        db = SessionLocal()
        r = Result(commodity="Toor Dal", variety="PU2",
                   result={"scan_data": {"image_unique_id": name}})
        db.add(r)
        db.commit()

        def images():
            target._read_cache = (0.0, None)
            return get_result_images(r.id, db=db, limit=50, offset=0, item=None)

        try:
            here = images()
            self.assertEqual((here["source"], here["total"]), ("device", 1))
            self.env.run()                               # 40 days old: moved to the SSD
            self.assertFalse((self.env.data / "output" / "toor_dal" / "pu2" / name).exists())
            moved = images()
            self.assertEqual((moved["source"], moved["storage"], moved["total"]), ("ssd", "on_ssd", 1))
            self.assertTrue(moved["images"][0]["data_uri"].startswith("data:image/png;base64,"))
            self.assertFalse(moved["on_device"])
            (self.env.ssd / ".eyecompass_archive").unlink()
            unplugged = images()
            self.assertEqual((unplugged["total"], unplugged["storage"], unplugged["ssd_connected"]),
                             (0, "missing_from_ssd", False))
        finally:
            db.close()


ORIGINAL_FREE_PERCENT = worker._free_percent


if __name__ == "__main__":
    unittest.main(verbosity=2)
