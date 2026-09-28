"""An offline login getting back online must invite a re-login, not force one.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_offline_reconnect_relogin.py

Reported directly: an operator signed in offline with a correct password, the
network came back during the session, and they were hit with the hard
"you must sign in again" dialog anyway — with nothing actually wrong with
their account. `_flag_offline_sessions_if_back_online` was calling
`flag_needs_relogin` (the block) where it should have called `suggest_relogin`
(the dismissible banner) — nothing about being offline is evidence the account
is bad, so reconnecting must never look like a rejection.
"""
import sys
sys.path.insert(0, '.')

from app.core.config import settings
settings.AUTH_PROVIDER = "keycloak"

import app.services.session_worker as session_worker  # noqa: E402


class FakeSessionStore:
    def __init__(self, pending):
        self._pending = pending
        self.suggested = []
        self.force_flagged = []

    def unverifiable_sessions(self):
        return self._pending

    def suggest_relogin(self, token, reason=""):
        self.suggested.append((token, reason))

    def flag_needs_relogin(self, token, reason=""):
        # If this is ever called for an offline reconnect, that IS the bug —
        # it is the hard, blocking dialog, and nothing here justifies it.
        self.force_flagged.append((token, reason))


class FakeKeycloak:
    def __init__(self, reachable):
        self._reachable = reachable

    def is_reachable(self):
        return self._reachable


# 1. The exact scenario: one session that logged in offline, no refresh token,
#    and Keycloak is reachable again. Must be a soft invitation.
pending = [{"token": "tok-1", "username": "op1", "mode": "offline"}]
session_worker.session_store = FakeSessionStore(pending)
session_worker.keycloak_service = FakeKeycloak(reachable=True)

summary = {"offline_flagged": 0}
session_worker._flag_offline_sessions_if_back_online(summary)

store = session_worker.session_store
assert store.force_flagged == [], (
    "flag_needs_relogin (the BLOCKING dialog) was called for a session with "
    "nothing wrong — this is the exact bug reported: %s" % store.force_flagged)
assert len(store.suggested) == 1, store.suggested
assert store.suggested[0][0] == "tok-1", store.suggested
assert summary["offline_flagged"] == 1, summary
print("1. reconnecting after an offline login -> suggest_relogin (soft banner), "
      "NOT flag_needs_relogin (hard block)")

# 2. Still offline (Keycloak unreachable): nothing happens at all — an
#    offline device must not be nudged about something it cannot yet confirm.
session_worker.session_store = FakeSessionStore(pending)
session_worker.keycloak_service = FakeKeycloak(reachable=False)
summary2 = {"offline_flagged": 0}
session_worker._flag_offline_sessions_if_back_online(summary2)
store2 = session_worker.session_store
assert store2.suggested == [] and store2.force_flagged == [], (store2.suggested, store2.force_flagged)
assert summary2["offline_flagged"] == 0, summary2
print("2. still offline -> left alone, no popup of either kind")

# 3. Nothing pending: the reachability probe must not even run (a fleet of
#    normally-online devices should never pay for this check).
calls = []
class CountingKeycloak(FakeKeycloak):
    def is_reachable(self):
        calls.append(1)
        return True
session_worker.session_store = FakeSessionStore([])
session_worker.keycloak_service = CountingKeycloak(reachable=True)
summary3 = {"offline_flagged": 0}
session_worker._flag_offline_sessions_if_back_online(summary3)
assert calls == [], "reachability was probed with nothing pending to check"
print("3. no unverifiable sessions -> no network call made at all")

print("ALL OFFLINE-RECONNECT RELOGIN CHECKS PASSED")
