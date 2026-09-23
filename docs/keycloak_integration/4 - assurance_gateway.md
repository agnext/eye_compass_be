# 4 — The Assurance gateway (how scan results reach Qualix)

**What this file is:** how scan results and commodity settings travel to Qualix
now, and why syncing deliberately does **not** use the operator's own login.

---

## Before and after

**Before:** the app talked to Qualix directly, using the token from the
operator's own login.

**After (Keycloak mode):** the app talks to **Assurance**, a gateway that sits in
front of Qualix. Assurance accepts a Keycloak token, adds whatever extra
information Qualix needs, and passes the request on.

```
Before:  Eye Compass  ──────────────────────────────►  Qualix
After:   Eye Compass  ────►  Assurance gateway  ────►  Qualix
```

## Addresses

| | Address |
|---|---|
| Gateway base | `https://dev.perfeqtfoods.com/api/asu/gateway/assaying-dev/` |
| Commodity settings | `api/icompass/v1/config` |
| Posting scan results | `api/scan/v2/post-visio` |
| Qualix membership lookup (login-time only) | `api/user/keycloak-profile` |

**Note the paths are different from the direct-Qualix ones.** Direct Qualix uses
`portal/api/icompass/v1/config`; through the gateway the `portal/` prefix must be
dropped, or the request returns 404. Both sets of paths are kept in the settings
file so either can be corrected independently.

> **The gateway address above points at the DEV Qualix environment**
> (`assaying-dev`). Switching a device to Keycloak therefore also changes *where
> scan data is stored*. Make sure this is intended before switching a device that
> handles real production scans.

---

## Syncing uses a fixed account, not the operator

This is the most important design decision in this file.

Syncing **always** authenticates as one fixed account (currently
`cgi.op3@agnext.in`, from the settings file), regardless of who is logged in.
This applies to all three places that send data:

- the upload right after a batch finishes,
- the retry worker (see `6 - environment_variables.md` / `SYNC_RETRY_INTERVAL_MINUTES`),
- the manual re-sync.

### Why not use the operator's own login?

Three reasons, the last one decisive:

1. **Identity travels in the payload instead, so authentication does not have
   to carry it.** The datagram names the operator outright, in an `operator_id`
   field (alongside `device_serial_no` and `warehouse_name`) — so Qualix does
   not have to infer who or where from the account that posted. Nothing is lost
   by authenticating as someone else. *This was not always true: the payload
   originally had no "submitted by" field at all, only a manually typed
   `surveyor_name`, and the trio was added specifically so that the decision
   below would not cost Qualix the mapping it needs.*
2. **The retry worker runs unattended** — overnight, or right after a reboot,
   when nobody is logged in at all. It cannot depend on somebody's session
   existing.
3. **An operator who logged in offline has no Keycloak token at all.** Tiers 2
   and 3 never contact Keycloak, so there is nothing to reuse — not even for the
   upload immediately after a scan. An operator-based approach would therefore
   only work *sometimes*, which is worse than not having it.

The result is simpler than what came before: syncing no longer depends on anyone
being logged in, and behaves identically whether the operator signed in online,
offline, or with device credentials.

### Where `operator_id` comes from

It is Qualix's own `user.user_id`, taken from the `/user/keycloak-profile`
response at login (the same call described in the next section) and stored on
both the `sessions` and `creds` rows.

It is refreshed **only on a fresh online login**. An offline or cached login
carries the stored value forward unchanged rather than overwriting it with a
blank — which is what makes the field reliable on exactly the devices that need
it most, the ones that go weeks without a network. A fresh value that differs
from the stored one is logged at WARNING before being saved, so an operator
being reassigned is visible rather than silent.

`current_operator_id()` in `core/security.py` reads it for the scan endpoints,
and returns `""` rather than raising when there is no session — `scan.py`
enforces no authentication today, and reading this must not become a new way
for a scan to be rejected.

---

## The one call that uses the operator's own token, not the sync account

Everything above (config, scan posting) deliberately avoids the operator's
token. There is exactly one exception: `api/user/keycloak-profile`, called once
at login time with the operator's own brand-new Keycloak token.

This is not for syncing — it exists to answer a different question: is this
Keycloak account actually a Qualix operator at all? Keycloak's realm is shared
with other systems, so a valid Keycloak login proves nothing about Qualix
membership by itself. This call is what Qualix's own web frontend uses for
exactly the same purpose, right after its own Keycloak login, so calling it
here checks membership the same way Qualix itself does.

- **A real Qualix operator** — HTTP 200, with their profile (`user`,
  `permissions`, etc.). Their real name and customer are read from here for
  display, since Keycloak's own claims were empty for the accounts tested.
- **Not a Qualix user** — HTTP 500, body `{"message": "USERNR01", ...}`. The
  login is refused.
- **Gateway unreachable** — treated as inconclusive, not a rejection. The login
  proceeds on Keycloak's answer alone, so a gateway hiccup can never lock out a
  real operator.

See `2 - how_login_works.md` for the full login sequence this fits into.

---

## If the sync token expires

Tokens are short-lived. If the gateway rejects one as expired (HTTP 401), the
backend automatically fetches a fresh token and retries **once** before treating
it as a real failure. Without this, records would sit unsent until the next
retry cycle for no good reason.

## What did not change

The three-way result code that the rest of the app depends on is untouched:

| Code | Meaning |
|---|---|
| `1` | Qualix accepted it |
| `2` | Qualix rejected it — permanent, never retried |
| `0` | Not delivered — the retry worker will try again |

The retry worker and the History screen both rely on this, so it was deliberately
left exactly as it was.
