"""
Held and interrupted batch scans — keeping a run so it can be continued later.

A batch scan used to live only in memory. Leaving it, on purpose or by losing
power, lost it. Two features are built on one record, the `scan_progress`
table (app/models/schema.py), one row per run:

* **Hold.** On the results page after Submit, the operator can put the batch on
  hold instead of Save or Cancel. It is listed under Held Batches on Home and
  can be continued from the live scan screen: Start scans more material into the
  same batch, or Submit goes straight to the results page.

* **Power-cut recovery.** The row of the batch being scanned is kept up to date
  at every operator action. If the backend stops while a batch is active — power
  cut, crash, restart — that batch is found still `active` at the next startup,
  marked `interrupted`, and is listed and continued exactly like a held one.

What makes this cheap: a run's results are on disk already. create_results
counts the crop files in its output folder and Frame Count counts its r_frame
files, so continuing a run only needs the small in-memory part back
(ScanSession.progress_state / restore). The row is written at operator actions
only — Start, Stop, each reviewed detection, Submit, Hold — never from the
per-frame detection loop, so scanning speed is unaffected.

Every write here is best-effort for the scan itself: a database problem is
logged and the scan carries on, because losing the ability to *recover* a batch
must never stop the operator *scanning* one.

The open rows (active, held, interrupted) are also what the S3 worker protects:
see open_folders().
"""

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from app.core.database import SessionLocal
from app.models.schema import BatchDetails, ScanProgress

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("held", "interrupted")
LIVE_STATUSES = ("active",) + OPEN_STATUSES


def _now() -> datetime:
    return datetime.now()


def _close_hold(row: ScanProgress, now: datetime) -> None:
    """End the hold in progress, adding its length to the stored totals."""
    if row.held_at:
        row.total_held_seconds = int((row.total_held_seconds or 0)
                                     + max(0.0, (now - row.held_at).total_seconds()))
        history = list(row.hold_history or [])
        if history and not history[-1].get("resumed_at"):
            history[-1] = {**history[-1], "resumed_at": now.isoformat(timespec="seconds")}
        row.hold_history = history
    row.held_at = None


def _open_hold(row: ScanProgress, reason: str, at: datetime) -> None:
    row.held_at = at
    row.hold_count = (row.hold_count or 0) + 1
    row.hold_history = list(row.hold_history or []) + [
        {"held_at": at.isoformat(timespec="seconds"), "resumed_at": None, "reason": reason}
    ]


def checkpoint(state: Dict, awaiting_save: bool = False) -> None:
    """Record the run in `state` (ScanSession.progress_state()) as the active one.

    Any other run still marked active was left without being finished — the
    operator went on to a different batch — so it is marked interrupted rather
    than lost: it shows up under Held Batches and can be continued.

    Never raises.
    """
    folder = state.get("folder_name")
    if not folder:
        return
    db = SessionLocal()
    try:
        now = _now()
        for other in (db.query(ScanProgress)
                      .filter(ScanProgress.status == "active",
                              ScanProgress.folder_name != folder)
                      .all()):
            other.status = "interrupted"
            _open_hold(other, "interrupted", other.updated_at or now)
            # Nothing to announce: the operator did this themselves, by going
            # on to another batch, and is standing in front of the machine.
            # Only a run cut off while nobody could be told is announced.
            other.interrupt_notified = True
            logger.info("Batch %s left unfinished — kept as interrupted", other.folder_name)

        row = db.query(ScanProgress).filter(ScanProgress.folder_name == folder).first()
        if row is None:
            row = ScanProgress(folder_name=folder, created_at=now, hold_history=[])
            db.add(row)
        for key in ("sample_id", "commodity", "variety", "batch_id", "analysis_parameters",
                    "start_date", "start_time", "output_folder", "output_frame_folder",
                    "frame_count", "saved_frame_count", "clean_frame_count",
                    "next_pending_index", "conveyor_stop_count"):
            if key in state:
                setattr(row, key, state[key])
        row.status = "active"
        row.awaiting_save = awaiting_save
        row.updated_at = now
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not record scan progress for %s: %s", folder, exc)
    finally:
        db.close()


def _set_status(folder: str, status: str) -> None:
    if not folder:
        return
    db = SessionLocal()
    try:
        row = db.query(ScanProgress).filter(ScanProgress.folder_name == folder).first()
        if row is None:
            return
        now = _now()
        if status in ("saved", "discarded"):
            _close_hold(row, now)
            row.closed_at = now
            row.awaiting_save = False
        row.status = status
        row.updated_at = now
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not mark scan %s as %s: %s", folder, status, exc)
    finally:
        db.close()


def mark_saved(folder: str) -> None:
    _set_status(folder, "saved")


def mark_discarded(folder: str) -> None:
    _set_status(folder, "discarded")


def interrupt(folder: str) -> None:
    """The live run `folder` is being dropped without Save, Cancel or Hold —
    e.g. the scan page was reloaded. Keep it as interrupted. Never raises."""
    if not folder:
        return
    db = SessionLocal()
    try:
        row = (db.query(ScanProgress)
               .filter(ScanProgress.folder_name == folder, ScanProgress.status == "active")
               .first())
        if row is not None:
            row.status = "interrupted"
            _open_hold(row, "interrupted", row.updated_at or _now())
            # Deliberate and in front of the operator (the scan page was
            # reloaded or left), so there is nothing to announce later.
            row.interrupt_notified = True
            db.commit()
            logger.info("Batch %s dropped unfinished — kept as interrupted", folder)
    except Exception as exc:
        db.rollback()
        logger.error("Could not keep scan %s as interrupted: %s", folder, exc)
    finally:
        db.close()


def hold(folder: str) -> bool:
    """Put the submitted run `folder` on hold. Returns False if there is no row."""
    db = SessionLocal()
    try:
        row = db.query(ScanProgress).filter(ScanProgress.folder_name == folder).first()
        if row is None:
            return False
        now = _now()
        row.status = "held"
        row.awaiting_save = True
        _open_hold(row, "held", now)
        row.updated_at = now
        db.commit()
        logger.info("Batch %s put on hold", folder)
        return True
    finally:
        db.close()


def interrupt_all_active() -> int:
    """At startup: whatever was active when the backend last stopped was cut off.

    The hold starts at the row's last update — the last moment the run is known
    to have been alive — not at startup, so the outage counts as time held and
    never as stop time.
    """
    db = SessionLocal()
    try:
        rows = db.query(ScanProgress).filter(ScanProgress.status == "active").all()
        for row in rows:
            row.status = "interrupted"
            _open_hold(row, "interrupted", row.updated_at or _now())
            # The one case where nobody could be told at the time: announce it
            # on Home the next time an operator is there. See
            # pending_interrupt_notice().
            row.interrupt_notified = False
        db.commit()
        for row in rows:
            logger.warning(
                "Batch %s was in progress when the backend stopped — kept as "
                "interrupted; it can be continued from Held Batches.", row.folder_name,
            )
        return len(rows)
    except Exception as exc:
        db.rollback()
        logger.error("Could not check for interrupted scans: %s", exc)
        return 0
    finally:
        db.close()


def _crop_count(folder: Optional[str]) -> int:
    if not folder or not os.path.isdir(folder):
        return 0
    return sum(1 for n in os.listdir(folder)
               if os.path.splitext(n)[1].lower() in (".png", ".jpg", ".jpeg"))


def _summary(row: ScanProgress, batch: Optional[BatchDetails]) -> Dict:
    return {
        "id": row.id,
        "sample_id": row.sample_id,
        "commodity": row.commodity,
        "variety": row.variety,
        "batch_id": row.batch_id,
        "vendor_name": getattr(batch, "vendor_name", "") or "",
        "start_date": row.start_date,
        "start_time": row.start_time,
        "status": row.status,
        "held_at": row.held_at.isoformat(timespec="seconds") if row.held_at else None,
        "submitted": bool(row.awaiting_save),
        "fm_count": _crop_count(row.output_folder),
        "files_present": bool(row.output_folder and os.path.isdir(row.output_folder)),
    }


def list_open() -> List[Dict]:
    """Held and interrupted runs, most recently held first."""
    db = SessionLocal()
    try:
        rows = (db.query(ScanProgress)
                .filter(ScanProgress.status.in_(OPEN_STATUSES))
                .order_by(ScanProgress.held_at.desc().nullslast(), ScanProgress.id.desc())
                .all())
        batch_ids = [r.batch_id for r in rows if r.batch_id]
        batches = {}
        if batch_ids:
            batches = {b.id: b for b in
                       db.query(BatchDetails).filter(BatchDetails.id.in_(batch_ids)).all()}
        return [_summary(r, batches.get(r.batch_id)) for r in rows]
    finally:
        db.close()


def count_open() -> int:
    db = SessionLocal()
    try:
        return db.query(ScanProgress).filter(ScanProgress.status.in_(OPEN_STATUSES)).count()
    finally:
        db.close()


def pending_interrupt_notice() -> Optional[Dict]:
    """The interrupted run the operator has not been told about yet, if any.

    Answers the Home screen's "was a scan cut off while the machine was away?"
    question. Only ever one batch — the most recent — because a prompt listing
    several is a prompt nobody reads; `others` says how many more are waiting
    under Held Batches, which is where they are all dealt with anyway.

    Deliberately narrow:

    * `interrupted` only. A `held` batch was the operator's own decision and
      needs no announcement.
    * Unannounced only. Continue and Later both mark it told, so a batch left
      for later never reopens the same prompt on the next boot.
    * Its images must still be on the device. Continue is impossible without
      them, so a prompt offering it would be a dead end; the row stays listed
      under Held Batches, where it can be discarded.

    Never raises: a database problem must not keep the operator off Home.
    """
    db = SessionLocal()
    try:
        rows = (db.query(ScanProgress)
                .filter(ScanProgress.status == "interrupted",
                        ScanProgress.interrupt_notified.is_(False))
                .order_by(ScanProgress.held_at.desc().nullslast(),
                          ScanProgress.id.desc())
                .all())
        # Latest first, so the first one whose images survived is the newest
        # continuable run. Ties break on id, so the answer is stable.
        row = next((r for r in rows
                    if r.output_folder and os.path.isdir(r.output_folder)), None)
        if row is None:
            return None
        batch = (db.query(BatchDetails).filter(BatchDetails.id == row.batch_id).first()
                 if row.batch_id else None)
        others = (db.query(ScanProgress)
                  .filter(ScanProgress.status.in_(OPEN_STATUSES),
                          ScanProgress.id != row.id)
                  .count())
        return {**_summary(row, batch), "others": others}
    except Exception as exc:
        logger.error("Could not check for an unannounced interrupted scan: %s", exc)
        return None
    finally:
        db.close()


def mark_interrupt_notified(progress_id: int) -> bool:
    """The operator has been told about this run — never prompt for it again.

    Called by Later and by Continue alike. Returns False if there is no such
    row. Never raises: failing to record the acknowledgement must not block
    either action, and the worst case is the prompt appearing once more.
    """
    db = SessionLocal()
    try:
        row = db.query(ScanProgress).filter(ScanProgress.id == progress_id).first()
        if row is None:
            return False
        row.interrupt_notified = True
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Could not record the interrupt notice for %s: %s", progress_id, exc)
        return False
    finally:
        db.close()


def get_open(progress_id: int) -> Optional[Dict]:
    """The stored state of an open run, in ScanSession.restore()'s shape."""
    db = SessionLocal()
    try:
        row = (db.query(ScanProgress)
               .filter(ScanProgress.id == progress_id,
                       ScanProgress.status.in_(OPEN_STATUSES))
               .first())
        if row is None:
            return None
        csc = dict(row.conveyor_stop_count or {})
        # A run submitted before it was held already counted Submit's own
        # implicit stop; it will count another at its final Submit, and the
        # Manual Stop Count only discounts one (update_fm_count's "-1"). Take
        # the earlier one back out so holding a batch never adds a stop.
        if row.awaiting_save and csc.get("stop_count"):
            csc["stop_count"] = max(0, int(csc["stop_count"]) - 1)
        return {
            "id": row.id,
            "folder_name": row.folder_name,
            "sample_id": row.sample_id,
            "commodity": row.commodity,
            "variety": row.variety,
            "batch_id": row.batch_id,
            "analysis_parameters": list(row.analysis_parameters or []),
            "start_date": row.start_date,
            "start_time": row.start_time,
            "output_folder": row.output_folder,
            "output_frame_folder": row.output_frame_folder,
            "frame_count": row.frame_count,
            "saved_frame_count": row.saved_frame_count,
            "clean_frame_count": row.clean_frame_count,
            "next_pending_index": row.next_pending_index,
            "conveyor_stop_count": csc,
            "last_checkpoint_ts": row.updated_at.timestamp() if row.updated_at else None,
        }
    finally:
        db.close()


def mark_resumed(progress_id: int) -> None:
    """The run is loaded into the scan session again: active, hold closed."""
    db = SessionLocal()
    try:
        row = db.query(ScanProgress).filter(ScanProgress.id == progress_id).first()
        if row is None:
            return
        now = _now()
        _close_hold(row, now)
        row.status = "active"
        row.awaiting_save = False
        # Continuing it is itself an acknowledgement: if it is interrupted
        # again later, that is a new interruption worth announcing, but this
        # one has been dealt with.
        row.interrupt_notified = True
        row.updated_at = now
        db.commit()
    finally:
        db.close()


def open_folders() -> List[str]:
    """Folders of every run that is not finished — the S3 worker must not touch
    them. Raises if the database cannot be read: the caller must then skip its
    run rather than treat "unknown" as "nothing protected"."""
    db = SessionLocal()
    try:
        rows = (db.query(ScanProgress.output_folder, ScanProgress.output_frame_folder)
                .filter(ScanProgress.status.in_(LIVE_STATUSES))
                .all())
        return [f for pair in rows for f in pair if f]
    finally:
        db.close()
