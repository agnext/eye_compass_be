# 1 — Overview

**What this folder is:** everything about moving operator login from Qualix to
Keycloak. Written to be readable by someone who was not part of the work.

---

## What changed, in one sentence

Operators used to log in by having their username and password checked directly
by Qualix. Now those credentials are checked by **Keycloak** instead, and scan
results are sent to Qualix through a gateway called **Assurance**.

## What did NOT change

- **The login screen looks exactly the same.** The operator types a username
  and password into the same form as before. There is no new login page and no
  redirect anywhere.
- **Offline working is unchanged.** A device with no internet still lets the
  operator log in and run scans, exactly as before.
- **Everything else in the app** — scanning, the conveyor, reclassifying,
  history, S3 uploads — is untouched.

## Why it was done

The organisation already uses Keycloak as its central login system, and Keycloak
is already connected to Qualix for the Qualix web application. So this is
adopting something that already exists, rather than introducing a new system.

## The one thing that is switchable

A single setting, `AUTH_PROVIDER`, decides which system checks passwords:

| Value | Meaning |
|---|---|
| `legacy` | Password checked by Qualix, exactly as before this work |
| `keycloak` | Password checked by Keycloak, syncing goes via Assurance |

Both paths are fully working. A device is switched by changing this one value
and restarting the backend. Nothing else has to change, and switching back is
just as easy. This is deliberate: it means the change can be rolled out one
device at a time, and reversed instantly if something goes wrong.

---

## The pieces that were built

| Piece | What it does |
|---|---|
| `app/services/keycloak_service.py` | Talks to Keycloak — logs the operator in, re-confirms sessions, gets the token used for syncing |
| `app/services/session_worker.py` | Wakes every few hours; verifies with Keycloak once a day that logged-in operators still have valid accounts |
| `sessions` database table | **New table.** Stores who is logged in. See `5 - database_changes.md` |
| Changes to `app/api/auth.py` | Chooses Keycloak or Qualix based on `AUTH_PROVIDER` |
| Changes to `app/services/sync_service.py` | Sends scan results through the Assurance gateway |
| Change to `Home.jsx` (frontend) | Signs an operator out if their account was disabled — but only at a safe moment |

## A real problem this work fixed along the way

Sessions used to be held **in the backend's memory**. That meant every restart
of the backend — including every device reboot — silently logged everybody out,
even though the system was supposed to keep them logged in.

Sessions are now stored in the database, so they survive restarts and the promise
of staying logged in is finally true. This was not part of the original request;
it was found while building the rest, and the Keycloak work depended on fixing
it.

---

## Where to read next

| File | Read it for |
|---|---|
| `2 - how_login_works.md` | What happens when an operator logs in, in every situation |
| `3 - keycloak_configuration.md` | What had to be set up inside Keycloak, and why |
| `4 - assurance_gateway.md` | How scan results reach Qualix now |
| `5 - database_changes.md` | The new database table |
| `6 - environment_variables.md` | Every setting and what it does |
| `7 - sessions_and_expiry.md` | How staying logged in works, and when it ends |
| `8 - switching_a_device.md` | Step-by-step to switch a device over |
| `9 - troubleshooting.md` | What error messages mean |
| `10 - open_items.md` | What is still left to do |
