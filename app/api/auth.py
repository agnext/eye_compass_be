"""
Authentication.

Port of the legacy login path (main.py:591-641 + api_handle.py:59-102), which
was online-first with an offline fallback:

  1. Try Qualix with <username><domain>. On success, cache the credentials
     locally (database.py:158-176) and kick off the config sync.
  2. If Qualix is unreachable, fall back to the cached `creds` table
     (database.py:326-345) so the operator can still work offline.

The previous version did neither: it compared against a single hardcoded
env credential pair and returned the literal string "dummy_offline_token",
so only one operator could ever log in and nothing was actually verified.

settings.AUTH_PROVIDER selects which identity provider tier 1 uses:

  "legacy"   — Qualix directly, exactly as above. Unchanged.
  "keycloak" — this organization's Keycloak instance, which already fronts
               Qualix org-wide via the Assurance gateway. Same userbase.

Tiers 2 and 3 are identical either way, and deliberately so: the cached-hash
check is what lets an operator sign in with no connectivity, including after
they have explicitly logged out, and the device-credential pair is the
break-glass path for a device that has never been online at all.
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.core.security import require_session, session_store
from app.models.schema import Creds
from app.services.keycloak_service import keycloak_service
from app.services.sync_service import sync_service

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


def _hash(password: str) -> str:
    """Cached credentials are hashed. Legacy stored them in clear text; there is
    no reason to reproduce that when the offline check works just as well
    against a digest."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _qualix_username(username: str) -> str:
    """Legacy appends the domain before authenticating (main.py:598)."""
    if "@" in username:
        return username
    return f"{username}{settings.QUALIX_USER_DOMAIN}"


def _cache_credentials(
    db: Session, username: str, password: str,
    keycloak_user_id: str = "", refresh_token: str = "", operator_id: str = "",
):
    """Port of write_creds (database.py:158-176) — one row, replaced each time.

    keycloak_user_id/refresh_token ride along whenever this follows a real
    Keycloak login (tier 1) — see the Creds model's docstring for why. A
    legacy-path caller passes neither, which is correct: that path has no
    Keycloak identity to carry.

    operator_id is different: it is Qualix's own user_id, not a Keycloak
    concept, so BOTH tier-1 paths pass it (from /user/keycloak-profile under
    Keycloak, from the direct Qualix login response under legacy — see
    _login_keycloak / _login_legacy). Blank preserves the previous cached
    value rather than wiping it, since not every caller of this function has
    just fetched a fresh one (e.g. a bare password re-cache).
    """
    try:
        existing = db.query(Creds).filter(Creds.user == username).first()
        if not operator_id and existing:
            operator_id = existing.operator_id or ""
        db.query(Creds).delete()
        db.add(Creds(
            user=username, password=_hash(password),
            keycloak_user_id=keycloak_user_id, refresh_token=refresh_token,
            operator_id=operator_id,
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not cache credentials: %s", exc)


def _log_operator_id_change(db: Session, username: str, new_operator_id: str):
    """Logs it when a fresh online login's operator_id differs from what was
    cached last time, so a Qualix-side account remap (or a mistaken one) is
    visible in the logs rather than silently overwriting the old value.
    """
    if not new_operator_id:
        return
    row = db.query(Creds).filter(Creds.user == username).first()
    old_operator_id = (row.operator_id or "") if row else ""
    if old_operator_id and old_operator_id != new_operator_id:
        logger.warning(
            "[AUTH] operator_id changed for %s: %s -> %s. Storing the new value.",
            username, old_operator_id, new_operator_id,
        )


def _update_cached_operator_id(db: Session, username: str, operator_id: str):
    """Updates just the operator_id column on the existing Creds row.

    Deliberately narrower than _cache_credentials: this runs after a fast-path
    login is confirmed by a background Keycloak check, which — unlike a full
    tier-1 login — has never re-cached the password/keycloak_user_id/
    refresh_token here, and this must not start silently doing that as a side
    effect of adding operator_id.
    """
    if not operator_id:
        return
    try:
        db.query(Creds).filter(Creds.user == username).update({"operator_id": operator_id})
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not update cached operator_id for %s: %s", username, exc)


def _check_cached(db: Session, username: str, password: str) -> bool:
    """Port of check_creds (database.py:326-345)."""
    row = db.query(Creds).filter(Creds.user == username).first()
    if not row or not row.password:
        return False
    return hmac.compare_digest(row.password, _hash(password))


def _cached_keycloak_identity(db: Session, username: str) -> dict:
    """The identity riding along with the cached password, if any.

    Called whenever a login is about to be accepted from the cached hash
    (tier 2, or the fast path) rather than a fresh online check — so that
    session starts out carrying whatever refresh token and operator_id this
    device last obtained for this exact account, instead of nothing at all.
    operator_id is carried forward unchanged here on purpose: it only ever
    gets refreshed by a real online login (see _login_keycloak / _login_legacy),
    never invented or guessed for an offline one. _check_cached already
    guarantees the username matches this row (Creds is one row for the whole
    device), so there is no ambiguity about whose identity this is.
    """
    row = db.query(Creds).filter(Creds.user == username).first()
    if not row:
        return {}
    return {
        "keycloak_user_id": row.keycloak_user_id or "",
        "refresh_token": row.refresh_token or "",
        "operator_id": row.operator_id or "",
    }


def _clear_cached_credentials(db: Session):
    """Throw away the saved password after Keycloak rejects it.

    This is what stops a stale password from working offline forever. It also
    forces the next login through Keycloak, since the fast path has nothing
    left to match — no extra flag needed, the absence of the row is the rule.

    The operator is not stranded: the fixed device credential (tier 3) still
    works, and a successful online login caches a fresh password again.
    """
    try:
        db.query(Creds).delete()
        db.commit()
        logger.warning(
            "[AUTH] Cleared the saved password on this device — Keycloak rejected "
            "it, so it must not keep working offline. The next login has to go "
            "through Keycloak."
        )
    except Exception as exc:
        db.rollback()
        logger.error("Could not clear cached credentials: %s", exc)


def _verify_cached_login_in_background(username: str, password: str, token: str):
    """Check a fast-path login against Keycloak, after the operator is already in.

    The login itself was accepted from the password saved on this device, which
    is instant and works with no connectivity. That is only safe because of
    this: the very same credentials are then put to Keycloak for real, and the
    answer is acted on.

    Deliberately a password login rather than redeeming a refresh token. A
    changed password is the case this exists to catch, and Keycloak's offline
    refresh tokens generally survive a password change — so redeeming one would
    happily succeed and prove nothing. Sending the actual typed password is the
    only thing that genuinely tests it.

      * Confirmed   -> promote the session; it becomes a normal online session.
      * Rejected    -> flag it and drop the saved password. The operator keeps
                       working and is asked to sign in again at the Home
                       screen, never mid-task.
      * Unreachable -> change nothing. The device is offline, which says
                       nothing about the account.
    """
    db = SessionLocal()
    try:
        claims = keycloak_service.login(username, password)

        if claims:
            profile = keycloak_service.fetch_qualix_profile(claims.get("access_token", ""))
            if not profile and keycloak_service.last_profile_check_was_explicit_rejection:
                logger.warning(
                    "[AUTH] Background check: %s is no longer a registered Qualix "
                    "user. Flagging for re-login.", username,
                )
                session_store.flag_needs_relogin(
                    token, reason="no longer a registered Qualix user"
                )
                _clear_cached_credentials(db)
                return

            qualix_user = (profile or {}).get("user") or {}
            if qualix_user.get("first_name"):
                claims["first_name"] = qualix_user["first_name"]
            operator_id = str(qualix_user.get("user_id") or "")
            claims["operator_id"] = operator_id
            _log_operator_id_change(db, username, operator_id)
            logger.info(
                "[AUTH] Background check CONFIRMED %s with Keycloak — the fast "
                "login was legitimate.", username,
            )
            session_store.promote_to_online(token, claims)
            _update_cached_operator_id(db, username, operator_id)
            # Already running in the background here, so this is a direct
            # call rather than another scheduled task — see
            # _seed_password_baseline_in_background's docstring.
            _seed_password_baseline_in_background(token, claims.get("sub", ""))
            sync_service.sync_commodity_config(db)
            return

        if keycloak_service.last_login_was_explicit_rejection:
            logger.warning(
                "[AUTH] Background check REJECTED %s — Keycloak says these "
                "credentials are no longer valid (password changed, or account "
                "disabled). Flagging for re-login at the Home screen.", username,
            )
            session_store.flag_needs_relogin(
                token, reason="Keycloak rejected the credentials used to sign in"
            )
            _clear_cached_credentials(db)
        else:
            logger.info(
                "[AUTH] Background check could not reach Keycloak for %s — leaving "
                "the session alone. It will be checked again when there is a network.",
                username,
            )
            # Settle it as offline so the Home screen can say so. Nothing else
            # about the session changes: being offline is not a verdict on the
            # account, only on the network.
            session_store.mark_offline(token)
    except Exception as exc:
        logger.error("Background credential check failed for %s: %s", username, exc)
    finally:
        db.close()


def _seed_password_baseline_in_background(token: str, keycloak_user_id: str):
    """Right after a fresh tier-1 login, read today's password-credential
    timestamp and store it as this session's baseline.

    Runs in the background, after the login response is already sent — an
    extra Admin API round trip has no business adding latency to a login that
    already succeeded on its own terms. Best-effort: if the Admin API is
    unreachable or not yet permitted, this simply does nothing and the
    baseline is established later the normal way, by the daily worker's first
    successful check instead. Nothing about login's own pass/fail behavior
    depends on this succeeding.
    """
    if not keycloak_user_id:
        return
    try:
        status = keycloak_service.fetch_account_status(keycloak_user_id)
        if status:
            session_store.set_password_baseline(token, status["password_created_at"])
    except Exception as exc:
        logger.error("Could not seed password baseline for token %s: %s", token, exc)


def _sync_config_in_background(username: str = None, password: str = None):
    """Refresh commodities/vendors/brands/surveyors after a successful login.

    Runs after the response. Opens its own session — the request-scoped one is
    closed by the time a BackgroundTask executes.

    Credentials are only used on the legacy path, where the config fetch rides
    on the operator's own Qualix login. Under Keycloak they are omitted: the
    fetch authenticates as the fixed sync account instead, so it works even
    when the operator signed in offline and no Keycloak token exists.
    """
    db = SessionLocal()
    try:
        sync_service.sync_commodity_config(db, username=username, password=password)
    except Exception as exc:
        logger.error("Background config sync failed: %s", exc)
    finally:
        db.close()


def _offline_tiers(db: Session, username: str, password: str) -> dict:
    """Tiers 2 and 3, shared by both providers. Returns a response dict, or
    None if neither matches.

    Tier 2 works whether the device merely lost connectivity or the operator
    deliberately logged out — /logout does not clear the cached hash, which is
    the whole reason the hash is cached in the first place.
    """
    # 2. Offline fallback against the cached credentials.
    logger.info("[AUTH] Tier 2: checking %s against the saved password on this device.", username)
    if _check_cached(db, username, password):
        logger.info(
            "[AUTH] TIER 2 SUCCESS — offline login for %s from cached credentials "
            "(%s was unreachable or rejected it). Session mode=offline.",
            username, settings.AUTH_PROVIDER.upper(),
        )
        # Carries forward whatever refresh token this device last obtained
        # for this account, so this session isn't a total unknown to the
        # daily worker just because THIS login happened to go through the
        # cached hash — see the Creds model's docstring.
        identity = _cached_keycloak_identity(db, username)
        token = session_store.create(username, mode="offline", **identity)
        return {
            "status": "success",
            "mode": "offline",
            "token": token,
            "username": username,
            "message": "Signed in offline. Results will sync when connectivity returns.",
        }

    # 3. Last resort: the device credentials from .env / config.INI. This keeps
    #    a brand-new device usable before its first successful online login.
    if (
        settings.EMERGENCY_LOGIN_USERNAME
        and settings.EMERGENCY_LOGIN_PASSWORD
        and username == _qualix_username(settings.EMERGENCY_LOGIN_USERNAME)
        and hmac.compare_digest(password, settings.EMERGENCY_LOGIN_PASSWORD)
    ):
        logger.info(
            "[AUTH] TIER 3 SUCCESS — offline login for %s from the fixed device "
            "credentials. Session mode=offline-device.",
            username,
        )
        token = session_store.create(username, mode="offline-device")
        return {
            "status": "success",
            "mode": "offline",
            "token": token,
            "username": username,
            "message": "Signed in offline using device credentials.",
        }

    logger.warning(
        "[AUTH] ALL TIERS FAILED for %s — no provider accepted it, no saved "
        "password matched, and it is not the device credential. Login refused.",
        username,
    )
    return None


def _login_keycloak(
    db: Session, background_tasks: BackgroundTasks, username: str, password: str
) -> dict:
    """Tier 1 against Keycloak, then the shared offline tiers.

    The username is passed through verbatim — Keycloak expects the full
    username, unlike Qualix which needed the @domain appended.
    """
    # Fast path: this device already has a saved password for this operator and
    # it matches. Let them in now and confirm with Keycloak afterwards.
    #
    # Worth it because the slow case is not the online one — it is the offline
    # one, where reaching Keycloak fails only after the network times out and
    # the operator waits ~10 seconds at a spinner for a login that was always
    # going to come from the saved password anyway.
    #
    # Safe because the same credentials are verified in the background straight
    # after (see _verify_cached_login_in_background). Anything Keycloak objects
    # to is caught within seconds, and acted on at the Home screen — so a
    # changed password or disabled account cannot quietly keep working, and an
    # operator mid-batch is still never interrupted.
    #
    # A rejected credential is deleted, so the fast path stops matching and the
    # next login is forced through Keycloak.
    if _check_cached(db, username, password):
        logger.info(
            "[AUTH] FAST LOGIN for %s — matched the password saved on this device. "
            "Signed in now; confirming with Keycloak in the background.",
            username,
        )
        # "pending", not "offline": at this instant we genuinely do not know
        # whether this device has a network. Settling that is the background
        # check's job, and until it does, the Home screen must not claim
        # either way.
        #
        # The carried-forward refresh token is a safety net, not the normal
        # path: _verify_cached_login_in_background below resolves this within
        # seconds with a fresh one either way. It only matters if that
        # background task never completes (e.g. the process dies first) —
        # without it, a session stuck in "pending" would otherwise have
        # nothing at all for the daily worker to check.
        identity = _cached_keycloak_identity(db, username)
        token = session_store.create(username, mode="pending", **identity)
        background_tasks.add_task(
            _verify_cached_login_in_background, username, password, token
        )
        return {
            "status": "success",
            # Not "offline": we do not yet know whether this device has a
            # network, and claiming it does not would show the operator a
            # "results will sync later" notice that may well be wrong.
            "mode": "cached",
            "token": token,
            "username": username,
        }

    logger.info(
        "[AUTH] Login attempt for %s. Provider=KEYCLOAK. Tier 1: asking Keycloak (%s).",
        username, settings.KEYCLOAK_REALM,
    )
    claims = keycloak_service.login(username, password)

    if claims:
        cache_username = claims.get("username") or username

        # Keycloak alone only proves the password is valid on the shared
        # CentralIAM realm — it says nothing about whether this person is
        # actually a Qualix operator. Anyone else on that realm (a customer
        # login, an unrelated internal tool) would otherwise pass straight
        # through. This is the actual membership check.
        profile = keycloak_service.fetch_qualix_profile(claims.get("access_token", ""))
        if not profile and keycloak_service.last_profile_check_was_explicit_rejection:
            logger.warning(
                "[AUTH] TIER 1 REJECTED for %s — authenticated fine by Keycloak, "
                "but is not a registered Qualix user. Login refused.",
                cache_username,
            )
            raise HTTPException(status_code=401, detail="Invalid credentials")

        qualix_user = (profile or {}).get("user") or {}
        first_name = qualix_user.get("first_name") or claims.get("first_name", "")
        customer_name = qualix_user.get("customer_name", "")
        operator_id = str(qualix_user.get("user_id") or "")

        _log_operator_id_change(db, username, operator_id)

        # Cached under what the operator actually typed, not Keycloak's
        # canonical preferred_username: tier 2 looks the row up by whatever
        # they type at the offline login screen, and the two can differ.
        _cache_credentials(
            db, username, password,
            keycloak_user_id=claims.get("sub", ""),
            refresh_token=claims.get("refresh_token", ""),
            operator_id=operator_id,
        )
        # Same refresh legacy does on every login — without this a device
        # switched to Keycloak would keep whatever commodities, vendors and
        # brands it had at switchover, with nothing ever updating them. No
        # credentials passed: the fetch uses the fixed sync account.
        background_tasks.add_task(_sync_config_in_background)
        logger.info(
            "[AUTH] TIER 1 SUCCESS — online login for %s via KEYCLOAK "
            "(realm=%s, client=%s, roles=%s, qualix_customer=%s). Session "
            "mode=online, offline password cached, config refresh queued.",
            cache_username, settings.KEYCLOAK_REALM, settings.KEYCLOAK_CLIENT_ID,
            claims.get("roles", []), customer_name or "unknown",
        )
        token = session_store.create(
            cache_username,
            mode="online",
            first_name=first_name,
            email=claims.get("email", ""),
            roles=claims.get("roles", []),
            # Held for the daily revalidation worker; never sent to the client.
            refresh_token=claims.get("refresh_token", ""),
            keycloak_user_id=claims.get("sub", ""),
            operator_id=operator_id,
        )
        # Seeds password_credential_created_at now, rather than leaving the
        # daily worker's first pass to plant it with nothing to compare
        # against. See _seed_password_baseline_in_background's docstring.
        background_tasks.add_task(
            _seed_password_baseline_in_background, token, claims.get("sub", "")
        )
        return {
            "status": "success",
            "mode": "online",
            "token": token,
            "username": cache_username,
            "first_name": first_name,
            "customer_name": customer_name,
        }

    result = _offline_tiers(db, username, password)
    if result:
        return result

    raise HTTPException(status_code=401, detail="Invalid credentials")


def _login_legacy(
    db: Session, background_tasks: BackgroundTasks, username: str, password: str
) -> dict:
    """Tier 1 against Qualix directly — today's behavior, unchanged."""
    qualix_user = _qualix_username(username)
    logger.info(
        "[AUTH] Login attempt for %s. Provider=QUALIX (legacy). Tier 1: asking Qualix.",
        qualix_user,
    )

    online = False
    try:
        online = sync_service.login_qualix(qualix_user, password)
    except Exception as exc:
        logger.warning("Qualix login attempt failed (treating as offline): %s", exc)

    if online:
        # sync_service.customer_id is misnamed — it's actually Qualix's
        # user_id (api_handle.py:91 in the legacy source: `self.customer_id =
        # self.response.json()['user']['user_id']`), the same field
        # /user/keycloak-profile returns as user.user_id under Keycloak mode.
        operator_id = str(sync_service.customer_id or "")
        _log_operator_id_change(db, qualix_user, operator_id)
        _cache_credentials(db, qualix_user, password, operator_id=operator_id)
        background_tasks.add_task(_sync_config_in_background, qualix_user, password)
        logger.info(
            "[AUTH] TIER 1 SUCCESS — online login for %s via QUALIX (legacy direct "
            "login). Session mode=online, offline password cached.",
            qualix_user,
        )
        token = session_store.create(
            qualix_user,
            mode="online",
            first_name=sync_service.first_name,
            customer_name=sync_service.customer_name,
            operator_id=operator_id,
        )
        return {
            "status": "success",
            "mode": "online",
            "token": token,
            "username": qualix_user,
            "first_name": sync_service.first_name,
            "customer_name": sync_service.customer_name,
        }

    result = _offline_tiers(db, qualix_user, password)
    if result:
        return result

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/login")
def login(
    request: LoginRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    username = (request.username or "").strip()
    password = request.password or ""

    if not username or not password:
        raise HTTPException(status_code=422, detail="Username and password are required")

    if settings.AUTH_PROVIDER == "keycloak":
        return _login_keycloak(db, background_tasks, username, password)

    return _login_legacy(db, background_tasks, username, password)


@router.post("/logout")
def logout(request: Request):
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        session_store.revoke(header[7:].strip())
    return {"status": "success"}


@router.get("/me")
def me(session: dict = Depends(require_session)):
    """Current session.

    Two different re-login signals, and the difference matters to the operator:

      needs_relogin      — Keycloak actively refused the account (disabled,
                           deleted, or the password was changed). The
                           credentials on this device are wrong now, so the
                           Home screen blocks with a dialog.
      relogin_suggested  — the stored offline token aged out while the device
                           was out of contact, but the account itself is
                           confirmed fine. Home shows a banner they can
                           ignore; signing in again just restores a live
                           token.

    Either way the session stays usable, and either way only the Home screen
    acts on it, so an operator part-way through a batch is never cut off
    mid-scan.
    """
    return {
        "username": session.get("username"),
        "mode": session.get("mode"),
        "first_name": session.get("first_name", ""),
        "customer_name": session.get("customer_name", ""),
        "needs_relogin": bool(session.get("needs_relogin", False)),
        "relogin_suggested": bool(session.get("relogin_suggested", False)),
    }
