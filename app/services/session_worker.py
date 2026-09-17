"""
Periodic session revalidation.

A local session is deliberately self-sufficient: once an operator logs in,
the device keeps working without needing to reach any
identity provider again. That is the whole point — this device regularly has
no connectivity, and a login must not evaporate because of it.

The cost of that is a session which knows nothing about the outside world. If
an operator's Keycloak account is disabled, nothing here would notice. So
whenever the device *does* have connectivity, this quietly redeems each
session's stored refresh token once a day and acts on the answer:

  * Keycloak confirms it      -> record that it was verified just now, and
                                 store the rotated refresh token. The operator
                                 notices nothing and stays logged in.
  * Keycloak explicitly says
    no (account disabled or
    deleted, offline token
    idle past its window)     -> flag the session for re-login. It stays
                                 usable; see below.
  * Keycloak is unreachable   -> change nothing at all. An offline device must
                                 never be punished for being offline, which is
                                 exactly the case this whole design exists to
                                 support.

A session that signed in offline has no refresh token, so none of the above
can apply to it — there is nothing to redeem. It is left alone for as long as
the device stays offline. Once Keycloak is reachable again it is flagged for
re-login, because that is the only way such a session can ever be confirmed:
by the operator typing their password once while there is a network to check
it against. That re-login costs them nothing if they are genuinely still
offline — it simply falls through to the offline tiers again.

Nothing here ever revokes a session outright, and nothing here ever ends one
because time has passed. An operator could be part-way through a batch scan or
a data-collection run, and killing their token would lose that work. The flag
is acted on by the frontend at a safe checkpoint instead — only on the Home
screen, which they can only reach between tasks.

A session is checked ONCE A DAY, and `last_verified_at` on the session row is
what records that — so the rule holds however many times, and from wherever, a
pass is started. Two things start one:

  * this worker's own timer (SESSION_REVALIDATION_INTERVAL_HOURS), which wakes
    more often than daily only so a device that was offline at the first
    attempt gets another chance before tomorrow, and
  * sync_worker.py, after a sync that actually delivered something — proof the
    network is up right now, which is the best possible moment to look.

Both go through run_revalidation_now(), so neither needs to know about the
other, and a pass that finds every session already confirmed today contacts
nobody. Their schedules stay independent: sync_worker keeps its own cadence
and knows nothing about auth beyond calling that one function.
"""

import asyncio
import logging
import threading
from datetime import datetime

from app.core.config import settings
from app.core.security import session_store
from app.services.keycloak_service import keycloak_service

logger = logging.getLogger(__name__)

# Two things can start a pass now — this worker's own timer, and a successful
# sync — so they could in principle overlap. Nothing would corrupt if they did
# (each opens its own database session), but a session could be refreshed twice
# for no reason, so passes are serialised.
_cycle_lock = threading.Lock()


def _start_of_today() -> datetime:
    """Midnight today, UTC.

    A session confirmed after this point has already had its check for the
    day. UTC rather than local time because every other timestamp in this
    system is UTC; the only visible effect is where the day boundary falls.
    """
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)


def _run_one_cycle() -> dict:
    """One revalidation pass. Runs in a worker thread."""
    summary = {"checked": 0, "verified": 0, "flagged": 0, "unreachable": 0, "offline_flagged": 0}

    # Skip anything Keycloak already confirmed today. One successful check a
    # day is the point; the repeated wake-ups exist only so a device that was
    # offline earlier gets another chance before tomorrow.
    sessions = session_store.online_sessions_with_refresh_token(
        skip_if_verified_after=_start_of_today()
    )
    summary["checked"] = len(sessions)
    if sessions:
        logger.info(
            "[REVALIDATE] Check starting — %s online session(s) due for verification "
            "with Keycloak.",
            len(sessions),
        )
    else:
        logger.info(
            "[REVALIDATE] Nothing due — every online session has already been "
            "confirmed with Keycloak today (or there are none)."
        )

    for session in sessions:
        try:
            claims = keycloak_service.refresh(session["refresh_token"])
        except Exception as exc:
            logger.error("Revalidation failed for %s: %s", session["username"], exc)
            summary["unreachable"] += 1
            continue

        if claims:
            session_store.mark_verified(
                session["token"],
                refresh_token=claims.get("refresh_token", ""),
            )
            summary["verified"] += 1
        elif keycloak_service.last_refresh_was_explicit_rejection:
            logger.warning(
                "[REVALIDATE] Keycloak REJECTED %s (account disabled/deleted, or the "
                "token went unused too long) — flagging for re-login.",
                session["username"],
            )
            session_store.flag_needs_relogin(
                session["token"],
                reason="Keycloak rejected the account, or the token went unused too long",
            )
            summary["flagged"] += 1
        else:
            # Unreachable. Explicitly a no-op: the session keeps its previous
            # verification time and we try again next cycle.
            logger.info(
                "[REVALIDATE] Could not reach Keycloak for %s — leaving the session "
                "untouched (an offline device must not be logged out). Will try again "
                "next cycle.",
                session["username"],
            )
            summary["unreachable"] += 1

    _flag_offline_sessions_if_back_online(summary)
    return summary


def _flag_offline_sessions_if_back_online(summary: dict):
    """Ask offline-signed-in operators to sign in again, once there is a network.

    Their session has no refresh token, so it can never be confirmed silently.
    Rather than let it run forever unchecked, prompt for one re-login the first
    time Keycloak is actually reachable. The reachability probe is only made
    when such a session exists, so a fleet of normally-online devices never
    makes this call at all.
    """
    pending = session_store.unverifiable_sessions()
    if not pending:
        return

    if not keycloak_service.is_reachable():
        logger.info(
            "[REVALIDATE] %s offline session(s) still cannot be confirmed — Keycloak "
            "is unreachable. Leaving them alone; the operator keeps working.",
            len(pending),
        )
        return

    for session in pending:
        logger.info(
            "[REVALIDATE] %s signed in offline (mode=%s) and Keycloak is reachable "
            "again — asking them to sign in once at the Home screen so the account "
            "can actually be checked.",
            session["username"], session["mode"],
        )
        session_store.flag_needs_relogin(
            session["token"],
            reason="signed in offline and Keycloak is reachable again, so the account can now be checked",
        )
        summary["offline_flagged"] += 1


def run_revalidation_now(trigger: str) -> dict:
    """Run a pass outside this worker's own timer. Blocking; call in a thread.

    Exists so a successful sync can trigger a check: a sync that delivered
    something is proof the network is up right now, which is exactly the
    moment a check is worth making. Cheap to call — if every session was
    already confirmed today, the pass finds nothing due and does nothing.

    Returns an empty summary without doing anything if revalidation is off or
    the device is not on Keycloak, so callers need no guards of their own.
    """
    empty = {"checked": 0, "verified": 0, "flagged": 0, "unreachable": 0, "offline_flagged": 0}
    if not settings.SESSION_REVALIDATION_ENABLED or settings.AUTH_PROVIDER != "keycloak":
        return empty

    # Do not queue up behind a pass that is already running: whatever it is
    # doing makes this one redundant.
    if not _cycle_lock.acquire(blocking=False):
        logger.info("[REVALIDATE] Skipping the pass triggered by %s — one is already running.", trigger)
        return empty
    try:
        logger.info("[REVALIDATE] Pass triggered by %s.", trigger)
        return _run_one_cycle()
    finally:
        _cycle_lock.release()


async def session_revalidation_worker():
    """Loop forever, revalidating sessions on the configured interval."""
    interval = max(1, settings.SESSION_REVALIDATION_INTERVAL_HOURS) * 60 * 60

    # Let the app finish starting before the first pass.
    await asyncio.sleep(60)

    while True:
        try:
            summary = await asyncio.to_thread(
                run_revalidation_now, "the scheduled timer"
            )
            if summary["checked"] or summary["offline_flagged"]:
                logger.info(
                    "[REVALIDATE] Done: %s checked -> %s verified, %s flagged, "
                    "%s unreachable; %s offline session(s) asked to sign in again.",
                    summary["checked"], summary["verified"],
                    summary["flagged"], summary["unreachable"],
                    summary["offline_flagged"],
                )
        except asyncio.CancelledError:
            logger.info("Session revalidation worker stopped.")
            raise
        except Exception as exc:
            logger.error("Session revalidation worker error: %s", exc)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Session revalidation worker stopped.")
            raise
