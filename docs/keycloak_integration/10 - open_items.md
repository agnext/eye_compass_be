# Open items and future work

**What this file is:** what is deliberately unfinished, known trade-offs, and
what would need doing before this goes to production devices.

---

## Known trade-offs (decided on purpose, not oversights)

### Using username/password instead of a redirect login

The textbook approach is to send the user to Keycloak's own login page. This
integration deliberately does not.

**Why:** if Keycloak's page collects the password, our backend never sees it —
and then there is nothing to hash and store for offline login. Offline working
would be impossible, which is unacceptable for this device.

**Consequence:** this method **cannot support MFA / two-factor login**. If MFA is
ever enforced on these accounts, this login method stops working entirely and the
redirect approach becomes mandatory.

**If that happens:** add the redirect flow for online logins, and keep the
current form as the **offline-only** path. The `LOGIN_UI_MODE` setting already
exists as the switch for this; nothing built so far would need undoing.

### Any Qualix operator can log in — restricted to Qualix, not by role, customer, or site

Login requires being a real Qualix operator: Keycloak checks the password, and
a second call to the Assurance gateway's `api/user/keycloak-profile` confirms
the account is actually registered in Qualix, rejecting anyone who only has an
unrelated account on the shared Keycloak realm. See `2 - how_login_works.md`
and `4 - assurance_gateway.md`.

Beyond that single check, there is no restriction by role, customer or site —
any Qualix operator can log in regardless of which customer or role they
belong to. This matches how the system has always worked — the legacy app
accepted any valid Qualix login too — so it is not a new gap. Keycloak would
make restricting it further easy (via groups or roles), if that is ever
wanted.

### A disabled account keeps working for up to about a day

Each session is verified with Keycloak at most once per day, and the result is
then only enforced when the operator reaches the Home screen. Deliberate:
uninterrupted work was judged more important than instant lockout on a device
that is frequently offline — such a device may be out of signal for days at a
time, so no checking cadence can make revocation prompt in the field.

A day is the worst case, not the normal one: the worker looks every 6 hours and
a successful sync triggers an extra pass, so on a connected device the check
usually lands early in the day. And any fresh login on a connected device is
checked with Keycloak within seconds of being accepted, so a disabled account is
caught at that point too. See `7 - sessions_and_expiry.md`.

---

## Sessions never expire on time, only on evidence

A session is valid until Keycloak explicitly says the account is no longer
good:

```
valid = not needs_relogin
```

A login lasts indefinitely. If the device cannot reach Keycloak, the operator
keeps working — for as long as that takes, with no limit. `last_verified_at` is
recorded on every check, but never decides validity; it only drives the
once-a-day rule that skips a session already confirmed today.

Two consequences worth stating plainly:

- **Offline logins get flagged when connectivity returns.** An operator who
  signed in offline has no refresh token, so their session can never be checked
  silently. Once Keycloak is reachable again they are asked to sign in once, at
  the Home screen, because that is the only way that session can ever be
  confirmed. If they are offline again by then, the login just falls through to
  the offline tiers and nothing is lost.
- **A device offline for 30+ consecutive days** finds its Keycloak offline token
  past its idle window, so the next successful contact is an explicit rejection.
  Same outcome: signed in again, once, at the Home screen. This is the only
  thing that routinely forces a fresh login.

### Still genuinely open

Whether there should be a **hard cap that forces re-login every N days even for
a device that is online regularly and whose account is completely fine** — security
hygiene rather than technical necessity. There is deliberately no such cap.

If one is ever wanted it needs its own field measured from `created_at` (e.g.
`SESSION_ABSOLUTE_MAX_DAYS`), since `last_verified_at` is reset by every check
and so cannot distinguish "open for an hour" from "open for a year". It would
need to apply only to **online** devices, and must not fire on an offline one.

**Recommendation:** leave it as is unless someone explicitly asks. Forcing a
periodic re-login on a shared kiosk mostly teaches operators to re-type an
unchanged password, with little security gain while the account itself is still
valid — and any real revocation is already caught within about a day.

### Related: a login from the cached password, and a revoked account

A login can be accepted from the password saved on the device without Keycloak
being consulted first — that is the fast path, and it is also what Tier 2 does
when Keycloak is unreachable or rejects the attempt. Taken alone that would let
an operator whose account was just disabled, or whose password was changed, keep
signing in on any device holding their saved password.

It does not, because every such login is checked against Keycloak in the
background immediately afterwards, with the exact credentials just typed. On a
device with a network the outcome lands **within seconds of the login**: the
session is flagged for re-login at the Home screen, the saved password is
deleted so it cannot be used offline again, and the next login is forced through
Keycloak because there is nothing left to match. See `2 - how_login_works.md`.

**The genuine residual, stated plainly:** on a device that is offline *and stays
offline*, a changed password or a disabled account still works from the cache
until connectivity returns. There is no way around this — the device has no
means of learning that anything changed, and refusing to let anyone in on that
suspicion would break the offline working this product exists to provide. The
window is "until this device next reaches Keycloak", not "indefinitely", and the
first moment it does reach Keycloak — whether through a login's background check
or the once-a-day session check — the account is caught.

---

## Still to do before production

### 1. Point at the production environment
Both `ASSURANCE_API_URL` and `QUALIX_API_URL` currently point at **dev**. The
production gateway address is needed, and the two must stay in step — otherwise
flipping `AUTH_PROVIDER` silently changes where scan data is stored.

### 2. Confirm production Keycloak details
The realm, `qualix-backend` client and the four scopes must exist in the
production Keycloak too. Do not assume the dev setup was copied across — the
scopes were custom-made for the gateway.

### 3. Test with a real operator account
All testing so far used `cgi.op3@agnext.in` (the service account) and
`cgi.admin@agnext.in`. A normal operator account should be tested too — in
particular that their roles and permissions come through correctly.

---

## Smaller known issues

### The dev environment has no surveyors
`surveyorDetails` came back empty from the dev config, so the Sorter Name
dropdown may be empty on a switched device. Believed to be a dev data gap rather
than a fault — worth confirming against production.

### Scan posting has not been tested end to end
The scan-posting route was confirmed to exist and accept the token, but no scan
was actually posted, to avoid writing test data. The first real batch through a
switched device should be watched.

In particular, Qualix may check the `permissions` claim (which contains
`scan_visio`, matching the `post-visio` endpoint) on that call. It is included in
the token, but that it is *accepted* has not been proven.

---

## What was verified, and how

Worth recording so nobody re-tests what is already proven:

| Verified | Method |
|---|---|
| Keycloak login works | Real login against the live server |
| The long-lived token works | Refresh token decoded — `typ: Offline`, no expiry |
| Renewal works repeatedly | Two chained renewals against the live server |
| Disabled accounts are detected | Rejection correctly distinguished from unreachable |
| Offline devices are not punished | Unreachable Keycloak leaves sessions untouched |
| Sessions survive restarts | Created in one process, read in another |
| Offline login works after logout | Tested via the real login path |
| Wrong passwords are refused | Tested |
| Commodity config loads via gateway | Real fetch — 17 commodities, 9 vendors, 8 brands |
| Legacy mode is unchanged | Re-tested all three tiers with `AUTH_PROVIDER=legacy` |
| Qualix membership check works, and returns real profile data | Real call against the live gateway using a real Keycloak token — got back `first_name`, `customer_name`, and `permissions` matching a captured real browser login |
