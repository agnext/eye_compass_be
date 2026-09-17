# 2 — How login works

**What this file is:** what actually happens when an operator types their
username and password, in every situation the device can be in.

---

## The fast path, then the three tiers

When someone logs in under `AUTH_PROVIDER=keycloak`, the backend first tries the
**fast path**. Only if that does not apply does it fall into the three tiers
below, in order, stopping at the first that works.

### The fast path — the password saved on this device (no network call at all)

Before Keycloak is contacted, the typed password is compared against the hash
saved on this device from a previous successful online login. If it matches, the
operator is **signed in immediately** — no network round trip is made.

Why this comes first: the slow case was never the online one, it was the
**offline** one. On a device with no network, reaching Keycloak fails only after
the connection times out, so the operator watched a spinner for around ten
seconds before a login that was always going to come from the saved password
anyway. Measured on a device:

| Route | Time to sign in |
|---|---|
| Fast path | about 0.015 s |
| Tier 1 online | 2–4 s |
| Tier 1 while offline (timeout, then Tier 2) | about 10 s |

What the fast path creates:

- A session with `mode="pending"`. That is the only honest label at that
  instant: nothing has been verified yet, the session holds no Keycloak
  refresh token, and it is not yet known whether this device even has a
  network. The background check settles it to `online` or `offline`.
- The HTTP response reports `"mode": "cached"` — distinct from `offline`,
  because the device may well have a network and telling the operator their
  results will sync later could be wrong.
- A **background verification task**, scheduled to run the moment the response
  has been sent. This is what makes the fast path safe, and is described in its
  own section below.

If the saved password does **not** match — a different operator, a first login
on this device, or a genuinely wrong password — nothing above happens and the
login proceeds through the tiers exactly as described next.

### Tier 1 — Ask Keycloak, then confirm they are a Qualix operator (needs internet)

The username and password are sent straight to Keycloak. Keycloak saying yes is
**not enough by itself** — Keycloak only proves the password is valid somewhere
on the shared `CentralIAM` realm, which also has accounts that have nothing to
do with Qualix. So a second check follows immediately: the operator's brand-new
Keycloak token is used to call the Assurance gateway's `api/user/keycloak-profile`
endpoint — the exact same call Qualix's own website makes right after a
Keycloak login, to look up who this actually is in Qualix's own records.

- **A real Qualix operator** gets back their profile (name, customer,
  permissions). Login proceeds.
- **Anyone else** — a valid Keycloak account with no matching Qualix user —
  gets an explicit rejection (HTTP 500, `"message": "USERNR01"`). The login is
  refused outright, exactly as if the password had been wrong.

Only once both checks pass:

- A **local session** is created. It stays valid for as long as Keycloak keeps
  confirming the account (see `7 - sessions_and_expiry.md`).
- The password is **hashed and saved on the device**, so the operator can log in
  later without internet (this is what makes Tier 2 possible).
- A **refresh token** from Keycloak is saved with the session, used later to
  check the account is still valid (see `7 - sessions_and_expiry.md`).
- The commodity/vendor/brand list is refreshed in the background.
- The operator's real name and customer, from the profile lookup above, are
  used for display instead of whatever (often blank) fields Keycloak itself
  returned.

**If the gateway cannot be reached to run this second check at all** (not
rejected — genuinely unreachable), it is treated as inconclusive rather than a
rejection: the login is allowed to proceed on Keycloak's answer alone, same as
before this check existed. Only an explicit `USERNR01` blocks the login.

### Tier 2 — Check the saved password (works with no internet)

If Keycloak cannot be reached, or rejects the password, the backend compares
what was typed against the hashed password saved during a previous successful
online login on **this device**.

This tier is the whole reason offline working is possible. It works whether the
device simply has no internet, or the operator deliberately logged out earlier —
**logging out does not erase the saved password.**

### Tier 3 — The device's own fixed login (last resort)

If neither of the above matches, the backend checks against one fixed
username/password stored in the device's settings file. This exists so a brand
new device that has never been online is still usable.

If all three fail, the login is refused.

> Tiers 2 and 3 behave identically whether `AUTH_PROVIDER` is `keycloak` or
> `legacy`. Only Tier 1 differs — which system is asked to check the password.
> The fast path applies under `keycloak` only.

---

## The background check that makes the fast path safe

Right after a fast-path login's response is sent,
`_verify_cached_login_in_background` in `app/api/auth.py` asks Keycloak about
**the exact username and password the operator just typed** — a real password
login, not a refresh-token redemption.

That distinction is the whole point. A **changed password** is the main thing
this check exists to catch, and Keycloak's offline refresh tokens generally
survive a password change: redeeming one would succeed and prove nothing. Only
sending the real password actually tests it.

There are three outcomes.

| Keycloak's answer | What happens |
|---|---|
| **Confirmed** | `session_store.promote_to_online(...)` upgrades the session in place. |
| **Explicitly rejected** | The session is flagged for re-login **and** the saved password is deleted. |
| **Unreachable** | Nothing changes at all. |

**Confirmed.** The session's `mode` becomes `online`, the Keycloak refresh token
is stored on it, first name / email / roles are filled in, `last_verified_at` is
stamped and `needs_relogin` is cleared. From that moment the session is
indistinguishable from one created by a normal online login, and is picked up by
the once-a-day check like any other. The commodity/vendor/brand config sync is
triggered too, exactly as a normal online login would.

**Explicitly rejected** — the password was changed, or the account was disabled.
Two things happen:

- The session is flagged for re-login but **deliberately keeps working**. An
  operator part-way through a batch is never interrupted; they are prompted by
  the popup at the **Home screen**, the same as every other re-login prompt.
- The saved password row on this device is **deleted**. This is what stops a
  stale password from working offline forever, and it is also what forces the
  *next* login through Keycloak: with nothing to match, the fast path simply
  does not apply. No extra flag or database column was needed for that — the
  absence of the row **is** the rule.

The operator is not stranded by this. Tier 3, the fixed device credential, still
works, and a successful online login saves a fresh password again.

**Unreachable** — the device genuinely has no network. Nothing about the
account's standing changes, and the saved password is *not* deleted. The only
thing recorded is the fact of being offline: the session settles from `pending`
to `mode="offline"`, which is what lets the Home screen show *"Signed in
offline — results will sync later"*. That notice deliberately never appears
while the check is still `pending`, so an operator who is really online is
never told otherwise. Being offline proves nothing about the account. Such a session is later
picked up by the existing "signed in offline, Keycloak reachable again" flagging
when connectivity returns (see `7 - sessions_and_expiry.md`).

Telling "Keycloak says no" apart from "we could not ask" is what makes this
safe, and it is what `keycloak_service.login()` records in
`last_login_was_explicit_rejection` — true on an HTTP rejection, false when the
server was unreachable. It mirrors the existing
`last_refresh_was_explicit_rejection` and
`last_profile_check_was_explicit_rejection` flags.

The Qualix membership check runs here too: if the confirmed Keycloak account is
no longer a registered Qualix user, it is treated the same as a rejection.

---

## Every situation, in plain words

**1. First login ever, internet working**
Nothing is saved on the device yet, so the fast path does not apply. Keycloak
checks the password, the operator is logged in, and the password is saved on the
device for future use. They stay logged in from then on.

**2. First login ever on a device, no internet, operator never used this device**
Nothing is saved for this person yet, so there is nothing to check against. Only
the fixed device login (Tier 3) will work. This is the one genuine case that
needs internet at least once.

**3. Normal day, internet working**
The saved password matches, so they are signed in instantly — no waiting on the
network. A background check confirms the credentials with Keycloak a moment
later and quietly upgrades the session to a full online one. The operator sees
none of this; they just see a login that is effectively immediate.

**4. No internet, but this operator has logged in on this device before**
Their saved password matches and they are signed in instantly, with no timeout
to wait through. The background check cannot reach Keycloak, so it leaves
everything exactly as it is.

**5. Operator is working and the internet drops mid-shift**
Nothing happens. They keep working. No re-checking, no interruption.

**6. Internet comes back while someone is logged in**
Once a day, quietly in the background, the backend confirms with Keycloak that
the account is still valid and records that it did so. The operator notices
nothing, and stays logged in.

**7. An operator's Keycloak account is disabled by an administrator**
The daily check notices. **They are not thrown out immediately** — if they are
part-way through a batch scan or data collection, that continues and saves
normally. They are only signed out the next time they return to the Home screen,
with a message asking them to sign in again. No work is ever lost.

**7a. An operator's password is changed, or their account is disabled, and they
log in again on a device with a network**
The saved password still matches, so they are signed in instantly — but the
background check puts those exact credentials to Keycloak and is told no. The
session is flagged, and the saved password on this device is deleted so it can
never be used offline again. The operator finishes whatever they are doing and
is asked to sign in again at the Home screen. Their next login has to go through
Keycloak, because there is no longer anything on the device to match against.

**8. Operator logs out, then wants to log back in with no internet**
This works, as long as they have logged in on this device before — the saved
password is still there and the fast path matches it immediately. Logging out
does not delete it.

**9. Device is offline for more than 30 days straight**
The check cannot run during that time. When internet returns, the saved
refresh token may be too old to renew, so the operator will need to log in
normally once. This is not a lockout, just a normal login.

**10. A device still set to `legacy`**
Everything behaves exactly as it did before this work — Qualix checks the
password. No difference at all.

**10a. A device on `keycloak` where the saved password belongs to someone else**
The fast path only matches the one operator whose password is saved (the table
holds a single row). Anyone else falls straight through to Tier 1 and a normal
Keycloak login.

**11. A valid Keycloak login for someone who is not a Qualix operator**
Keycloak accepts the password — they are a real account on the shared realm —
but the Qualix membership check comes back `USERNR01`. The login is refused
with the same "Invalid credentials" message as a wrong password. Nothing is
cached, nothing is created; this person is never allowed onto the device.

---

## What is written in the logs

Every login now says which system checked the password, so this can be confirmed
from `journalctl -u eye-compass-backend.service` (or `scripts/logs.py --auth`):

```
[AUTH] FAST LOGIN for <user> — matched the password saved on this device. Signed in now; confirming with Keycloak in the background.
[AUTH] TIER 1 SUCCESS — online login for <user> via KEYCLOAK (realm=CentralIAM, client=qualix-backend, roles=[...], qualix_customer=<customer>). Session mode=online, offline password cached, config refresh queued.
[AUTH] TIER 1 SUCCESS — online login for <user> via QUALIX (legacy direct login). Session mode=online, offline password cached.
[AUTH] TIER 1 REJECTED for <user> — authenticated fine by Keycloak, but is not a registered Qualix user. Login refused.
[AUTH] TIER 2 SUCCESS — offline login for <user> from cached credentials (KEYCLOAK was unreachable or rejected it). Session mode=offline.
[AUTH] TIER 3 SUCCESS — offline login for <user> from the fixed device credentials. Session mode=offline-device.
```

A fast login is always followed, seconds later, by exactly one of the three
background-check outcomes:

```
[AUTH] Background check CONFIRMED <user> with Keycloak — the fast login was legitimate.
[SESSION] Promoted <user> to a verified online session — Keycloak confirmed the credentials just used are still valid.
```

```
[AUTH] Background check REJECTED <user> — Keycloak says these credentials are no longer valid (password changed, or account disabled). Flagging for re-login at the Home screen.
[SESSION] Flagged <user> for re-login — Keycloak rejected the credentials used to sign in. The session still works on purpose: they are only asked to sign in again when they next reach the Home screen, so nothing in progress is lost.
[AUTH] Cleared the saved password on this device — Keycloak rejected it, so it must not keep working offline. The next login has to go through Keycloak.
```

```
[AUTH] Background check could not reach Keycloak for <user> — leaving the session alone. It will be checked again when there is a network.
```

And, if Keycloak accepts the credentials but the account is no longer a Qualix
operator, the same flag-and-clear pair is preceded by:

```
[AUTH] Background check: <user> is no longer a registered Qualix user. Flagging for re-login.
```

The backend also states which system it is using at startup:

```
Auth provider: KEYCLOAK (https://dev.perfeqtfoods.com/keycloak, realm=CentralIAM, client=qualix-backend). Sync via Assurance: ...
```

This was added because a successful login otherwise looks identical in the logs
either way, making it impossible to tell which system a device was actually using.
