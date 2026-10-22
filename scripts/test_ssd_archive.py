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
from app.models.schema import DataCollectionSession, SsdArchiveState  # noqa: E402

assert str(engine.url).startswith("sqlite"), f"refusing to test against {engine.url}"
Base.metadata.create_all(bind=engine)

from app.services import dc_history  # noqa: E402
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
            mount_roots=(self.mounts,), require_separate_disk=False)
        settings.OUTPUT_DIR = str(self.data)
        ArchiveConfig.from_settings = classmethod(lambda cls: self.cfg)
        target._read_cache = (0.0, None)
        worker._busy = lambda: None
        worker._protected_dirs = lambda: set()
        db = SessionLocal()
        db.query(DataCollectionSession).delete()
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
        self.assertIsNone(self.env.row(src.name).ssd_archived_at)

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
            with worker.DEVICE_FILES_LOCK:          # the S3 upload is running
                self.assertIsNone(task.tick(force=True))
            self.assertFalse(any(self.env.ssd.rglob("G2_*")))
            task.tick(force=True)
            self.assertTrue(any(self.env.ssd.rglob("G2_*")))
        finally:
            task.stop()


class HistoryRowTests(Case):
    """The database learns where each Data Collection run went, in the same
    run that copies and removes it."""

    def test_recent_run_marked_on_ssd_and_kept(self):
        dt = datetime.now() - timedelta(days=2)
        src = self.env.add_run(_epoch_name(dt, "S1"), {"a.png": b"a"}, 2)
        self.env.run()
        row = self.env.row(src.name)
        self.assertTrue(row.ssd_archived_at and not row.ssd_removed_at)
        self.assertEqual(row.ssd_name, "ARCHIVE")
        self.assertTrue((self.env.ssd / row.ssd_path / "a.png").is_file())
        self.assertTrue((src / "a.png").exists())
        first = row.ssd_archived_at
        self.env.run()                                  # again: first time kept
        self.assertEqual(self.env.row(src.name).ssd_archived_at, first)

    def test_old_run_marked_then_removed(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "OLD"), {"a.png": b"a"}, 40)
        self.assertEqual(self.env.run()["result"], "ok")
        self.assertFalse(src.exists())
        row = self.env.row(src.name)
        self.assertTrue(row.ssd_archived_at and row.ssd_removed_at)

    def test_database_failure_keeps_run_on_device(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "DB"), {"a.png": b"a"}, 40)
        original = dc_history.record_ssd_copy
        dc_history.record_ssd_copy = lambda *a, **k: False
        try:
            summary = self.env.run()
        finally:
            dc_history.record_ssd_copy = original
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
        self.assertTrue(row.ssd_removed_at)

    def test_storage_and_where_images_are_read_from(self):
        dt = datetime.now() - timedelta(days=40)
        moved = self.env.add_run(_epoch_name(dt, "MOVED"), {"a.png": b"a", "b.png": b"b"}, 40)
        here = self.env.add_run(_epoch_name(dt + timedelta(days=38), "HERE"), {"c.png": b"c"}, 2)
        gone = self.env.add_run(_epoch_name(dt, "GONE"), {}, 40)
        self.env.run()

        def state(name):
            target._read_cache = (0.0, None)
            row = self.env.row(name)
            folder, where, names = dc_history.image_folder(row)
            return dc_history.storage_state(row, where), where, names

        self.assertEqual(state(moved.name), ("on_ssd", "ssd", ["a.png", "b.png"]))
        self.assertEqual(state(here.name)[:2], ("on_device", "device"))
        self.assertTrue(self.env.row(here.name).ssd_archived_at)  # "SSD" tag
        self.assertEqual(state(gone.name)[0], "missing_from_device")

        # Unplugged
        (self.env.ssd / ".eyecompass_archive").rename(self.env.ssd / "marker.off")
        self.assertEqual(state(moved.name), ("missing_from_ssd", None, []))
        self.assertFalse(dc_history.ssd_connected())
        # Plugged back, but the run's folder is not on it
        (self.env.ssd / "marker.off").rename(self.env.ssd / ".eyecompass_archive")
        shutil.rmtree(self.env.ssd / self.env.row(moved.name).ssd_path)
        self.assertEqual(state(moved.name)[0], "missing_from_ssd")
        target._read_cache = (0.0, None)
        self.assertTrue(dc_history.ssd_connected())

    def test_ssd_path_cannot_leave_the_drive(self):
        dt = datetime.now() - timedelta(days=40)
        src = self.env.add_run(_epoch_name(dt, "T"), {"a.png": b"a"}, 40)
        self.env.run()
        db = SessionLocal()
        row = db.query(DataCollectionSession).filter(
            DataCollectionSession.rel_path.endswith(src.name)).first()
        row.ssd_path = "../../../.."
        db.commit()
        db.close()
        target._read_cache = (0.0, None)
        self.assertIsNone(dc_history.ssd_folder(self.env.row(src.name)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
