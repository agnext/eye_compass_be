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


def _cache_credentials(db: Session, username: str, password: str):
    """Port of write_creds (database.py:158-176) — one row, replaced each time."""
    try:
        db.query(Creds).delete()
        db.add(Creds(user=username, password=_hash(password)))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Could not cache credentials: %s", exc)


def _check_cached(db: Session, username: str, password: str) -> bool:
    """Port of check_creds (database.py:326-345)."""
    row = db.query(Creds).filter(Creds.user == username).first()
    if not row or not row.password:
        return False
    return hmac.compare_digest(row.password, _hash(password))


def _sync_config_in_background(username: str, password: str):
    """Runs after the response. Opens its own session — the request-scoped one
    is closed by the time a BackgroundTask executes."""
    db = SessionLocal()
    try:
        sync_service.sync_commodity_config(db, username=username, password=password)
    except Exception as exc:
        logger.error("Background config sync failed: %s", exc)
    finally:
        db.close()


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

    qualix_user = _qualix_username(username)

    # 1. Online first.
    online = False
    try:
        online = sync_service.login_qualix(qualix_user, password)
    except Exception as exc:
        logger.warning("Qualix login attempt failed (treating as offline): %s", exc)

    if online:
        _cache_credentials(db, qualix_user, password)
        background_tasks.add_task(_sync_config_in_background, qualix_user, password)
        token = session_store.create(
            qualix_user,
            mode="online",
            customer_id=sync_service.customer_id,
            first_name=sync_service.first_name,
            customer_name=sync_service.customer_name,
        )
        return {
            "status": "success",
            "mode": "online",
            "token": token,
            "username": qualix_user,
            "first_name": sync_service.first_name,
            "customer_name": sync_service.customer_name,
        }

    # 2. Offline fallback against the cached credentials.
    if _check_cached(db, qualix_user, password):
        logger.info("Offline login accepted for %s from cached credentials.", qualix_user)
        token = session_store.create(qualix_user, mode="offline")
        return {
            "status": "success",
            "mode": "offline",
            "token": token,
            "username": qualix_user,
            "message": "Signed in offline. Results will sync when connectivity returns.",
        }

    # 3. Last resort: the device credentials from .env / config.INI. This keeps
    #    a brand-new device usable before its first successful online login.
    if (
        settings.QUALIX_USERNAME
        and settings.QUALIX_PASSWORD
        and qualix_user == _qualix_username(settings.QUALIX_USERNAME)
        and hmac.compare_digest(password, settings.QUALIX_PASSWORD)
    ):
        logger.info("Offline login accepted for %s from device configuration.", qualix_user)
        token = session_store.create(qualix_user, mode="offline-device")
        return {
            "status": "success",
            "mode": "offline",
            "token": token,
            "username": qualix_user,
            "message": "Signed in offline using device credentials.",
        }

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/logout")
def logout(request: Request):
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        session_store.revoke(header[7:].strip())
    return {"status": "success"}


@router.get("/me")
def me(session: dict = Depends(require_session)):
    return {
        "username": session.get("username"),
        "mode": session.get("mode"),
        "first_name": session.get("first_name", ""),
        "customer_name": session.get("customer_name", ""),
    }
