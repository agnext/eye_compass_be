"""
Session tokens.

The legacy app was a single-process kiosk: the login screen physically gated
the page change, so nothing else needed protecting. This backend listens on a
network socket, so an opaque bearer token is issued at login and required by
the endpoints that touch hardware or data.

Tokens live in the database rather than in memory. They were process-local
originally, which quietly broke the device's own promise: an operator was
supposed to stay logged in for weeks, but every service restart or reboot
dropped every session, so the real lifetime was "until the next restart".
Persisting them also gives the daily revalidation worker something durable to
revalidate.

A session never expires on its own. The only thing that ends one is Keycloak
explicitly saying the account is no longer good — which sets `needs_relogin`.
Time alone proves nothing: a device that has been in a shed with no signal for
two months has exactly the same evidence about its operator's account as it had
on day one, so logging them out would be punishing them for the network rather
than for anything about their account.

Two earlier designs got this wrong and are worth recording so they are not
reintroduced. The first stored an `expires_at` that the daily worker rewrote to
"now + 45 days" on every check — a stored date that silently moved every day,
so the row could not tell you whether a session was minutes or months old. The
second replaced it with a `last_verified_at` cutoff, which read better but still
ended sessions purely because time had passed — signing out an offline operator
who had done nothing wrong and could not possibly have been checked.

`last_verified_at` is still recorded, because knowing when Keycloak last
confirmed an account is genuinely useful when reading logs. It just does not
decide anything.

Still no JWT: the token stays an opaque random string, so there is no signing
secret to manage and revocation is a row delete.
"""

import logging
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import or_

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


class SessionStore:
    """Database-backed session storage.

    Every method opens its own SessionLocal(): require_session runs as a
    dependency outside any request-scoped session, and the revalidation worker
    calls in from a worker thread, so there is no ambient session to borrow.
    """

    def create(self, username: str, **extra) -> str:
        from app.models.schema import Session as SessionRow

        token = secrets.token_urlsafe(32)
        now = datetime.utcnow()
        db = SessionLocal()
        try:
            db.add(
                SessionRow(
                    token=token,
                    username=username,
                    mode=extra.get("mode", ""),
                    created_at=now,
                    # An online login IS a confirmation from Keycloak; an
                    # offline one is not. Recorded either way as the last point
                    # of contact, for diagnostics only — nothing expires on it.
                    last_verified_at=now,
                    refresh_token=extra.get("refresh_token", ""),
                    needs_relogin=False,
                    first_name=extra.get("first_name", ""),
                    email=extra.get("email", ""),
                    roles=extra.get("roles", []) or [],
                    # Free from the JWT already on hand at login — no admin
                    # call needed just to capture it. What lets the daily
                    # worker later ask Keycloak's Admin API about this exact
                    # account (see fetch_account_status).
                    keycloak_user_id=extra.get("keycloak_user_id", ""),
                )
            )
            db.commit()
            logger.info(
                "[SESSION] Created for %s (mode=%s). Valid indefinitely — it ends "
                "only if Keycloak tells us the account is no longer good, never "
                "because time passed. Stored in the database, so it survives a restart.",
                username, extra.get("mode", ""),
            )
        except Exception as exc:
            db.rollback()
            logger.error("Could not persist session for %s: %s", username, exc)
            raise
        finally:
            db.close()
        return token

    def get(self, token: str) -> Optional[dict]:
        """Returns the session as a plain dict, or None if there is no such row.

        There is no expiry check, on purpose: an existing row is a valid
        session. needs_relogin deliberately does not fail here either — a
        flagged session must keep working until the operator is somewhere safe
        to be signed out, which the Home screen decides, not this function.

        A dict rather than the ORM row so callers keep working unchanged after
        the session is detached, and so require_session cannot accidentally
        hand an endpoint a live database object.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row:
                return None
            return {
                "token": row.token,
                "username": row.username,
                "mode": row.mode,
                "created_at": row.created_at,
                "last_verified_at": row.last_verified_at,
                "needs_relogin": bool(row.needs_relogin),
                "relogin_suggested": bool(row.relogin_suggested),
                "first_name": row.first_name or "",
                "email": row.email or "",
                "roles": row.roles or [],
                "keycloak_user_id": row.keycloak_user_id or "",
                "password_credential_created_at": row.password_credential_created_at,
            }
        except Exception as exc:
            logger.error("Session lookup failed: %s", exc)
            return None
        finally:
            db.close()

    def revoke(self, token: str):
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            db.query(SessionRow).filter(SessionRow.token == token).delete()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("Session revoke failed: %s", exc)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Used by the daily revalidation worker (services/session_worker.py).
    # ------------------------------------------------------------------

    def online_sessions_with_refresh_token(
        self, skip_if_verified_after: datetime = None
    ) -> List[dict]:
        """Every session that still holds a refresh token, of any mode.

        Having a refresh token — not the mode label — is what actually makes a
        session checkable silently, without the operator typing anything. This
        used to also require mode == "online", but that wrongly excluded a
        cached-hash (offline/pending) login that carried a still-good refresh
        token forward from an earlier online login on this same device (see
        auth.py's _cached_keycloak_identity and the Creds model's docstring) —
        such a session is just as checkable as a freshly-online one, and
        treating it as unverifiable gave a needless hard "sign in again" wall
        to an account that had been confirmed online just hours or days
        before.

        skip_if_verified_after leaves out sessions Keycloak has already
        confirmed since that moment. The caller passes the start of today, so
        a session is verified once a day no matter how many times, or from how
        many places, a check is triggered — the stored `last_verified_at` is
        the record of that, so no separate bookkeeping is needed and it
        survives a restart.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            query = db.query(SessionRow).filter(
                SessionRow.refresh_token.isnot(None),
                SessionRow.refresh_token != "",
            )
            if skip_if_verified_after is not None:
                query = query.filter(
                    or_(
                        SessionRow.last_verified_at.is_(None),
                        SessionRow.last_verified_at < skip_if_verified_after,
                    )
                )
            rows = query.all()
            return [
                {
                    "token": r.token,
                    "username": r.username,
                    "refresh_token": r.refresh_token,
                    "keycloak_user_id": r.keycloak_user_id or "",
                    "password_credential_created_at": r.password_credential_created_at,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error("Could not list sessions for revalidation: %s", exc)
            return []
        finally:
            db.close()

    def unverifiable_sessions(self) -> List[dict]:
        """Sessions with no refresh token at all — genuinely never checkable.

        This is now a smaller, more honest bucket than "not mode == online":
        a cached-hash login carries forward whatever refresh token this
        device last obtained for the account (see
        online_sessions_with_refresh_token), so those are handled there
        instead, exactly like a freshly-online session. What actually lands
        here is the fixed device credential (mode="offline-device", never
        touches Keycloak by design) and a genuinely first-ever login on a
        brand-new device with nothing cached yet. Neither has ever been
        confirmed against Keycloak even once, so the only way to confirm
        either is to ask the operator to sign in again — worth doing once
        connectivity is actually back.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            rows = (
                db.query(SessionRow)
                .filter(
                    or_(
                        SessionRow.refresh_token.is_(None),
                        SessionRow.refresh_token == "",
                    ),
                    SessionRow.needs_relogin.is_(False),
                )
                .all()
            )
            return [{"token": r.token, "username": r.username, "mode": r.mode} for r in rows]
        except Exception as exc:
            logger.error("Could not list unverifiable sessions: %s", exc)
            return []
        finally:
            db.close()

    def active_sessions(self) -> List[dict]:
        """Every stored session. Used at startup to report how many survived a
        restart, which is the visible proof that sessions are persisted."""
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            rows = db.query(SessionRow).all()
            return [{"username": r.username, "mode": r.mode} for r in rows]
        except Exception as exc:
            logger.error("Could not list active sessions: %s", exc)
            return []
        finally:
            db.close()

    def mark_verified(
        self,
        token: str,
        refresh_token: str = None,
        password_credential_created_at=None,
        password_baseline_checked: bool = False,
    ):
        """Record that Keycloak just confirmed this account is still good.

        Stores a fact, not a deadline: the session does not gain "more time"
        (it never had a deadline to extend), it gains a note saying when it was
        last known to be fine. Its practical effect is clearing needs_relogin,
        which matters when an account is disabled and then re-enabled.

        Keycloak issues a new refresh token on every use, so failing to store
        the new one would make tomorrow's check fail.

        password_credential_created_at is only ever passed when the caller
        actually asked the Admin API this time (password_baseline_checked
        True) — a bare mark_verified (e.g. the Admin API was unreachable this
        cycle) must never overwrite the stored baseline with None, or the very
        next comparison would wrongly look like "no change" regardless of
        what really happened.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row:
                return
            row.last_verified_at = datetime.utcnow()
            if refresh_token:
                row.refresh_token = refresh_token
            if password_baseline_checked:
                row.password_credential_created_at = password_credential_created_at
            # Clears both flags: the account is demonstrably fine now, and a
            # successful redemption means there is a live token again, so the
            # banner asking for one has nothing left to ask for.
            row.needs_relogin = False
            row.relogin_suggested = False
            db.commit()
            logger.info(
                "[SESSION] Verified for %s — Keycloak confirmed the account is "
                "still valid. Stays logged in.",
                row.username,
            )
        except Exception as exc:
            db.rollback()
            logger.error("Session extend failed: %s", exc)
        finally:
            db.close()

    def mark_offline(self, token: str):
        """Settle a still-being-verified session as genuinely offline.

        A fast-path login starts as `pending`, meaning "signed in from the
        saved password, not yet checked". When the background check finds
        Keycloak unreachable, that stops being a temporary state and becomes
        the answer: this device has no network. Recording it is what lets the
        Home screen tell the operator they are working offline, instead of
        guessing while the check is still in flight.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row or row.mode != "pending":
                return
            row.mode = "offline"
            db.commit()
            logger.info(
                "[SESSION] %s is working offline — Keycloak could not be reached to "
                "confirm the login. The session stands; nothing is lost.",
                row.username,
            )
        except Exception as exc:
            db.rollback()
            logger.error("Could not mark session offline: %s", exc)
        finally:
            db.close()

    def promote_to_online(self, token: str, claims: dict):
        """Turn a fast-path session into a fully verified online one.

        A login accepted from the cached password starts out unverified: there
        was no Keycloak round trip, so there is no refresh token and none of
        the identity Keycloak holds. When the background check then confirms
        the credentials really are still good, this fills all that in, so from
        here the session is indistinguishable from one created by a normal
        online login — including being picked up by the daily check and no
        longer looking like something that was never verified.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row:
                return
            row.mode = "online"
            row.refresh_token = claims.get("refresh_token", "")
            row.first_name = claims.get("first_name", "") or row.first_name
            row.email = claims.get("email", "") or row.email
            row.roles = claims.get("roles", []) or row.roles
            row.keycloak_user_id = claims.get("sub", "") or row.keycloak_user_id
            row.last_verified_at = datetime.utcnow()
            row.needs_relogin = False
            row.relogin_suggested = False
            db.commit()
            logger.info(
                "[SESSION] Promoted %s to a verified online session — Keycloak "
                "confirmed the credentials just used are still valid.",
                row.username,
            )
        except Exception as exc:
            db.rollback()
            logger.error("Could not promote session to online: %s", exc)
        finally:
            db.close()

    def flag_needs_relogin(self, token: str, reason: str = "Keycloak rejected the account"):
        """Mark a session as needing re-login WITHOUT invalidating it.

        The token keeps working on purpose. The operator may be part-way
        through a batch scan or a data-collection run, and cutting them off
        there would lose the work. The frontend prompts them on the Home
        screen instead — the one place they can only reach between tasks.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row:
                return
            row.needs_relogin = True
            db.commit()
            logger.warning(
                "[SESSION] Flagged %s for re-login — %s. The session still works "
                "on purpose: they are only asked to sign in again when they next "
                "reach the Home screen, so nothing in progress is lost.",
                row.username, reason,
            )
        except Exception as exc:
            db.rollback()
            logger.error("Could not flag session for re-login: %s", exc)
        finally:
            db.close()

    def set_password_baseline(self, token: str, password_credential_created_at):
        """Seed the password-credential timestamp right after a fresh online
        login, instead of leaving it for the daily worker's first pass to
        establish.

        Deliberately its own method rather than folded into mark_verified:
        this happens once, right after login, and is not itself a
        confirmation that a day has passed and the account is still fine — it
        only records what the password's timestamp was AT login, so the very
        first real daily check afterward has something to compare against
        instead of just planting a flag with nothing to compare it to. Touches
        nothing else on the row — not last_verified_at, not needs_relogin.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row:
                return
            row.password_credential_created_at = password_credential_created_at
            db.commit()
            logger.info(
                "[SESSION] Seeded the password baseline for %s at login time — "
                "the next daily check can now actually detect a change instead "
                "of just recording one for the first time.",
                row.username,
            )
        except Exception as exc:
            db.rollback()
            logger.error("Could not seed password baseline: %s", exc)
        finally:
            db.close()

    def suggest_relogin(self, token: str, reason: str = "the offline token reached its idle limit"):
        """Invite a re-login without demanding one.

        Set when the stored offline token has aged out but Keycloak has
        confirmed the account itself is still perfectly good — the operator's
        credentials are not in question, there is simply no live token left to
        confirm them silently with any more. Signing in again takes seconds
        and restores that, but nothing is wrong if they carry on for now, so
        the frontend shows a banner rather than the blocking dialog
        needs_relogin gets.

        Never set alongside needs_relogin: a session with a genuinely refused
        account has a real problem, and softening that to a dismissible
        banner would be wrong.
        """
        from app.models.schema import Session as SessionRow

        db = SessionLocal()
        try:
            row = db.query(SessionRow).filter(SessionRow.token == token).first()
            if not row or row.needs_relogin:
                return
            if row.relogin_suggested:
                return  # already invited; nothing to say a second time
            row.relogin_suggested = True
            db.commit()
            logger.info(
                "[SESSION] Suggesting re-login for %s — %s. The account is "
                "confirmed fine, so this is a banner on the Home screen only; "
                "they can keep working and sign in whenever it suits them.",
                row.username, reason,
            )
        except Exception as exc:
            db.rollback()
            logger.error("Could not suggest re-login: %s", exc)
        finally:
            db.close()


session_store = SessionStore()


def _extract_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # The WebSocket client cannot set headers, so a query parameter is accepted.
    return request.query_params.get("token")


def require_session(request: Request) -> dict:
    """Dependency: reject the request unless it carries a valid session token."""
    token = _extract_token(request)
    session = session_store.get(token) if token else None
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


CurrentUser = Depends(require_session)
