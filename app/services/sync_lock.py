"""Stops the same result being delivered twice at the same time.

Three things deliver a scan to Qualix — the post right after a batch is saved,
the retry worker, and History's manual re-sync — and until this existed,
nothing stopped two of them working on the same record concurrently.

That is not hypothetical. A record stays at sync_status='0' for the *whole*
duration of its POST, and that POST is slow (the endpoint averages ~30s, see
_POST_TIMEOUT_SECONDS in sync_service). So a batch saved shortly before the
retry worker's tick is still marked unsent when the worker lists what needs
sending, and both post it. No operator action is needed for this; a manual
re-sync simply adds a third way in.

What actually breaks is Google Sheets, not Qualix. Qualix recognises the
repeat by sample_id and answers "already exists", which is handled. But
post_to_sheets checks `already_in_sheet` and *then* appends, and two deliveries
interleaving between those two steps both see "not present" and both append —
a duplicate row, for a scan that ran once.

Claims are per result id: two different records syncing at once is normal and
fine. In-process only, which is all that is needed — one backend process owns
this device, and the same assumption already underpins the single-slot pending
submission in api/scan.py.

This set records who is busy *right now*, not a history of what has been sent,
so it does not grow over the life of the process: every claim is released in a
`finally`, including when a delivery raises or returns early. Its size is
bounded by the number of deliveries running concurrently — on this device, one
— and never by how many scans have ever been synced. That matters because this
backend runs for weeks at a time on a device nobody restarts.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Set

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_in_flight: Set[int] = set()


@contextmanager
def claim_result(result_id: int):
    """Claim the exclusive right to deliver `result_id`.

    Yields True if the claim was granted and False if another delivery already
    holds it, so the caller decides what to do about losing — the worker skips
    the record, the manual re-sync reports it back to the operator. The claim
    is always released, including when the delivery raises.
    """
    with _lock:
        granted = result_id not in _in_flight
        if granted:
            _in_flight.add(result_id)

    if not granted:
        logger.info(
            "Result %s is already being delivered — not starting a second attempt.",
            result_id,
        )

    try:
        yield granted
    finally:
        if granted:
            with _lock:
                _in_flight.discard(result_id)
