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
The fixed account every outbound delivery authenticates as — the scan POST and
the config fetch — under **both** auth providers, never the logged-in
operator's. See `4 - assurance_gateway.md` for why.

Under `AUTH_PROVIDER=keycloak` it must be a real **Keycloak** account.

**No fallback.** Blank means syncing fails loudly and logs what to set. It used
to fall back to `QUALIX_USERNAME`/`PASSWORD`, and that caused a real outage:
those held a Qualix-only account that Keycloak had never heard of, so every
sync failed with "Invalid user credentials" and nothing said why.

> **Not the same account as `EMERGENCY_LOGIN_*`.** One is an identity that
> posts data; the other is a door key. See below.

### `EMERGENCY_LOGIN_USERNAME` / `EMERGENCY_LOGIN_PASSWORD`
The tier-3 credentials that unlock the device when Keycloak is unreachable
**and** no cached password exists — a brand-new device, or one this operator
has never logged into online here. See flow 2 in `11 - user_flows.md`.

Checked entirely on-device (`api/auth.py`), against these values. No network
call, and it authenticates nothing outbound: it decides who gets *in*, never
what anything is sent *as*. It therefore does not need to exist in Keycloak,
or in Qualix.

Give it its own credentials rather than reusing a real operator's. These have
to be shared with whoever might need to recover a device in the field, and if
they are the same as `SYNC_SERVICE_*` then everyone holding the emergency key
also holds the account that posts every scan.

Resolved in three steps:

1. `EMERGENCY_LOGIN_USERNAME` / `EMERGENCY_LOGIN_PASSWORD` — what to set now
2. `QUALIX_USERNAME` / `QUALIX_PASSWORD` — **deprecated aliases**
3. legacy `config.INI`, `[CONFIG_SETTINGS] username` / `password`

Step 3 is why this is not simply an env var: on a real device the credentials
live in `config.INI`, not necessarily in `.env` at all. Steps 2 and 3 exist so
that deploying this change cannot silently take away a device's emergency
login — the one path whose entire purpose is to work when everything else has
failed.

`QUALIX_USERNAME`/`QUALIX_PASSWORD` **no longer exist as settings** and are read
nowhere else. While either of the two older sources is still supplying the
value, the backend says so at startup:

```
The tier-3 emergency login is still being read from legacy config.INI
(/home/nvidia/eye_compass/config.INI). Set EMERGENCY_LOGIN_USERNAME /
EMERGENCY_LOGIN_PASSWORD instead — they are the device's recovery key and
should not be the account that syncs (SYNC_SERVICE_USERNAME).
```

That warning is the signal that a device has been migrated and the old values
can be deleted.

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

## Per-device identity settings

These three are what let Qualix map a scan to a place and a person from the
payload itself, rather than inferring it from whichever account authenticated
the post. All three are per physical device. See the *Qualix is now told the
device, operator and warehouse explicitly* entry in
`../architecture_and_segregation/enhancements.md` for why this replaced the
idea of authenticating each sync as the operator.

### `DEVICE_ID`
**Not** sent to Qualix. It is the 2-character prefix (`A-Z`/`0-9`) on every
batch number, and the only thing keeping one device's batch numbers distinct
from another's — the remaining 10 digits are an epoch timestamp that two
devices can produce identically. Assign these from one central list; a
duplicate silently reintroduces the cross-device collisions the scheme exists
to prevent.

Batch creation returns HTTP 500 with a readable message until it is set to
something valid, and the New Batch form shows that message in place of the
batch id.

### `DEVICE_CODE`
Sent as `device_serial_no` on every scan datagram. Free-form — it must match
whatever serial Qualix has registered for this device, and Qualix rejects the
post with `Device does not exist` if it does not.

> **These two are not interchangeable, and were previously assigned the other
> way round.** Neither is derived from the other. `DEVICE_ID` is local and
> short; `DEVICE_CODE` is whatever Qualix expects.
>
> Note also that the datagram's `device_id` field is neither of them — it is
> the machine's own `/etc/machine-id` fingerprint.

### `WAREHOUSE_NAME`
Sent as `warehouse_name` on every scan datagram. Fixed per device.

There is no `OPERATOR_ID` setting — the third member of that trio is read from
Qualix at login rather than configured. See `11 - user_flows.md`.

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

# WHO SYNCS. Must be a real Keycloak account. No fallback.
SYNC_SERVICE_USERNAME=<a Keycloak account>
SYNC_SERVICE_PASSWORD=<its password>

# WHO CAN GET IN WHEN NOTHING ELSE WORKS. A door key, not an identity —
# deliberately NOT the same account as the two lines above.
EMERGENCY_LOGIN_USERNAME=<device recovery account>
EMERGENCY_LOGIN_PASSWORD=<its password>

# Per-device, and different on every device. DEVICE_ID must be unique across
# the fleet; DEVICE_CODE must match the serial Qualix has registered.
DEVICE_ID=<2 chars, unique per device>
DEVICE_CODE=<the device serial Qualix knows>
WAREHOUSE_NAME=<this device's warehouse>
```

Everything else has working defaults.
