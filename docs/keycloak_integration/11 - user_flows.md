# 11 — Every user flow, and how to confirm it from the logs

**What this file is:** every situation an operator can be in, what the system
does, and **exactly which log lines prove it worked**. Use this to verify a
device after switching it over, or to work out what happened after a complaint.

Follow the logs with:

```bash
journalctl -u eye-compass-backend.service -f | grep -E "AUTH|SESSION|REVALIDATE|SYNC"
```

---

## 1. First login on a new device, internet working

**What happens:** nothing is saved on the device for this operator yet, so the
fast path does not apply. Keycloak checks the password and the operator is
logged in. Their password is hashed and saved on the device so later logins are
instant and work without internet. The commodity list refreshes in the
background. The session lasts **indefinitely** — nothing about elapsed time can
end it.

**Logs:**
```
[AUTH] Login attempt for <user>. Provider=KEYCLOAK. Tier 1: asking Keycloak (CentralIAM).
[AUTH] TIER 1 SUCCESS — online login for <user> via KEYCLOAK (realm=CentralIAM, client=qualix-backend, roles=[...], qualix_customer=<customer>). Session mode=online, offline password cached, config refresh queued.
[SESSION] Created for <user> (mode=online). Valid indefinitely — it ends only if Keycloak tells us the account is no longer good, never because time passed. Stored in the database, so it survives a restart.
```

**The proof:** the word `KEYCLOAK` and a populated `roles=[...]` list. A legacy
login says `QUALIX` and has no roles.

---

## 1b. A valid Keycloak login for someone who is not a Qualix operator

**What happens:** the password checks out against Keycloak — this is a real
account on the shared realm — but the follow-up check against Qualix
(`api/user/keycloak-profile`) comes back `USERNR01`: no matching Qualix user.
The login is refused with the same message as a wrong password. Nothing is
cached and no session is created.

**Logs:**
```
[AUTH] Login attempt for <user>. Provider=KEYCLOAK. Tier 1: asking Keycloak (CentralIAM).
[AUTH] Qualix profile check REJECTED (USERNR01) — this Keycloak account has no matching Qualix user.
[AUTH] TIER 1 REJECTED for <user> — authenticated fine by Keycloak, but is not a registered Qualix user. Login refused.
```

**The proof:** the `USERNR01` line. No `[SESSION] Created` line follows it —
this is the difference between this and every other rejection, which fall
through to the offline tiers instead of refusing outright.

---

## 2. New device, no internet, operator has never used it

**What happens:** nothing is saved for this person, so there is nothing to check.
Only the fixed device credential works. This is the one case that genuinely
requires internet at least once.

**Logs (device credential accepted):**
```
[AUTH] Login attempt for <user>. Provider=KEYCLOAK. Tier 1: asking Keycloak (CentralIAM).
[AUTH] Tier 2: checking <user> against the saved password on this device.
[AUTH] TIER 3 SUCCESS — offline login for <user> from the fixed device credentials. Session mode=offline-device.
```

**Logs (refused):**
```
[AUTH] ALL TIERS FAILED for <user> — no provider accepted it, no saved password matched, and it is not the device credential. Login refused.
```

---

## 3. Normal day, internet working

**What happens:** the operator has logged in on this device before, so the typed
password matches the one saved here and they are signed in **immediately** —
around 0.015 s, with no network call. A background check then puts those same
credentials to Keycloak, is told they are fine, and promotes the session in
place to a full online one: refresh token stored, roles filled in,
`mode=online`. The config sync runs as usual. The operator sees nothing but a
fast login.

**Logs:**
```
[AUTH] FAST LOGIN for <user> — matched the password saved on this device. Signed in now; confirming with Keycloak in the background.
[SESSION] Created for <user> (mode=offline). Valid indefinitely — it ends only if Keycloak tells us the account is no longer good, never because time passed. Stored in the database, so it survives a restart.
[AUTH] Background check CONFIRMED <user> with Keycloak — the fast login was legitimate.
[SESSION] Promoted <user> to a verified online session — Keycloak confirmed the credentials just used are still valid.
```

**The proof:** `FAST LOGIN` followed within a second or two by `CONFIRMED` and
`Promoted`. After the promotion the session is indistinguishable from a flow 1
login and is picked up by the once-a-day check like any other.

**Why the session is first created as `mode=pending`:** at the instant of the
fast login nothing has been verified, no refresh token exists, and it is not yet
known whether this device has a network — so neither `online` nor `offline`
would be true. The background check settles it: `online` on confirmation,
`offline` if Keycloak could not be reached. The HTTP response says
`"mode": "cached"`, which is simply "you are in, from the saved password".

That is also why the Home screen's *"Signed in offline — results will sync
later"* notice appears only once the session has settled to `offline`, never
while it is still `pending` — otherwise an operator who is perfectly online
would be told their results are not syncing.

---

## 3b. Fast login, but Keycloak rejects the credentials

**What happens:** the operator's password was changed elsewhere, or their
account was disabled, since the last time they logged in here. The password
saved on this device still matches what they typed, so they are signed in
instantly. The background check then asks Keycloak with those exact credentials
and is told no.

Two things follow. The session is **flagged but keeps working** — an operator
mid-batch is never interrupted, and is prompted only at the Home screen. And the
saved password on this device is **deleted**, so it can never be used offline
again; with nothing left to match, the next login is forced through Keycloak.

This is the most important new behaviour to be able to confirm from the logs.

**Logs:**
```
[AUTH] FAST LOGIN for <user> — matched the password saved on this device. Signed in now; confirming with Keycloak in the background.
[AUTH] Background check REJECTED <user> — Keycloak says these credentials are no longer valid (password changed, or account disabled). Flagging for re-login at the Home screen.
[SESSION] Flagged <user> for re-login — Keycloak rejected the credentials used to sign in. The session still works on purpose: they are only asked to sign in again when they next reach the Home screen, so nothing in progress is lost.
[AUTH] Cleared the saved password on this device — Keycloak rejected it, so it must not keep working offline. The next login has to go through Keycloak.
```

If Keycloak accepts the credentials but the account is no longer a registered
Qualix user, the same flag-and-clear pair follows this line instead:

```
[AUTH] Background check: <user> is no longer a registered Qualix user. Flagging for re-login.
```

**Then, when they reach Home:** the same "Please sign in again" popup as flow 9.

**The proof:** `REJECTED` (or the `no longer a registered Qualix user` line)
paired with `Cleared the saved password`. Both must appear — the flag alone
would leave a stale password usable offline.

**Not to be confused with:** `Background check could not reach Keycloak`, which
is flow 4 and clears nothing.

**The operator is not stranded.** The fixed device credential (Tier 3) still
works, and a successful online login saves a fresh password again.

---

## 4. No internet, operator has logged in on this device before

**What happens:** the saved password matches and they are signed in
**immediately** — there is no timeout to sit through, because Keycloak is never
contacted on this path. The background check then tries Keycloak, cannot reach
it, and **changes nothing**: being offline proves nothing about the account, so
the session is left alone and the saved password is kept.

**Logs:**
```
[AUTH] FAST LOGIN for <user> — matched the password saved on this device. Signed in now; confirming with Keycloak in the background.
[SESSION] Created for <user> (mode=offline). Valid indefinitely — it ends only if Keycloak tells us the account is no longer good, never because time passed. Stored in the database, so it survives a restart.
[AUTH] Background check could not reach Keycloak for <user> — leaving the session alone. It will be checked again when there is a network.
```

**The proof:** `FAST LOGIN` followed by `could not reach Keycloak`, and **no**
`Promoted` line. The session stays `mode=offline`.

**One thing to know about `mode=offline`:** this session has no Keycloak refresh
token, so the daily check can never confirm it silently. The moment the device
can reach Keycloak again, the operator is asked to sign in once — see flow 9b.

**When you see `TIER 2 SUCCESS` instead:** the fast path did not match, so the
login went the long way round and the cached hash caught it at Tier 2 — for
example a second operator on a device whose saved password belongs to someone
else, or a device running `AUTH_PROVIDER=legacy`.

```
[AUTH] Login attempt for <user>. Provider=KEYCLOAK. Tier 1: asking Keycloak (CentralIAM).
Keycloak login attempt failed (treating as offline): <network error>
[AUTH] Tier 2: checking <user> against the saved password on this device.
[AUTH] TIER 2 SUCCESS — offline login for <user> from cached credentials (KEYCLOAK was unreachable or rejected it). Session mode=offline.
```

---

## 5. Internet drops while someone is working

**What happens:** nothing. They keep working; the session is not re-checked
during use.

**Logs:** none. Silence here is the correct behaviour.

---

## 6. Device restarts (or reboots) while someone is logged in

**What happens:** they stay logged in. This is the behaviour that was broken
before this work — sessions were held in memory, so every restart logged
everybody out.

**Logs (at startup):**
```
[SESSION] 1 session(s) restored from the database — operators stay logged in across restarts.
```

**The proof:** a non-zero count. If it says `No active sessions` when somebody
*was* logged in, persistence is not working — see `9 - troubleshooting.md`.

---

## 7. The daily check, account still valid

**What happens:** once a day the backend confirms with Keycloak that each
logged-in account is still valid, and stamps `last_verified_at` to record that
it did so. Nothing about the session's validity changes — it was already valid
indefinitely. The operator notices nothing.

**Logs:**
```
[REVALIDATE] Pass triggered by the scheduled timer.
[REVALIDATE] Check starting — 1 online session(s) due for verification with Keycloak.
[SESSION] Verified for <user> — Keycloak confirmed the account is still valid. Stays logged in.
[REVALIDATE] Done: 1 checked -> 1 verified, 0 flagged, 0 unreachable; 0 offline session(s) asked to sign in again.
```

**The proof:** `1 verified`. Note that a run with **no** `verified` is not
itself a problem for the operator — nothing expires — but it does mean the
check is not getting through, which is worth investigating.

---

## 7a. A later pass on the same day, with nothing to do

**What happens:** the worker wakes every 6 hours, but a session Keycloak already
confirmed since midnight UTC today is not due again until tomorrow. So most
passes contact nobody at all and finish instantly. This is the normal, healthy
case, not a fault.

**Logs:**
```
[REVALIDATE] Pass triggered by the scheduled timer.
[REVALIDATE] Nothing due — every online session has already been confirmed with Keycloak today (or there are none).
```

**The proof:** "Nothing due". The extra wake-ups exist so that a device which
was unreachable at the first attempt of the day gets more chances before
tomorrow — on a device that is simply online, they cost nothing.

---

## 7b. A successful sync triggers an extra pass

**What happens:** when the sync retry worker finishes a cycle in which anything
was delivered or rejected, a real answer came back from Qualix — so the network
is genuinely up right now, which is the best possible moment to talk to
Keycloak. The sync worker calls the revalidation entry point directly.

This is free when there is nothing to do: the once-a-day rule still applies, so
if the session was already confirmed today the pass contacts nobody. It is most
useful on a device that has been out of signal — the check that failed earlier
today gets retried the instant connectivity is proven.

The two workers keep their own separate schedules; the sync worker simply calls
this one function and knows nothing else about authentication.

**Logs:**
```
Retry cycle: 3 pending -> 3 delivered, 0 rejected, 0 still pending
[REVALIDATE] Pass triggered by a successful sync (so the network is up).
[REVALIDATE] Check starting — 1 online session(s) due for verification with Keycloak.
[SESSION] Verified for <user> — Keycloak confirmed the account is still valid. Stays logged in.
[REVALIDATE] Done: 1 checked -> 1 verified, 0 flagged, 0 unreachable; 0 offline session(s) asked to sign in again.
```

If a pass is already running when another is triggered, the second is skipped
rather than queued — two passes can never overlap:

```
[REVALIDATE] Skipping the pass triggered by a successful sync (so the network is up) — one is already running.
```

Nothing happens here at all when `AUTH_PROVIDER` is not `keycloak` or
`SESSION_REVALIDATION_ENABLED` is false.

---

## 8. The check runs, but the device is offline

**What happens:** **nothing at all.** The session is left exactly as it is and
still counts as due today, so the next pass — the next 6-hourly wake-up, or the
moment a sync succeeds — tries again. An offline device must never be logged out
for being offline; this is the whole scenario the design exists to support.

**Logs:**
```
[REVALIDATE] Check starting — 1 online session(s) due for verification with Keycloak.
[REVALIDATE] Could not reach Keycloak for <user> — leaving the session untouched (an offline device must not be logged out). Will try again next cycle.
[REVALIDATE] Done: 1 checked -> 0 verified, 0 flagged, 1 unreachable; 0 offline session(s) asked to sign in again.
```

If there is also a session that was created offline, the run says so and still
does nothing:

```
[REVALIDATE] 1 offline session(s) still cannot be confirmed — Keycloak is unreachable. Leaving them alone; the operator keeps working.
```

**The proof:** `unreachable` in the summary, and **no** `flagged`. If a device
offline for a day shows `flagged`, something is wrong — an unreachable server is
being mistaken for a rejected account.

This can repeat for as long as you like. There is no day count at which an
offline device starts logging people out, because there is no session-lifetime
setting any more.

---

## 9. An administrator disables someone's Keycloak account

**What happens:** the daily check notices. The operator is **not** thrown out
immediately — if they are mid-batch, it continues and saves normally. They are
signed out only when they next reach the Home screen.

**Logs (when the check runs):**
```
[REVALIDATE] Keycloak REJECTED <user> (account disabled/deleted, or the token went unused too long) — flagging for re-login.
[SESSION] Flagged <user> for re-login — Keycloak rejected the account. The session still works on purpose: they are only signed out when they next reach the Home screen, so nothing in progress is lost.
[REVALIDATE] Done: 1 checked -> 0 verified, 1 flagged, 0 unreachable; 0 offline session(s) asked to sign in again.
```

**Then, when they reach Home:** the frontend sees `needs_relogin` on
`GET /api/auth/me` and shows a **popup** — "Please sign in again", with a single
**Sign in again** button and no way to dismiss it. There is no backend log for
that step — it happens in the browser. Confirm it from the access log:
```
INFO:  ... "GET /api/auth/me HTTP/1.1" 200 OK
```

**The proof:** `flagged` in the summary, and the session still present in the
database afterwards (deliberately not deleted).

**This is the only way a session ever ends.** Keycloak has to explicitly reject
the account. Time alone never does it.

---

## 9b. A device that was offline gets its internet back

**What happens:** an operator who signed in while the device was offline has no
Keycloak refresh token, so their session can never be checked silently — no
amount of waiting will confirm it. The daily check spots these with
`SessionStore.unverifiable_sessions()` and, **only if such a session exists**,
probes Keycloak with `is_reachable()` (a GET on the realm's
`.well-known/openid-configuration`).

If Keycloak answers, those sessions are flagged for re-login: asking the operator
to type their password once is the only way the account can ever actually be
checked. If Keycloak does not answer, they are left completely alone.

**Logs:**
```
[REVALIDATE] <user> signed in offline (mode=offline) and Keycloak is reachable again — asking them to sign in once at the Home screen so the account can actually be checked.
[REVALIDATE] Done: 0 checked -> 0 verified, 0 flagged, 0 unreachable; 1 offline session(s) asked to sign in again.
```

**Then, when they reach Home:** the same "Please sign in again" popup as flow 9.
Nothing in progress is interrupted, and signing in works offline as well, so the
popup can never strand anybody.

**The proof:** a non-zero `offline session(s) asked to sign in again` count,
paired with the per-user `signed in offline ... Keycloak is reachable again`
line. If the device is still offline you should instead see
`still cannot be confirmed — Keycloak is unreachable` and nothing else.

---

## 10. Operator logs out, then logs back in with no internet

**What happens:** it works, and it is instant — logging out does not erase the
saved password, so the fast path matches it just as it would have before.

**Logs:** identical to flow 4 (`FAST LOGIN`, then `could not reach Keycloak`).
The fact that a logout happened in between makes no difference.

---

## 11. Device offline for more than 30 days

**What happens:** the device stays logged in and fully usable the entire time —
being offline never ends a session. But the stored Keycloak **offline refresh
token** has a 30-day idle window of Keycloak's own (Offline Session Idle, see
`3 - keycloak_configuration.md`). Once the device has gone that long without
using it, Keycloak rejects it on the next successful contact, and the operator is
asked to sign in once.

This is the one everyday case where a long offline stretch does eventually lead
to a re-login — but note the cause: **Keycloak rejecting the token**, not the
device deciding time was up. The device itself has no such rule.

**Logs:** same as flow 9 (a rejection), followed by a normal flow 1 login when
they sign in again.

**Not a lockout:** the device kept working offline the whole time, and the
re-login itself also works offline against the saved password.

---

## 12. A scan syncs to Qualix

**What happens:** the backend authenticates as the **fixed sync account** — never
the logged-in operator — and sends the result through the Assurance gateway. This
is identical whether the operator logged in online, offline, or with device
credentials.

**Logs (first sync after a restart):**
```
[SYNC] Getting a Keycloak token for the fixed sync account (cgi.op3@agnext.in) — this is never the logged-in operator.
[SYNC] Sync account token obtained.
```

**If it fails:**
```
[SYNC] Keycloak login FAILED for the sync account — syncing cannot proceed.
```

**The proof:** the sync account's username appears, not the logged-in operator's.
If you see the operator's name here, something is wrong.

**Who Qualix thinks ran the batch:** not the sync account. The payload carries
`operator_id`, `device_serial_no` and `warehouse_name` as explicit fields, so
the authenticating identity and the recorded identity are deliberately two
different things. See `4 - assurance_gateway.md`.

---

## 12bb. Two things try to send the same scan at once

**What happens:** only one of them sends it.

A record is marked `'0'` for the whole time its POST is in flight — around half
a minute against this endpoint — so the retry worker can list it as unsent
while the post that followed the batch is still running. Pressing Re-sync
during that window, or twice, is the same situation.

Whichever gets there first holds a claim on that record
(`services/sync_lock.py`). The others stand down:

```
Result 412 is already being delivered — not starting a second attempt.
```

and a manual Re-sync is answered `409 This record is already being sent. Give
it a moment — the status updates on its own.`

**Why it matters:** Qualix would have coped (it answers `12063`, see above),
but Google Sheets would not — `post_to_sheets` checks for an existing row and
*then* appends, so two deliveries overlapping between those steps both append
and the sheet gets two rows for one scan.

---

## 12ba. Qualix says the sample already exists

**What happens:** it is marked **Synced**, not Rejected.

`12063 Sample ID already exists` means Qualix already holds this scan from an
earlier attempt — normally one that timed out on the way back, leaving this
device thinking it had failed. The scan is safe; the record is set to `'1'`
and any stored error is cleared.

**Logs:**
```
Qualix already has sample T11790155376 (error 12063) — counting it as delivered, not rejected.
```

WARNING rather than INFO on purpose: the outcome is fine, but it means a
delivery was recorded as failed when it had in fact succeeded. A run of these
points at the POST timeout being too tight for how slow the endpoint is.

---

## 12b. Qualix rejects the scan

**What happens:** Qualix answers HTTP 400 with a reason, e.g.
`{"error-code":"12092","error-message":"Device does not exist"}` — usually
meaning `DEVICE_CODE` does not match a serial Qualix has registered.

The record is marked `sync_status='2'`, which is **terminal**: the retry worker
deliberately does not pick it up again, because nothing about retrying an
unchanged rejected payload would produce a different answer.

Two consequences worth knowing:

- **It is not written to Google Sheets.** Only an accepted post is. Sheets and
  Qualix would otherwise disagree about which scans exist.
- **The reason is stored on the record** (`result.sync_error`) and shown in the
  History list and on the record detail page, so it does not live only in the
  backend log.

**To recover:** fix the setting the reason points at, then use the manual
re-sync on that record.

---

## 12c. The operator presses Save, the network drops, and they press Save again

**What happens:** nothing is saved twice. `/api/scan/submit` minted a UUID for
this save and the frontend kept it; every retry carries that same UUID, and
`/api/scan/confirm` recognises the repeat and answers with the `result_id` it
already stored.

This holds even if the operator leaves the results page and comes back before
retrying — the key lives in `sessionStorage`, not in the page.

**Logs:**
```
Replay of /confirm for request 550e8400-... — returning the existing result 412 instead of saving the scan again.
```

**Why it matters:** before this, the retry was answered `409 Nothing to confirm
— submit a result first`, because the first attempt had already consumed the
pending slot. The operator read that as a failed save for a scan that had in
fact been saved and synced — and the natural response, re-running the whole
batch, is what actually produced duplicates.

**A replay deliberately does not re-queue the sync.** The first attempt already
did, and the retry worker picks up anything still pending; posting again would
risk a duplicate reaching Qualix.

---

## 13. The periodic retry for anything unsent

**What happens:** same as flow 12 — its own fixed account, unrelated to who is
logged in. Runs even when nobody is logged in at all.

**Logs:**
```
Retry cycle: 2 pending -> 2 delivered, 0 rejected, 0 still pending
```

---

## 14. A device still on `legacy`

**What happens:** exactly as before this work. Qualix checks the password.

**Logs:**
```
Auth provider: QUALIX direct/legacy (https://assaying-dev.qualix.ai/). Set AUTH_PROVIDER=keycloak to switch.
[AUTH] Login attempt for <user>. Provider=QUALIX (legacy). Tier 1: asking Qualix.
[AUTH] TIER 1 SUCCESS — online login for <user> via QUALIX (legacy direct login). Session mode=online, offline password cached.
```

---

## Quick reference

| To check | Look for |
|---|---|
| Which system is in use | `Auth provider:` at startup |
| Which system checked a password | `via KEYCLOAK` or `via QUALIX` |
| Whether a login took the fast path | `[AUTH] FAST LOGIN` |
| How a fast login turned out | `Background check CONFIRMED` / `REJECTED` / `could not reach Keycloak` |
| Whether a fast-path session became a real online one | `[SESSION] Promoted <user> to a verified online session` |
| Whether a rejected password was removed from the device | `Cleared the saved password on this device` |
| Whether login was online or offline | `TIER 1` / `TIER 2` / `TIER 3` |
| Whether sessions survive restarts | `[SESSION] N session(s) restored` |
| Whether the revalidation worker runs | `[REVALIDATE] Pass triggered by` |
| Whether an offline device is treated correctly | `unreachable`, never `flagged` |
| Whether an offline session was asked to re-login | `offline session(s) asked to sign in again` |
| Whether a login was refused for not being a Qualix user | `USERNR01`, `TIER 1 REJECTED` |
| Why someone was asked to sign in again | `Keycloak REJECTED`, `Background check REJECTED`, or `signed in offline ... reachable again` — never a time limit |
| Which account syncing uses | `[SYNC] Getting a Keycloak token for the fixed sync account` |
| Whether an operator's Qualix id changed | `[AUTH] operator_id changed for <user>: <old> -> <new>` |
| Why Qualix rejected a scan | `sync_error` on the record, shown in History and on the record page |
| Whether a Save was a retry rather than a new scan | `Replay of /confirm for request <uuid>` |
| Whether a scan had already reached Qualix on an earlier try | `Qualix already has sample <id> (error 12063)` |
| Whether a second delivery of the same scan was blocked | `Result <id> is already being delivered` |
