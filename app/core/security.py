"""
Session tokens.

The legacy app was a single-process kiosk: the login screen physically gated
the page change, so nothing else needed protecting. This backend listens on a
network socket, so an opaque bearer token is issued at login and required by
the endpoints that touch hardware or data.

Tokens are held in memory. That matches the device's operating model — a
restart ends the shift's session — and avoids adding a JWT secret to manage.
"""

import logging
import secrets
import threading
import time
from typing import Dict, Optional

from fastapi import Depends, HTTPException, Request

logger = logging.getLogger(__name__)

# 45 days, matching the SSO session lifespan described in docs/1 - strategy.md.
TOKEN_TTL_SECONDS = 45 * 24 * 60 * 60


class SessionStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, dict] = {}

    def create(self, username: str, **extra) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "username": username,
                "created_at": time.time(),
                **extra,
            }
        return token

    def get(self, token: str) -> Optional[dict]:
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if time.time() - session["created_at"] > TOKEN_TTL_SECONDS:
                self._sessions.pop(token, None)
                return None
            return session

    def revoke(self, token: str):
        with self._lock:
            self._sessions.pop(token, None)


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
