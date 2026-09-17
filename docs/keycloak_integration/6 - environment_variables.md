# Settings reference

**What this file is:** every setting added by this work, what it does, and what
happens if it is wrong. All live in `eye_compass_be/.env`.

---

## The main switch

### `AUTH_PROVIDER`
Which system checks operator passwords.

| Value | Meaning |
|---|---|
| `legacy` | Qualix checks it — exactly as before this work. **This is the default.** |
| `keycloak` | Keycloak checks it, and syncing goes through the Assurance gateway |

Read once at startup, so **the backend must be restarted** after changing it.

---

## Keycloak settings

### `KEYCLOAK_URL`
`https://dev.perfeqtfoods.com/keycloak` — the Keycloak server.

### `KEYCLOAK_REALM`
`CentralIAM` — which set of users to check against.

### `KEYCLOAK_CLIENT_ID`
`qualix-backend` — the "application identity" used when asking Keycloak to check
a password. This is not a user; it identifies *which application* is asking.

### `KEYCLOAK_CLIENT_SECRET`
The password for that application identity. Obtained from Keycloak
(**Clients → qualix-backend → Credentials**).

### `KEYCLOAK_SCOPE`
The extra pieces of information requested in the login token:

```
offline_access qualix-application-permissions audience-for-gateway add-asu-be-audience
```

| Piece | Why it matters |
|---|---|
| `offline_access` | Long-lived login. Without it, sessions die after 1 day |
| `qualix-application-permissions` | The list of allowed actions, which Qualix checks |
| `audience-for-gateway` | Lets the Assurance gateway accept the token |
| `add-asu-be-audience` | Same — the gateway checks for both |

⚠️ **This list must match Keycloak exactly.** These are all attached as
"Optional" scopes, which means they must be asked for by name — but asking for
one that is *not* attached makes the entire login fail with `invalid_scope`. If
someone removes a scope in Keycloak, remove it here too, and vice versa.

---

## Sync settings

### `SYNC_SERVICE_USERNAME` / `SYNC_SERVICE_PASSWORD`
The fixed account used to send scan results — never the logged-in operator's.
If left blank, falls back to `QUALIX_USERNAME` / `QUALIX_PASSWORD`, which is the
normal setup. See `4 - assurance_gateway.md` for why this is separate.

### `ASSURANCE_API_URL`
`https://dev.perfeqtfoods.com/api/asu/gateway/assaying-dev/`

The gateway that scan results go through. **Required** when `AUTH_PROVIDER` is
`keycloak` — without it, syncing fails.

⚠️ This currently points at the **dev** Qualix environment. Keep it in step with
`QUALIX_API_URL`, or flipping `AUTH_PROVIDER` will silently change where scan
data is stored.

### `ASSURANCE_CONFIG_URI` / `ASSURANCE_ANALYSIS_POST_URI`
Defaults: `api/icompass/v1/config` and `api/scan/v2/post-visio`.

The gateway serves these **without** the `portal/` prefix that direct Qualix
uses. Normally never need changing; they exist as separate settings so the
gateway paths and the direct-Qualix paths can be corrected independently.

### `ASSURANCE_KEYCLOAK_PROFILE_URI`
Default `api/user/keycloak-profile`. Called once at every Keycloak login, with
the operator's own token, to confirm they are an actual Qualix operator and not
just anyone with a valid account on the shared Keycloak realm. See
`2 - how_login_works.md` and `4 - assurance_gateway.md`.

---

## Session settings

> **There is deliberately no session-lifetime setting.** A login lasts until
> Keycloak says the account is no longer good. Time alone never ends one, so a
> device that is offline for months keeps working. See
> `7 - sessions_and_expiry.md`.

### `SESSION_REVALIDATION_ENABLED`
Default `true`. Whether to periodically check that logged-in accounts are still valid.
Only runs when `AUTH_PROVIDER=keycloak` — there is nothing to check against
otherwise.

Turning this off means a disabled or revoked account is **never noticed**:
sessions stay valid indefinitely, because nothing else ever ends one. It exists
as an escape hatch, not as something to switch off routinely.

### `SESSION_REVALIDATION_INTERVAL_HOURS`
Default `6`. How often the worker **wakes up and looks** — not how often
Keycloak is contacted.

Each session is verified with Keycloak **at most once per day**. That limit is
enforced by the session's own `last_verified_at` column: anything Keycloak
already confirmed since midnight UTC today is not selected as due, so the worker
skips it without making any call at all. Because the day boundary lives in the
database rather than in memory, it also survives a backend restart.

Waking every 6 hours therefore costs essentially nothing (about four wake-ups a
day, normally one real Keycloak call per session per day). The extra wake-ups
exist for the device that was offline or unreachable at the first attempt of the
day — it gets more chances before tomorrow instead of losing the whole day.

A successful sync also triggers a pass, for the same reason and at the same
cost — see `7 - sessions_and_expiry.md`.

> **Note:** how long Keycloak's *own* tokens last is **not** controlled here.
> Those are Keycloak server settings (Realm settings → Sessions), changeable only
> by a Keycloak administrator.

---

## Frontend setting

### `LOGIN_UI_MODE`
Default `single_form` — the current login screen.

Reserved for future use. No code depends on it yet; it exists so a future change
to a Keycloak-hosted login page can be switched on without restructuring
anything. See `10 - open_items.md`.

---

## Minimum needed to switch a device

```bash
AUTH_PROVIDER=keycloak
KEYCLOAK_URL=https://dev.perfeqtfoods.com/keycloak
KEYCLOAK_REALM=CentralIAM
KEYCLOAK_CLIENT_ID=qualix-backend
KEYCLOAK_CLIENT_SECRET=<from Keycloak>
KEYCLOAK_SCOPE=offline_access qualix-application-permissions audience-for-gateway add-asu-be-audience
ASSURANCE_API_URL=https://dev.perfeqtfoods.com/api/asu/gateway/assaying-dev/
```

Everything else has working defaults.
