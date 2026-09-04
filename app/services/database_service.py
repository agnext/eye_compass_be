"""Persistence helpers for scan results."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.schema import Result

logger = logging.getLogger(__name__)

# Legacy sync_status contract (api_handle.py:136-143):
#   '1' delivered, '2' rejected by Qualix (terminal), '0' pending retry.
SYNC_OK = "1"
SYNC_REJECTED = "2"
SYNC_PENDING = "0"


class DatabaseService:
    def __init__(self, db: Session):
        self.db = db

    def save_scan_result(
        self,
        sample_id: str,
        commodity: str,
        variety: str,
        datagram: dict,
        date: str = None,
        start_time: str = None,
        stop_time: str = None,
    ) -> Result:
        """Persist a completed scan.

        Times come from the ScanSession, which stamped them when the run
        actually started and ended — not from the moment of the HTTP call.
        """
        now = datetime.now()
        result = Result(
            sample_id=sample_id,
            commodity=commodity,
            variety=variety,
            result=datagram,
            date=date or now.strftime("%Y-%m-%d"),
            start_time=start_time or now.strftime("%H:%M:%S"),
            stop_time=stop_time or now.strftime("%H:%M:%S"),
            sync_status=SYNC_PENDING,
        )
        self.db.add(result)
        self.db.commit()
        self.db.refresh(result)
        return result

    def set_sync_status(self, result_id: int, status: str) -> bool:
        """Record the Qualix outcome, preserving all three legacy states."""
        result = self.db.query(Result).filter(Result.id == result_id).first()
        if not result:
            return False
        result.sync_status = status
        self.db.commit()
        return True

    def mark_as_synced(self, result_id: int) -> bool:
        return self.set_sync_status(result_id, SYNC_OK)

    def get_unsynced_results(self):
        """Records still awaiting delivery.

        Only '0'. A '2' was rejected by Qualix as malformed and retrying it
        forever would just fail forever — legacy treated anything != '0' as
        finished (main.py:2917-2924).
        """
        return (
            self.db.query(Result)
            .filter(Result.sync_status == SYNC_PENDING)
            .order_by(Result.id.asc())
            .all()
        )
