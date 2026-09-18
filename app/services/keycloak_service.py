"""
Keycloak authentication.

Operator identity moves from Qualix's own OAuth endpoint to this
organization's Keycloak instance, which already fronts Qualix org-wide via
the Assurance gateway. Same userbase either way — anyone who can sign in to
Qualix can sign in here.

Two distinct uses, both plain username/password (ROPC / "Direct Access
Grants"), because the operator keeps typing into the existing login form
rather than being redirected to a Keycloak-hosted page:

  * login()          — the operator, at login time
  * service_token()  — a fixed account used for syncing, never the operator
                       (an operator who signed in offline has no Keycloak
                       token at all, so syncing can never depend on one)

Structured to mirror sync_service.py: plain `requests`, a module-level
singleton, and no new dependency. Every failure resolves to None so callers
can treat "Keycloak said no" and "Keycloak is unreachable" identically and
fall through to the offline login tiers — the same convention as
login_qualix's bool return.
"""

import base64
import json
import logging
from datetime import datetime
from typing import List, Optional

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


def _decode_claims(token: str) -> dict:
    """Read the claims out of a JWT payload without verifying its signature.

    Deliberately unverified, and deliberately no PyJWT/python-jose: the token
    arrives over TLS from a request this backend just made itself, not from an
    untrusted redirect, so the only party who could have forged a claim is
    Keycloak — which we already trust, having just asked it to authenticate
    the password. There is no attacker-controlled channel here for signature
    verification to protect.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return {
            "username": claims.get("preferred_username", ""),
            "first_name": claims.get("given_name", ""),
            "email": claims.get("email", ""),
            "roles": (claims.get("realm_access") or {}).get("roles", []),
            # Keycloak's own internal user id. Already inside every token, so
            # capturing it costs nothing — it's what lets the daily
            # revalidation worker later ask the Admin API about this exact
            # account without needing the operator's password again.
            "sub": claims.get("sub", ""),
        }
    except Exception as exc:
        logger.warning("Could not decode Keycloak token claims: %s", exc)
        return {}


class KeycloakService:
    def __init__(self):
        self._service_token = ""
        self._admin_token_cache = ""
        # Lets the revalidation worker tell "Keycloak rejected this account"
        # apart from "we could not reach Keycloak", which decide opposite
        # outcomes: end the session vs. leave it completely alone.
        self.last_refresh_was_explicit_rejection = False
        # Same distinction for fetch_qualix_profile(): "this Keycloak account
        # is not a Qualix operator" (block login) vs. "the gateway could not
        # be reached" (inconclusive, do not block).
        self.last_profile_check_was_explicit_rejection = False
        # And for login(): "Keycloak says these credentials are wrong now"
        # (the password was changed, or the account disabled — stop trusting
        # the copy cached on this device) vs. "we could not ask" (an offline
        # device, which proves nothing and must change nothing).
        self.last_login_was_explicit_rejection = False

    @property
    def token_uri(self) -> str:
        base = (settings.KEYCLOAK_URL or "").rstrip("/")
        return f"{base}/realms/{settings.KEYCLOAK_REALM}/protocol/openid-connect/token"

    @property
    def is_configured(self) -> bool:
        return bool(settings.KEYCLOAK_URL and settings.KEYCLOAK_REALM)

    def is_reachable(self) -> bool:
        """Can this device currently talk to Keycloak at all?

        Asked about sessions that cannot be checked silently — someone who
        signed in offline has no refresh token to redeem, so the only way to
        find out whether their account is still good is to ask them to sign in
        again. Doing that requires knowing connectivity is genuinely back;
        prompting a still-offline device would be pointless, since the login
        would just fall through to the offline tiers and change nothing.

        Hits the realm's public discovery document: no credentials, no side
        effects, and a short timeout so a dead network fails fast.
        """
        if not self.is_configured:
            return False
        base = (settings.KEYCLOAK_URL or "").rstrip("/")
        url = f"{base}/realms/{settings.KEYCLOAK_REALM}/.well-known/openid-configuration"
        try:
            return requests.get(url, timeout=10).status_code == 200
        except Exception:
            return False

    def _token_request(self, data: dict, what: str) -> Optional[dict]:
        """POST to the token endpoint. Returns the JSON body, or None."""
        if not self.is_configured:
            # No network call at all — an unconfigured device must fall
            # through to the offline tiers instantly, not stall on a timeout.
            return None
        if settings.KEYCLOAK_CLIENT_SECRET:
            data["client_secret"] = settings.KEYCLOAK_CLIENT_SECRET
        try:
            response = requests.post(self.token_uri, data=data, timeout=15)
        except Exception as exc:
            logger.warning("Keycloak %s failed, treating as offline: %s", what, exc)
            return None
        if response.status_code != 200:
            logger.warning(
                "Keycloak %s rejected: HTTP %s %s",
                what, response.status_code, response.text[:200],
            )
            return {"__rejected__": True}
        try:
            return response.json()
        except Exception as exc:
            logger.warning("Keycloak %s returned a malformed body: %s", what, exc)
            return None

    def login(self, username: str, password: str) -> Optional[dict]:
        """Authenticate an operator. Returns claims + refresh_token, or None.

        The requested scopes matter more than they look — see
        settings.KEYCLOAK_SCOPE. One buys a refresh token that outlives the
        realm's 1-day SSO cap; another carries the audience the Assurance
        gateway checks for. Both are the difference between a login that
        merely succeeds and one that is actually usable afterwards.
        """
        self.last_login_was_explicit_rejection = False
        data = {
            "grant_type": "password",
            "client_id": settings.KEYCLOAK_CLIENT_ID,
            "username": username,
            "password": password,
        }
        if settings.KEYCLOAK_SCOPE:
            data["scope"] = settings.KEYCLOAK_SCOPE

        body = self._token_request(data, "login")
        if body and body.get("__rejected__"):
            # Keycloak answered, and the answer was no.
            self.last_login_was_explicit_rejection = True
            return None
        if not body:
            return None

        access_token = body.get("access_token", "")
        if not access_token:
            return None

        claims = _decode_claims(access_token)
        claims["access_token"] = access_token
        claims["refresh_token"] = body.get("refresh_token", "")
        return claims

    def refresh(self, refresh_token: str) -> Optional[dict]:
        """Redeem a stored refresh token. Used only by the daily worker.

        Sets last_refresh_was_explicit_rejection so the caller can distinguish
        the two failure modes. Keycloak rotates the refresh token on every
        use, so the new one in the return value must be stored.
        """
        self.last_refresh_was_explicit_rejection = False
        if not refresh_token:
            return None

        body = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": settings.KEYCLOAK_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            "refresh",
        )

        if body is None:
            # Unreachable / malformed — NOT a statement about the account.
            return None
        if body.get("__rejected__"):
            # Keycloak actively said no: account disabled, deleted, or the
            # offline token went unused past its idle window.
            self.last_refresh_was_explicit_rejection = True
            return None

        access_token = body.get("access_token", "")
        if not access_token:
            return None

        claims = _decode_claims(access_token)
        claims["access_token"] = access_token
        claims["refresh_token"] = body.get("refresh_token", refresh_token)
        return claims

    # ------------------------------------------------------------------
    # Sync identity — a fixed account, deliberately not the operator's.
    # ------------------------------------------------------------------

    def service_token(self) -> Optional[str]:
        """Access token for syncing, logging in on first use and caching it.

        A plain user login rather than a client-credentials grant: the team
        opted to reuse the existing Qualix service account, which is the same
        account in Keycloak's userbase. What matters for syncing is only that
        it is independent of whoever is logged in, which either approach
        satisfies.
        """
        if self._service_token:
            return self._service_token
        if not settings.SYNC_SERVICE_USERNAME or not settings.SYNC_SERVICE_PASSWORD:
            logger.warning("No sync service credentials configured.")
            return None

        logger.info(
            "[SYNC] Getting a Keycloak token for the fixed sync account (%s) — "
            "this is never the logged-in operator.",
            settings.SYNC_SERVICE_USERNAME,
        )
        claims = self.login(
            settings.SYNC_SERVICE_USERNAME, settings.SYNC_SERVICE_PASSWORD
        )
        if not claims:
            logger.error("[SYNC] Keycloak login FAILED for the sync account — syncing cannot proceed.")
            return None

        self._service_token = claims.get("access_token", "")
        logger.info("[SYNC] Sync account token obtained.")
        return self._service_token or None

    def invalidate_service_token(self):
        """Drop the cached token after Assurance rejects it, so the next
        attempt fetches a fresh one instead of replaying a stale one."""
        self._service_token = ""

    # ------------------------------------------------------------------
    # Qualix membership check — Keycloak alone is not enough.
    # ------------------------------------------------------------------

    def fetch_qualix_profile(self, access_token: str) -> Optional[dict]:
        """Confirm this Keycloak account is an actual Qualix operator.

        Keycloak only proves the username/password are valid on the shared
        CentralIAM realm — it says nothing about whether the person is one of
        Qualix's own users. Anyone with any account on that realm (a customer
        portal login, an unrelated internal tool, etc.) can pass Keycloak's
        check but has no business logging into this device.

        This calls the exact endpoint Qualix's own web frontend calls right
        after a Keycloak login to look up "who is this, in Qualix terms" —
        confirmed by capturing that real browser traffic. Its behavior, also
        confirmed directly:

          * A real Qualix operator -> HTTP 200, with `user`, `permissions`,
            etc. That response IS the source of truth for their profile.
          * A Keycloak account with no matching Qualix user -> HTTP 500, body
            `{"message": "USERNR01", ...}`. An explicit, unambiguous rejection
            — not a transient error.

        Sets last_profile_check_was_explicit_rejection so the caller can tell
        "not a Qualix user" (block the login) apart from "could not reach the
        gateway to check" (inconclusive — a network hiccup here should not be
        able to lock out an otherwise-legitimate operator).
        """
        self.last_profile_check_was_explicit_rejection = False
        if not settings.ASSURANCE_API_URL:
            return None

        base = settings.ASSURANCE_API_URL.rstrip("/")
        uri = settings.ASSURANCE_KEYCLOAK_PROFILE_URI.lstrip("/")
        url = f"{base}/{uri}"

        try:
            response = requests.get(
                url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
            )
        except Exception as exc:
            logger.warning(
                "[AUTH] Qualix profile check could not reach the gateway (%s) — "
                "treating as inconclusive, not as a rejection.", exc,
            )
            return None

        if response.status_code == 200:
            try:
                return response.json()
            except Exception as exc:
                logger.warning("Qualix profile check returned a malformed body: %s", exc)
                return None

        body = response.text[:300]
        if response.status_code == 500 and "USERNR01" in body:
            self.last_profile_check_was_explicit_rejection = True
            logger.warning(
                "[AUTH] Qualix profile check REJECTED (USERNR01) — this Keycloak "
                "account has no matching Qualix user."
            )
            return None

        logger.warning(
            "[AUTH] Qualix profile check: unexpected HTTP %s from the gateway: %s — "
            "treating as inconclusive, not as a rejection.",
            response.status_code, body,
        )
        return None

    # ------------------------------------------------------------------
    # Account status, straight from the source — used only by the daily
    # revalidation worker. No password involved anywhere in this section.
    # ------------------------------------------------------------------

    def _admin_token(self) -> Optional[str]:
        """Client-credentials token for Keycloak's own Admin REST API.

        Distinct from every other token in this file: those are all ROPC
        (grant_type=password), issued FOR an operator. This one authenticates
        the client itself — no user, no password — which is what the Admin
        API requires to look up an arbitrary account's status.

        Requires KEYCLOAK_CLIENT_ID to have "Service Accounts" enabled, with
        the realm-management client's `view-users` role assigned to that
        service account. That is a Keycloak realm configuration change, not
        something this code can do or verify — if it is missing, Keycloak
        answers 400/403 and this simply returns None, same as "unreachable".
        """
        if self._admin_token_cache:
            return self._admin_token_cache
        if not settings.KEYCLOAK_CLIENT_SECRET:
            return None

        body = self._token_request(
            {"grant_type": "client_credentials", "client_id": settings.KEYCLOAK_CLIENT_ID},
            "admin token",
        )
        if not body or body.get("__rejected__"):
            logger.warning(
                "[REVALIDATE] Could not get an Admin API token — either "
                "%s has no Service Account, or it lacks the realm-management "
                "'view-users' role. Password-change detection is skipped "
                "until this is granted; account-disabled detection via the "
                "refresh token is unaffected.",
                settings.KEYCLOAK_CLIENT_ID,
            )
            return None

        self._admin_token_cache = body.get("access_token", "")
        return self._admin_token_cache or None

    def invalidate_admin_token(self):
        self._admin_token_cache = ""

    def fetch_account_status(self, keycloak_user_id: str) -> Optional[dict]:
        """Ask Keycloak directly: is this account enabled, and when was its
        password last set.

        This is the piece a refresh-token grant cannot provide at all —
        redeeming a refresh token only proves the session/account is still
        valid, never that the password itself is unchanged (confirmed by
        direct test: an offline refresh token survives a password change
        untouched). Reading the password credential's own `createdDate` from
        the Admin API is the direct, authoritative answer instead, and it
        never needs or sees the actual password.

        Returns {"enabled": bool, "password_created_at": datetime | None}, or
        None if this cannot be determined right now (unreachable, not
        configured, or not permitted) — callers must treat that exactly like
        every other "inconclusive" result here: change nothing, try again
        next time.
        """
        if not keycloak_user_id:
            return None
        token = self._admin_token()
        if not token:
            return None

        base = (settings.KEYCLOAK_URL or "").rstrip("/")
        admin_base = f"{base}/admin/realms/{settings.KEYCLOAK_REALM}/users/{keycloak_user_id}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            user_resp = requests.get(admin_base, headers=headers, timeout=15)
        except Exception as exc:
            logger.warning("[REVALIDATE] Admin API user lookup unreachable: %s", exc)
            return None

        if user_resp.status_code == 401:
            # The cached admin token expired or was revoked — drop it so the
            # next call gets a fresh one, and treat this attempt as
            # inconclusive rather than guessing.
            self.invalidate_admin_token()
            return None
        if user_resp.status_code == 404:
            # The account itself is gone. Same verdict as "disabled".
            return {"enabled": False, "password_created_at": None}
        if user_resp.status_code != 200:
            logger.warning(
                "[REVALIDATE] Admin API user lookup returned HTTP %s — "
                "treating as inconclusive.", user_resp.status_code,
            )
            return None

        try:
            enabled = bool(user_resp.json().get("enabled", True))
        except Exception as exc:
            logger.warning("Admin API user lookup returned a malformed body: %s", exc)
            return None

        password_created_at = None
        try:
            cred_resp = requests.get(
                f"{admin_base}/credentials", headers=headers, timeout=15
            )
            if cred_resp.status_code == 200:
                for cred in cred_resp.json():
                    if cred.get("type") == "password" and cred.get("createdDate"):
                        # Keycloak reports this as epoch milliseconds.
                        password_created_at = datetime.utcfromtimestamp(
                            cred["createdDate"] / 1000
                        )
                        break
            else:
                logger.warning(
                    "[REVALIDATE] Admin API credentials lookup returned HTTP %s for "
                    "%s — the account-enabled result above is still used, but the "
                    "password-changed check is skipped this time.",
                    cred_resp.status_code, keycloak_user_id,
                )
        except Exception as exc:
            logger.warning("[REVALIDATE] Admin API credentials lookup unreachable: %s", exc)

        return {"enabled": enabled, "password_created_at": password_created_at}


keycloak_service = KeycloakService()
