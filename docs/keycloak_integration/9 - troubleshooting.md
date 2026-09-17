# Troubleshooting

**What this file is:** the errors this integration can produce, what each one
actually means, and how to fix it. Every message here was seen for real while
building this.

---

## `invalid_scope` — login fails completely

```
{"error":"invalid_scope","error_description":"Invalid scopes: offline_access qualix-application-permissions ..."}
```

**Means:** the app asked Keycloak for a scope that is not attached to the client.
Keycloak refuses the whole login rather than granting part of it.

**Fix:** make `KEYCLOAK_SCOPE` in `.env` match exactly what is attached to
`qualix-backend` in Keycloak (**Clients → qualix-backend → Client scopes**).
Either attach the missing scope there, or remove it from the setting.

**Watch out:** this happens the moment someone tidies up scopes in Keycloak. The
two must stay in step, and the failure is total — nobody can log in online.

---

## `Client is not within the token audience` — login works, everything else 401s

```
{"detail":"Token exchange failed: {\"error\":\"access_denied\",
 \"error_description\":\"Client is not within the token audience\"}"}
```

**Means:** the login succeeded and the token is genuine, but it is not *addressed
to* the Assurance gateway, so the gateway refuses it. This is not about the user
having insufficient permission.

**Fix:** the `audience-for-gateway` and `add-asu-be-audience` scopes must be
attached to the client **and** present in `KEYCLOAK_SCOPE`.

**How to confirm:** decode the access token and look at `aud`. It must contain
both `gateway-client` and `asu-be`. If it only shows `Inspect-backend` and
`account`, the audience scopes are not being applied.

---

## `Client not allowed for direct access grants`

```
{"error":"unauthorized_client","error_description":"Client not allowed for direct access grants"}
```

**Means:** the client in `KEYCLOAK_CLIENT_ID` does not permit username/password
to be sent directly — it only supports the browser redirect login.

**Fix:** either use a client that allows it (`qualix-backend` does), or enable
**Direct access grants** on the client (Clients → *client* → Settings →
Capability config).

This is why `qualix-frontend` cannot be used, even though it has the right
scopes.

---

## HTTP 404 from the gateway

**Means:** right gateway, wrong path. The gateway serves the Qualix endpoints
**without** the `portal/` prefix.

| Wrong | Right |
|---|---|
| `portal/api/icompass/v1/config` | `api/icompass/v1/config` |
| `portal/api/scan/v2/post-visio` | `api/scan/v2/post-visio` |

**Fix:** check `ASSURANCE_CONFIG_URI` and `ASSURANCE_ANALYSIS_POST_URI`.

---

## Everyone is logged out after a reboot

**Means:** sessions are not being stored in the database.

**Check:** does the `sessions` table exist? It is created automatically at
startup, so its absence means the backend failed to create tables — look for
database errors in the startup log.

This was the original behaviour before this work (sessions lived in memory), so
if it reappears, something has regressed to the old code.

---

## An operator was asked to sign in again, and it was not because of elapsed time

A session never lapses because time has passed. If an operator is asked to sign
in again, it is always one of the things below.

---

## "Cleared the saved password on this device" appeared in the log

```
[AUTH] Background check REJECTED <user> — Keycloak says these credentials are no longer valid (password changed, or account disabled). Flagging for re-login at the Home screen.
[SESSION] Flagged <user> for re-login — Keycloak rejected the credentials used to sign in. The session still works on purpose: they are only asked to sign in again when they next reach the Home screen, so nothing in progress is lost.
[AUTH] Cleared the saved password on this device — Keycloak rejected it, so it must not keep working offline. The next login has to go through Keycloak.
```

**Means:** the operator signed in using the password saved on this device, and
the background check that always follows such a login asked Keycloak about those
exact credentials and was told no. In practice that is one of two things: **their
password was changed** somewhere else, or **their account was disabled**. It is
working as designed, not a fault.

Two consequences, both intended:

- The session **keeps working** until they reach the Home screen, where they get
  the usual "Please sign in again" popup. Anything in progress finishes and
  saves.
- The saved password row on the device is **deleted**. That is what stops an
  out-of-date password from continuing to work offline, and it also forces the
  next login through Keycloak — with nothing left to match, the fast path simply
  does not apply. There is no flag or column for this; the absence of the row is
  the rule.

**What the operator should do:** sign in again with their **current** password
while the device has a network. That goes through Keycloak, and a fresh password
is saved on the device, restoring instant and offline logins.

**If they have no network right now:** the fixed device credential from the
settings file still works, so they are not locked out. Their own password will
not work offline again until they have logged in online once with the new one.

**If this fires for someone whose account is fine:** check the account really is
enabled in Keycloak, and that they are still a registered Qualix user — a
`Background check: <user> is no longer a registered Qualix user` line points at
the second case rather than the password.

**Worth distinguishing from:**

```
[AUTH] Background check could not reach Keycloak for <user> — leaving the session alone. It will be checked again when there is a network.
```

That one is a genuinely offline device. Nothing is flagged and nothing is
cleared, because being offline says nothing about the account. If you see
`Cleared the saved password` on a device you know had no connectivity, that
would be a real bug — an unreachable server being mistaken for a rejection.

---

## An operator was asked to sign in again just after the device got internet back

```
[REVALIDATE] <user> signed in offline (mode=offline) and Keycloak is reachable again — asking them to sign in once at the Home screen so the account can actually be checked.
```

**Means:** this is working as designed, not a fault. Someone who signed in while
the device was offline has no Keycloak refresh token, so the daily check has
nothing to ask with — that session can never be confirmed silently, no matter
how long it stays open. As soon as Keycloak is reachable again, the only way to
check that account is to have the operator type their password once.

**What they see:** a popup on the **Home screen** saying "Please sign in again",
with a single **Sign in again** button. Nothing in progress is interrupted — the
flag is only ever acted on at Home.

**Confirm from the summary line:**

```
[REVALIDATE] Done: N checked -> N verified, N flagged, N unreachable; N offline session(s) asked to sign in again.
```

**Not a fault if:** the device had genuinely been offline and has just
reconnected.

**Worth a look if:** it happens repeatedly to someone who logs in online — that
would mean their sessions are staying at `mode=offline` instead of becoming
`online`. Check the `[AUTH] TIER` lines, and for a fast login check that the
background result was `CONFIRMED` and that a `[SESSION] Promoted` line followed
it. A fast login whose background check never reports anything at all means the
background task is not running; one that reports `could not reach Keycloak` on a
device you believe is online is a connectivity or configuration problem.

While the device is still offline, nothing happens to these sessions at all:

```
[REVALIDATE] N offline session(s) still cannot be confirmed — Keycloak is unreachable. Leaving them alone; the operator keeps working.
```

---

## The revalidation check has stopped running

There is no time limit that fires when the check breaks, so a broken worker is
**silent** — sessions simply keep working, unchecked. That makes it worth
looking for deliberately.

**Check:** look for a pass being triggered, which happens roughly every 6 hours:

```
[REVALIDATE] Pass triggered by the scheduled timer.
```

If that line never appears, the worker is not running — confirm
`SESSION_REVALIDATION_ENABLED` is `true` and `AUTH_PROVIDER` is `keycloak`.

**Do not expect `[REVALIDATE] Check starting` on every pass.** Each session is
verified at most once per day, so most passes correctly find nothing to do and
log this instead:

```
[REVALIDATE] Nothing due — every online session has already been confirmed with Keycloak today (or there are none).
```

What should appear **once a day** is a real check:

```
[REVALIDATE] Check starting — 1 online session(s) due for verification with Keycloak.
```

If a whole day passes with only "Nothing due" and no session was ever confirmed,
something is wrong. If checks do start but every one reports `unreachable`, the
device has had no working route to Keycloak; that is a network problem, and the
operator is correctly left alone meanwhile.

You may also see a pass started by the sync worker after a successful upload
cycle — this is normal and usually finds nothing due:

```
[REVALIDATE] Pass triggered by a successful sync (so the network is up).
```

Passes never overlap. If one is already running when another is triggered, the
second is skipped rather than queued, which is also harmless:

```
[REVALIDATE] Skipping the pass triggered by a successful sync (so the network is up) — one is already running.
```

---

## Sessions die after about a day

Operators are being asked to log in again roughly every day, even though nothing
in the session settings says so.

**Means:** the login is not using the long-lived `offline_access` token, so the
realm's 1-day limit (SSO Session Max) is applying. The daily check then cannot
renew anything, because the token it was given is already dead.

**Check:** decode the `refresh_token` stored in the `sessions` table. It must say
`typ: Offline`. If it says `Refresh`, the offline scope is not being granted.

**Fix:** ensure `offline_access` is in `KEYCLOAK_SCOPE` and attached to the
client.

This is a nasty one because it looks fine in testing and only shows up a day
later.

---

## An operator cannot log in offline

**Most likely not a fault.** Offline login only works for the operator who most
recently logged in **online on that specific device** — the saved-password table
holds a single row, and every online login replaces it.

If a different operator logged in since, the previous one can no longer log in
offline until they log in online again.

**The other possibility:** the saved row was deleted because Keycloak explicitly
rejected it — see "Cleared the saved password on this device" above. Look for
that line; if it is there, the fix is a fresh online login with the current
password, and the fixed device credential covers them meanwhile.

---

## An operator gets "Invalid credentials" even though their password is right

**Means:** they authenticated fine against Keycloak, but the follow-up Qualix
membership check rejected them.

Look for:

```
[AUTH] Qualix profile check REJECTED (USERNR01) — this Keycloak account has no matching Qualix user.
[AUTH] TIER 1 REJECTED for <user> — authenticated fine by Keycloak, but is not a registered Qualix user. Login refused.
```

**Fix:** this person has a valid account on the shared `CentralIAM` Keycloak
realm, but no matching Qualix operator account — check with whoever manages
Qualix user accounts whether one should be created for them. This is not a
device or configuration fault.

**If this fires for someone who SHOULD be a valid operator:** check
`ASSURANCE_KEYCLOAK_PROFILE_URI` is correct, and confirm the same account works
when logged into `assaying-dev.qualix.ai` directly in a browser — if it fails
there too, the problem is in Qualix's own user records, not this device.

---

## Someone was signed out unexpectedly with "please sign in again"

**Means:** Keycloak said the account is no longer good. Either the daily check
found it disabled or deleted:

```
Keycloak rejected <user> — flagging for re-login at the next safe point.
```

or the background check that follows a fast login was rejected outright (see
"Cleared the saved password on this device" above), which is the quicker of the
two — it lands within seconds of the login rather than within a day.

A rejection from Keycloak is the **only** thing that ever forces a fresh login.
It includes the case
where the device was offline for more than 30 consecutive days: the Keycloak
offline refresh token passed its idle window and is rejected the next time the
device successfully reaches Keycloak.

**This is working as designed.** Note it only takes effect when they reach the
Home screen — as a popup with a single **Sign in again** button — so any scan in
progress completed safely. Signing in works offline as well, so the popup is
never a dead end.

---

## Syncing fails but login works

Login and syncing use **different** accounts — the operator logs in as
themselves, but syncing always uses the fixed `SYNC_SERVICE_USERNAME`. A working
login therefore says nothing about syncing.

**Check:** `ASSURANCE_API_URL` is set (required in Keycloak mode), and the sync
account's credentials are valid.

---

## How to inspect a token

The single most useful debugging step. Paste the token at
[jwt.io](https://jwt.io), or decode it locally, and check:

| Field | Should be |
|---|---|
| `azp` | The client that issued it, e.g. `qualix-backend` |
| `aud` | Must contain `gateway-client` and `asu-be` |
| `scope` | The granted scopes |
| `permissions` | `scan_visio`, `scan_history`, etc. |
| `typ` (on the **refresh** token) | `Offline` |

**Important:** the *access* token's `scope` field does **not** list everything.
Scopes with "Include in token scope" switched off — including both audience
scopes — do their work invisibly. **Decode the refresh token to see the full
list.** This is exactly what made the audience problem so hard to diagnose.
