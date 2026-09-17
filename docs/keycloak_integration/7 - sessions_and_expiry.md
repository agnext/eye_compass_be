# Staying logged in, and being logged out

**What this file is:** how staying logged in works, how it is kept alive, and
what happens when someone's account is disabled.

---

## The basic promise

An operator logs in once and **keeps working indefinitely** — no repeat logins,
and **no internet needed** in between. This device regularly has no
connectivity, so a login must never depend on it.

There is no expiry date stored anywhere, and **no session-lifetime setting at
all**. The rule is a single flag:

```
valid = not needs_relogin
```

In plain words: *a session is valid until Keycloak tells us the account is no
longer good.* Nothing else ends one — in particular, **time never does**.

A device that has been out of signal for six months knows exactly as much about
its operator's account as it did on day one: nothing has changed, nothing has
been learned. Logging the operator out at that point would be punishing them
for the network, not for anything about their account. So the device simply
keeps them logged in and asks Keycloak the moment it can.

`last_verified_at` is still recorded — it is genuinely useful when reading logs
to know when an account was last confirmed — but it **never decides whether a
session is valid**. Its one functional job is the once-a-day rule below:
skipping a session that Keycloak already confirmed today.

## How it is stored

The session lives in the `sessions` database table. Because it is in the
database, it **survives restarts and reboots** — see `5 - database_changes.md`
for why this had to be fixed before anything else worked properly.

---

## The once-a-day check

Once a day, quietly in the background, the backend takes each logged-in
operator's saved refresh token and asks Keycloak: *"is this account still fine?"*

There are exactly three possible answers, and the difference between the second
and third matters enormously:

| Keycloak's answer | What happens |
|---|---|
| **Yes, still valid** | `last_verified_at` is stamped with the current time. The operator notices nothing and stays logged in. |
| **No — account disabled or deleted** | The session is **flagged** for re-login (see below). |
| **Could not reach Keycloak** | **Nothing at all.** The session is left completely alone, and is still due today, so the next pass tries again. |

That last row is the important one. A device with no internet must never be
punished for being offline — that is the exact situation this whole design exists
to support. So "Keycloak said no" and "we could not ask Keycloak" are treated as
completely different things, and the code checks which one occurred rather than
treating any failure the same way.

The worker calls `session_store.mark_verified(...)`. The name is the point: it
**records a fact** — "Keycloak said yes just now". There is no clock being
pushed forward, because there is no clock.

### Sessions that started offline and were promoted

A login accepted from the password saved on the device creates its session as
`mode=offline` and then has those same credentials checked with Keycloak in the
background. If Keycloak confirms them, the session is **promoted**: it gains the
refresh token, becomes `mode=online`, and has `last_verified_at` stamped there
and then. From that point the daily check treats it exactly like any other
online session — it is due again tomorrow, and the three answers above apply to
it normally. There is nothing special about it afterwards.

If the background check is rejected, the session is flagged for re-login
immediately rather than waiting for the daily check. If Keycloak could not be
reached, it simply stays `mode=offline` and falls under the case below.

### The fourth case: someone who signed in offline

An operator whose session is still `mode=offline` — the device had no
connectivity, so the background check could not confirm anything — has **no
refresh token at all**. Keycloak was never successfully contacted, so there is
nothing to redeem. None of the three answers above can apply to them.

Such a session is left completely alone for as long as the device stays
offline. But once Keycloak becomes reachable again, it is **flagged for
re-login**, because asking the operator to type their password once is the only
way that session can ever be checked.

This costs them almost nothing:

- If the device really is online, they sign in via Keycloak and get a normal,
  silently-renewing session from then on.
- If connectivity has dropped again by the time they sign in, the login simply
  falls through to the offline tiers and they carry on exactly as before.

The reachability check is only made when such a session actually exists, so a
fleet of normally-online devices never makes that call.

### Once a day, but looked at more often

Each session is verified with Keycloak **at most once per day**. The rule is
enforced by the session row's own `last_verified_at` column: a session Keycloak
already confirmed since midnight UTC today is simply not selected as due, so
nothing is asked about it again until tomorrow. No separate bookkeeping exists
for this, which also means the rule **survives a backend restart** — restarting
the service does not buy a session a second check today.

The day boundary is **midnight UTC** (`_start_of_today()` in
`app/services/session_worker.py`), matching every other timestamp in the system.

The worker nevertheless **wakes up more often than once a day**:
`SESSION_REVALIDATION_INTERVAL_HOURS` (default `6`) is *how often it looks*, not
how often it asks Keycloak. Waking roughly four times a day costs essentially
nothing, because a session already confirmed today is skipped without contacting
Keycloak at all. The point of the extra wake-ups is the device that was offline
or unreachable at the first attempt of the day: it gets several more chances
before the day is out, rather than losing the whole day to one bad moment.

| | |
|---|---|
| Wake-ups per day | about 4 (every 6 hours) |
| Actual Keycloak calls per session per day | normally 1 |

### Two triggers, one entry point

A pass can be started two ways:

1. **The scheduled timer** — the wake-up described above.
2. **A successful sync.** When the sync retry worker completes a cycle in which
   anything was delivered or rejected, that is proof a real answer came back
   from Qualix — the network is genuinely up *right now* — so it calls
   `run_revalidation_now(...)`. This is free when there is nothing to do: the
   once-a-day rule still applies, so if the session was already confirmed today
   the pass finds nothing due and contacts nobody.

Both go through the same `run_revalidation_now()` entry point, which holds a
lock so two passes can never overlap. If one is already running, the second is
skipped with a log line rather than queued.

The two workers remain **separate, on their own schedules**. The sync worker
keeps its own cadence (`SYNC_RETRY_INTERVAL_MINUTES`, default 30) and knows
nothing about authentication beyond calling that one function; the revalidation
worker keeps its own timer. Nothing is merged — the sync worker simply offers an
opportunistic extra trigger at a moment when connectivity is known to be good.
`run_revalidation_now()` also guards itself: it does nothing when
`AUTH_PROVIDER` is not `keycloak` or `SESSION_REVALIDATION_ENABLED` is false, so
the sync worker needs no auth-related conditions of its own.

---

## Being logged out when an account is disabled

When an administrator disables someone's Keycloak account, it is noticed either
by the daily check or — if they sign in again on a device with a network — by
the background check that follows that login, within seconds. Either way **the
operator is not thrown out immediately**.

Instead:

1. The session is marked `needs_relogin`, but **keeps working normally**.
2. If the operator is part-way through a batch scan or data collection, it
   carries on and saves as usual. **Nothing is lost.**
3. The next time they return to the **Home screen**, a popup appears asking
   them to sign in again, with a single "Sign in again" button.

It is a popup rather than an abrupt redirect on purpose: the operator sees an
explanation of what happened and presses a button, instead of being bounced to
a login screen with no warning. The popup has no dismiss option — signing in is
the only thing left to do on that screen — but it is never a dead end, because
signing in works offline too.

The Home screen is the only place this is checked, and that is the whole point:
it is the one screen an operator can only reach **between** tasks. Checking there
means there is no way to interrupt work in progress.

### Timing, stated plainly

- The **check with Keycloak** happens **once a day per session** (the worker
  looks every 6 hours, but skips anything already confirmed today). A
  successful sync can also trigger an earlier pass on a day where the check has
  not yet succeeded.
- **Acting on it** is not on a timer — it happens whenever the operator next
  lands on Home, using a flag already stored locally. No extra Keycloak call.

So a disabled account may keep working for **up to about a day** before being
noticed, then until the operator finishes what they are doing. That is a
deliberate trade: uninterrupted work is considered more important than instant
lockout on a device that is often offline anyway, and a kiosk that may be out of
signal for days cannot be made prompt about this in any case. The figure is a
worst case — in practice the check usually lands in the first pass of the day,
or earlier still if a sync succeeds.

---

## The 1-day trap, and why `offline_access` matters

Keycloak's realm is configured so a **normal** login dies after **1 day**, no
matter what — see the table in `3 - keycloak_configuration.md`.

If the app had used a normal login, every session would have quietly stopped
working after a day, no matter what the session settings said. It would have
looked perfect in testing and failed in the field a week later.

Requesting the `offline_access` scope produces a different kind of long-lived
login with **no absolute limit**. It has a 30-day *idle* timeout instead: the
clock resets every time the token is used. The daily check uses it, so a device
with any regular connectivity keeps itself alive indefinitely.

**How to verify:** the saved refresh token should decode to `typ: Offline` with no
expiry. If it ever says `Refresh` instead, the 1-day cap is back and sessions
will start dying after a day.

### The one case where this bites

A device offline for **more than 30 consecutive days** will find its saved token
too old to renew when connectivity returns. Keycloak answers "no" to that day's
check, so the session is flagged and the operator is asked to sign in
again the next time they reach Home.

Note what does **not** happen: they are not logged out during those 30 days, and
they are not logged out on day 31 either. They keep working offline the entire
time, and are only ever prompted once the device can actually reach Keycloak
again — at which point signing in takes a few seconds. **This is the only thing
that ever forces a fresh login.**

---

## What the operator actually experiences

- Logs in once, works for weeks or months, never prompted again.
- Logs in again on a device they have used before — signed in near-instantly,
  online or off, with no spinner while the network is tried.
- Loses internet — nothing changes, keeps working. Indefinitely.
- Reboots the device — still logged in (this was broken before; now fixed).
- Logs out deliberately with no internet — can log back in immediately, because
  the saved password is still on the device.
- Account disabled by an admin — finishes what they are doing, then sees a
  popup on the Home screen asking them to sign in again.
- Offline for months, then reconnects — finishes what they are doing, then sees
  the same popup. Signs in once and carries on.
