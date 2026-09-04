"""
Background retry worker for unsynced results.

Port of sync_unsynced_data_Thread (main.py:2826-2966), which ran every 15
minutes and re-posted anything still at sync_status='0'. This is the whole
offline-resilience story for a device that regularly loses connectivity, and it
had no counterpart in the new backend — get_unsynced_results() existed but was
never called by anything.

Started from the app lifespan in app/main.py.
"""

import asyncio
import logging

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.database_service import DatabaseService
from app.services.sync_service import sync_service

logger = logging.getLogger(__name__)


def _run_one_cycle() -> dict:
    """One pass over the pending records. Runs in a worker thread."""
    db = SessionLocal()
    summary = {"pending": 0, "delivered": 0, "rejected": 0, "still_pending": 0}
    try:
        service = DatabaseService(db)
        pending = service.get_unsynced_results()
        summary["pending"] = len(pending)
        if not pending:
            return summary

        if not sync_service.is_authenticated:
            if not sync_service.login_qualix(
                settings.QUALIX_USERNAME, settings.QUALIX_PASSWORD
            ):
                logger.warning(
                    "Retry cycle: %s record(s) pending but Qualix login failed; "
                    "will try again next cycle.",
                    len(pending),
                )
                summary["still_pending"] = len(pending)
                return summary

        for record in pending:
            datagram = record.result
            if not isinstance(datagram, dict):
                logger.error(
                    "Result %s has a non-object payload (%s); skipping.",
                    record.id, type(datagram).__name__,
                )
                summary["still_pending"] += 1
                continue

            post_status, error_code = sync_service.post_analysis_data(datagram)

            if post_status == "1":
                try:
                    sync_service.post_to_sheets(datagram)
                except Exception as exc:
                    logger.error("Sheets retry failed for result %s: %s", record.id, exc)
                summary["delivered"] += 1
            elif post_status == "2":
                logger.error(
                    "Result %s rejected by Qualix (400) — marking terminal.", record.id
                )
                summary["rejected"] += 1
            else:
                logger.warning(
                    "Result %s still undelivered (%s).", record.id, error_code
                )
                summary["still_pending"] += 1

            service.set_sync_status(record.id, post_status)

        return summary
    except Exception as exc:
        logger.error("Retry cycle failed: %s", exc)
        return summary
    finally:
        db.close()


async def sync_retry_worker():
    """Loop forever, retrying undelivered results on the configured interval."""
    interval = max(1, settings.SYNC_RETRY_INTERVAL_MINUTES) * 60

    # Give the app a moment to finish starting before the first pass.
    await asyncio.sleep(30)

    while True:
        try:
            summary = await asyncio.to_thread(_run_one_cycle)
            if summary["pending"]:
                logger.info(
                    "Retry cycle: %s pending -> %s delivered, %s rejected, %s still pending",
                    summary["pending"], summary["delivered"],
                    summary["rejected"], summary["still_pending"],
                )
        except asyncio.CancelledError:
            logger.info("Sync retry worker stopped.")
            raise
        except Exception as exc:
            logger.error("Sync retry worker error: %s", exc)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Sync retry worker stopped.")
            raise
