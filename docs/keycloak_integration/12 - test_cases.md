# 12 — Keycloak test cases

**What this file is:** a checklist to run from the UI (and, where noted, from
the database/logs) to confirm the Keycloak integration behaves correctly.
Each case gives the setup, the steps, and what a pass looks like. For what
each log line *means*, see `11 - user_flows.md`; this file is the checklist,
that one is the reference.

Run with:
```bash
journalctl -u eye-compass-backend.service -f | grep -E "AUTH|SESSION|REVALIDATE|SYNC"
```

---

## A. Basic login — all three tiers

1. **Online login, valid Qualix operator.** Log in with real internet and a
   real operator account. *Pass:* lands on Home, no offline banner, `/me`
   returns `first_name`/`customer_name`.
2. **Online login, wrong password.** *Pass:* rejected with a clear "Invalid
   credentials" message, not a generic network error.
3. **Online login, a Keycloak account that is not a Qualix operator.** *Pass:*
   rejected. The password itself is valid on the shared realm — Keycloak says
   yes — but `api/user/keycloak-profile` comes back `USERNR01` and the login
   is refused anyway. See `11 - user_flows.md §1b` for the exact log lines.
4. **Offline login (no internet), previously-used credentials.** *Pass:*
   accepts in offline mode; Home shows the offline banner.
5. **Offline login (no internet), an account never used on this device
   before.** *Pass:* rejected — nothing cached to check against.
6. **The fixed `.env` device credential**, tried both online and offline.
   *Pass:* always works, regardless of connectivity.

## B. Fast-path login (cached credentials)

7. **Log in once online, log out, log in again with the same credentials.**
   *Pass:* near-instant; backend log shows `[AUTH] FAST LOGIN`; session starts
   as `mode: "cached"`.
8. **Watch Home right after a fast-path login.** *Pass:* session starts
   `pending`, settles to `online` (Keycloak reachable and confirms) or
   `offline` (unreachable) within a few seconds. No wrong banner ever flashes
   in between.

## C. Tier-2 refresh-token carry-forward

9. **Log in online once, log out, then force a tier-2 login** (block the
   network, or simply be offline when logging back in). *Pass:* the new
   `mode="offline"` session is **not** empty-handed — check the database: its
   `refresh_token`/`keycloak_user_id` should be populated, carried forward
   from the `creds` table rather than blank.
10. **With that tier-2 session active, trigger a revalidation pass.** *Pass:*
    it is checked exactly like an online session (refresh + Admin API), not
    dumped into the "never verified, ask to sign in" bucket. No
    `offline_flagged` outcome for this session; a normal verified/flagged/
    suggested one instead.

## D. Session revalidation — hard flag (account genuinely bad)

11. **Disable an operator's account in Keycloak while they are logged in**,
    then trigger revalidation. *Pass:* Home shows the **blocking modal**
    ("Please sign in again", no dismiss), not the banner. Confirm a batch
    scan or data collection already in progress is not interrupted — the
    modal only ever appears at Home.
12. **Change an operator's password in Keycloak, without logging in again
    afterward**, then trigger revalidation. *Pass:* the Admin API check
    catches the mismatch between the stored baseline and Keycloak's current
    password-credential timestamp; flagged hard, same blocking modal. Log
    line: `...'s password was changed (Admin API password credential
    timestamp no longer matches what was recorded at login) — flagging for
    re-login.`
13. **After a hard flag, try an offline login with the old password.** *Pass:*
    refused — the cached credential is cleared the moment a session is hard
    flagged.

## E. Session revalidation — soft flag (token aged out, account fine)

14. **A session whose refresh token has gone idle-expired, while the account
    itself is still enabled and its password unchanged.** *Pass:* Home shows
    the **dismissible orange banner** with the warning icon, not the modal —
    every other control on Home stays clickable underneath it. (In practice
    this needs either a genuinely idle token — 30+ real days — or calling
    `session_worker._handle_refresh_rejection` directly against a real
    session to stand in for the refresh rejection while letting the real
    Admin API check run; see `9 - troubleshooting.md` if this needs
    reproducing without a 30-day wait.)
15. **Click "Sign in" on that banner.** *Pass:* routes to `/login`. Signing in
    with real internet clears the banner and issues a fresh session. If the
    device is still genuinely offline at that moment, login falls through to
    the cached-hash tier instead — never a dead end.
16. **Admin API temporarily unreachable or unpermitted** (e.g. the
    `view-users` role removed) while a refresh is rejected. *Pass:* falls back
    to the **hard** flag, not a silently softened banner — an account that
    could not be confirmed must never be waved through as fine.

## F. Tier-3 device credential — the one case that stays hard

17. **A session signed in purely via the fixed `.env` device credential**
    (`mode="offline-device"`), once Keycloak becomes reachable. *Pass:* still
    gets the **hard** flag/modal, never the soft banner — this account was
    never confirmed by Keycloak even once, unlike a tier-2 session which
    always implies a prior real online confirmation.

## G. Baseline seeding at login

18. **Log in online, then immediately check the session's
    `password_credential_created_at` in the database.** *Pass:* already
    populated within a second or two of login (seeded in the background right
    after the login response), not left blank until the worker's first pass.
19. **Change the password once, right after a fresh login, then trigger
    revalidation.** *Pass:* detected on the very first real check — no need
    for a throwaway first pass to "establish" the baseline before a second
    pass can catch anything.

## H. Sync-triggered revalidation

20. **Complete a scan while offline, then bring the device online.** *Pass:*
    syncs within 30 minutes (retry worker) or immediately via manual resync.
21. **A successful sync triggers a revalidation pass.** *Pass:* visible in the
    logs; respects the once-a-day cap — does not re-check a session the
    6-hour timer already confirmed today.

## I. Login form UX

22. **An incomplete email** (no `@`/domain). *Pass:* rejected client-side
    instantly, no network call.
23. **Log in successfully once, reload, focus the email field.** *Pass:* the
    address appears as a suggestion; typing a prefix filters the list; the
    `×` on a row forgets that address.
24. **The browser's own save-password prompt.** *Pass:* never appears — the
    Firefox kiosk policy disables it device-wide (see
    `10 - pwa_and_deployment_rollout.md`).

---

## Known test-account gotcha

The membership check (Case 3) means a non-Qualix Keycloak account used for
testing sessions D/E/F **stops being able to log in at all** once that check
is active — it will be refused before a session ever gets created. Use a real
Qualix operator account for any test that needs a fresh login partway through
(re-testing baseline seeding, tier-2 carry-forward, etc.), or a disposable
Qualix-registered test account if one exists.
