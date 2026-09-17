"""
Periodic session revalidation.

A local session is deliberately self-sufficient: once an operator logs in,
the device keeps working without needing to reach any
identity provider again. That is the whole point — this device regularly has
no connectivity, and a login must not evaporate because of it.

The cost of that is a session which knows nothing about the outside world. If
an operator's Keycloak account is disabled, nothing here would notice. So
whenever the device *does* have connectivity, this quietly checks each
session once a day, in two steps that each answer a different question:

  1. Redeem the stored refresh token. This is what keeps the offline token's
     own 30-day idle clock from running out, and Keycloak also refuses it
     outright for a disabled/deleted account — a question a refresh grant
     genuinely can answer. What it CANNOT answer is whether the password
     itself changed: confirmed by direct test, an offline refresh token
     survives a password change untouched, since redeeming one was never a
     password check to begin with.
  2. Ask Keycloak's Admin REST API directly whether the account is enabled
     and when its password credential was last created
     (`keycloak_service.fetch_account_status`). Comparing that timestamp to
     what was recorded at login is what actually catches a password change —
     authoritative, and it never needs or sees the password itself. Requires
     the Keycloak client to have a service account with the realm-management
     `view-users` role; if that hasn't been granted yet, this step simply
     comes back inconclusive and step 1's result still stands on its own.

  * Keycloak confirms both    -> record that it was verified just now, and
                                 store the rotated refresh token. The operator
                                 notices nothing and stays logged in.
  * The account is genuinely
    refused — disabled,
    deleted, or its password
    was changed               -> flag needs_relogin. The credentials on this
                                 device are actually wrong now, so the
                                 frontend blocks with a dialog. Still only at
                                 the Home screen; see below.
  * The token aged out but
    the account is confirmed
    fine                      -> flag relogin_suggested instead. Nothing is
                                 wrong with the operator or their password —
                                 the device was simply out of contact past the
                                 realm's Offline Session Idle limit, so there
                                 is no live token left to confirm them with
                                 silently. The frontend shows a banner they
                                 can ignore, not a wall. Signing in again
                                 restores a fresh token; if they turn out to
                                 still be offline, login just falls through to
                                 the offline tiers and costs them nothing.
  * Unreachable/inconclusive  -> change nothing at all. An offline device must
                                 never be punished for being offline, which is
                                 exactly the case this whole design exists to
                                 support. The one exception: if Keycloak
                                 refused the token and the Admin API could not
                                 then say why, that is flagged hard rather
                                 than softened — an account that could not be
                                 confirmed must not be waved through.

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


def _password_changed(stored, current) -> bool:
    """Has the account's password been replaced since this session recorded it?

    Only answerable when both timestamps exist. A missing stored value means
    no baseline has been taken yet (the Admin API had never been reachable for
    this session before), which is not evidence of a change.
    """
    return stored is not None and current is not None and stored != current


def _handle_refresh_rejection(session: dict, summary: dict):
    """Keycloak refused to redeem this session's refresh token. Work out which
    of two very different reasons it was, and answer in kind.

    The refusal itself is ambiguous — Keycloak returns invalid_grant for a
    disabled account and for a token that simply sat unused past the realm's
    Offline Session Idle limit alike, and the error_description wording is not
    something to build behavior on. So rather than guess from the message, ask
    the Admin API what is actually true of the account:

      * account disabled/deleted, or its password changed -> the credentials
        on this device are genuinely wrong now. Hard flag: a blocking dialog.
      * account perfectly fine -> the token aged out while the device was out
        of contact, which is nobody's fault and nothing is wrong with the
        operator's credentials. Soft flag: a banner they can ignore.
      * cannot tell -> assume the worse of the two. Quietly downgrading an
        account we could not confirm to a dismissible banner is the one
        mistake here with a real consequence.
    """
    status = None
    if session["keycloak_user_id"]:
        status = keycloak_service.fetch_account_status(session["keycloak_user_id"])

    if status is None:
        logger.warning(
            "[REVALIDATE] Keycloak refused %s's token and the Admin API could not "
            "say why — flagging for re-login rather than assuming the account is "
            "fine.", session["username"],
        )
        session_store.flag_needs_relogin(
            session["token"],
            reason="Keycloak refused the token and the account could not be checked",
        )
        summary["flagged"] += 1
        return

    if not status["enabled"]:
        logger.warning(
            "[REVALIDATE] Keycloak refused %s's token because the account is "
            "disabled or deleted — flagging for re-login.", session["username"],
        )
        session_store.flag_needs_relogin(
            session["token"], reason="the account is disabled or deleted"
        )
        summary["flagged"] += 1
        return

    if _password_changed(
        session["password_credential_created_at"], status["password_created_at"]
    ):
        logger.warning(
            "[REVALIDATE] Keycloak refused %s's token and the account's password "
            "has changed — flagging for re-login.", session["username"],
        )
        session_store.flag_needs_relogin(
            session["token"], reason="the account's password was changed"
        )
        summary["flagged"] += 1
        return

    logger.info(
        "[REVALIDATE] %s's offline token reached the realm's idle limit while this "
        "device was out of contact, but the account itself is confirmed fine — "
        "suggesting a re-login rather than forcing one.",
        session["username"],
    )
    session_store.suggest_relogin(
        session["token"],
        reason="the offline token reached its idle limit while the device was offline",
    )
    summary["suggested"] += 1


def _run_one_cycle() -> dict:
    """One revalidation pass. Runs in a worker thread."""
    summary = {"checked": 0, "verified": 0, "flagged": 0, "suggested": 0, "unreachable": 0, "offline_flagged": 0}

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
        # Step 1: redeem the stored refresh token. Two jobs at once — this is
        # what keeps the offline token's own 30-day idle clock from running
        # out, and Keycloak also refuses this outright for a disabled/deleted
        # account, which is a question a refresh grant CAN answer correctly.
        try:
            claims = keycloak_service.refresh(session["refresh_token"])
        except Exception as exc:
            logger.error("Revalidation failed for %s: %s", session["username"], exc)
            summary["unreachable"] += 1
            continue

        if not claims:
            if keycloak_service.last_refresh_was_explicit_rejection:
                _handle_refresh_rejection(session, summary)
            else:
                # Unreachable. Explicitly a no-op: the session keeps its
                # previous verification time and we try again next cycle.
                logger.info(
                    "[REVALIDATE] Could not reach Keycloak for %s — leaving the "
                    "session untouched (an offline device must not be logged out). "
                    "Will try again next cycle.",
                    session["username"],
                )
                summary["unreachable"] += 1
            continue

        new_refresh_token = claims.get("refresh_token", "")

        # Step 2: the refresh above only proves the account/session is still
        # valid — it says nothing about whether the password itself changed.
        # Confirmed by direct test: an offline refresh token survives a
        # password change untouched, since redeeming one was never a password
        # check to begin with. Ask Keycloak's Admin API directly instead,
        # which answers that question authoritatively and never needs or sees
        # the password.
        status = None
        if session["keycloak_user_id"]:
            status = keycloak_service.fetch_account_status(session["keycloak_user_id"])

        if status is None:
            # Admin API unreachable, unconfigured, or not yet permitted on
            # this realm (see fetch_account_status's docstring). The refresh
            # above already proved the account/session are fine, so record
            # that much; the password-change check is simply retried next
            # cycle rather than blocking everything on it.
            session_store.mark_verified(session["token"], refresh_token=new_refresh_token)
            summary["verified"] += 1
            continue

        if not status["enabled"]:
            logger.warning(
                "[REVALIDATE] Admin API reports %s is disabled — flagging for "
                "re-login.", session["username"],
            )
            session_store.flag_needs_relogin(
                session["token"], reason="Keycloak's Admin API reports the account is disabled"
            )
            summary["flagged"] += 1
            continue

        current_pw_ts = status["password_created_at"]
        if _password_changed(session["password_credential_created_at"], current_pw_ts):
            logger.warning(
                "[REVALIDATE] %s's password was changed (Admin API password "
                "credential timestamp no longer matches what was recorded at "
                "login) — flagging for re-login.",
                session["username"],
            )
            session_store.flag_needs_relogin(
                session["token"], reason="the account's password was changed"
            )
            summary["flagged"] += 1
            continue

        # Either unchanged, or this is the first check ever able to reach the
        # Admin API for this session — either way, this timestamp becomes (or
        # stays) the baseline the next check compares against.
        session_store.mark_verified(
            session["token"],
            refresh_token=new_refresh_token,
            password_credential_created_at=current_pw_ts,
            password_baseline_checked=True,
        )
        summary["verified"] += 1

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
    empty = {"checked": 0, "verified": 0, "flagged": 0, "suggested": 0, "unreachable": 0, "offline_flagged": 0}
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
                    "%s invited to sign in again, %s unreachable; %s offline "
                    "session(s) asked to sign in again.",
                    summary["checked"], summary["verified"],
                    summary["flagged"], summary["suggested"],
                    summary["unreachable"], summary["offline_flagged"],
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
