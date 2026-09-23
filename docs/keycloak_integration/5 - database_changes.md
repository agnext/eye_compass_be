# 5 — Database changes

**What this file is:** every database change made by this work. Read this before
deploying to a device, and before any database backup/restore work.

---

## Summary

| Change | Detail |
|---|---|
| **New table** | `sessions` |
| Tables removed | **None** |
| Columns added to existing tables | `creds.operator_id`, `result.sync_error`, `result.client_request_id` (plus several on `sessions` itself as the work progressed) |
| Indexes added to existing tables | `uq_batch_details_batch_number`, `uq_result_client_request_id` — both `UNIQUE` |
| Data migration needed | **None** — every added column is nullable or defaulted, and existing rows are valid as they stand |
| Manual SQL needed | **None** — applied automatically at startup, see below |

No existing column was altered or dropped; the changes are one new table, some
added columns, and two unique indexes.

### How added columns and indexes reach a device that already has data

There is no Alembic in this project, and SQLAlchemy's `create_all()` only
creates missing *tables* — it will not add a column to a table that already
exists, nor add a constraint to one. Two idempotent startup steps in
`app/main.py` cover that gap, both safe to run on every boot:

- `_add_missing_columns()` — compares each table's live columns against a
  small explicit map and issues `ALTER TABLE ... ADD COLUMN` for anything
  absent.
- `_add_missing_constraints()` — issues `CREATE UNIQUE INDEX IF NOT EXISTS`
  for the two unique indexes above.

Two details worth knowing before a deploy:

- `result.client_request_id` is added **without** a `DEFAULT`. Existing rows
  must stay `NULL`, because a unique index permits any number of `NULL`s but
  would refuse to build if every pre-existing row shared a `''` default.
- `_add_missing_constraints()` will fail loudly if `batch_details` already
  contains duplicate batch numbers. That is the intended outcome — they have
  to be resolved by hand before uniqueness can be enforced.

---

## The new `sessions` table

Defined in `app/models/schema.py`, used by `app/core/security.py`.

| Column | Type | What it holds |
|---|---|---|
| `token` | String(64), primary key | The random session token given to the browser after login |
| `username` | String(150), indexed | Who is logged in |
| `mode` | String(30) | How they logged in: `pending`, `online`, `offline`, or `offline-device`. A row can move from `pending` to `online` or `offline` — see below |
| `created_at` | DateTime | When they logged in |
| `last_verified_at` | DateTime, indexed | When Keycloak last confirmed this account is still good. Never decides validity; its one job is the once-a-day rule — see below |
| `refresh_token` | Text | Keycloak token used by the daily check to confirm the account |
| `needs_relogin` | Boolean | Set when Keycloak says the account was disabled (see below) |
| `first_name` | String(150) | From Keycloak, for display |
| `email` | String(255) | From Keycloak |
| `roles` | JSON | The user's roles from Keycloak |
| `keycloak_user_id` | String(64) | Keycloak's own subject id for this account |
| `password_credential_created_at` | DateTime, nullable | When the Keycloak-side password credential was created — used to spot a password change |
| `relogin_suggested` | Boolean | Softer than `needs_relogin`: ask again at Home, do not cut the operator off |
| `operator_id` | String(64) | Qualix's own `user.user_id`, sent on the scan datagram so Qualix knows who ran the batch — see `11 - user_flows.md` |

### How it gets created

Automatically. The backend runs `Base.metadata.create_all(...)` at startup, which
creates any missing table. **No migration tool, no manual SQL, no downtime.**
Just deploy the code and restart.

> **One exception, already handled.** `create_all` creates missing *tables*, but
> never adds missing *columns* to a table that already exists. The one device
> already running this feature needed a manual migration to reshape the table:
>
> ```sql
> ALTER TABLE sessions ADD COLUMN last_verified_at TIMESTAMP;
> UPDATE sessions SET last_verified_at = GREATEST(created_at, expires_at - INTERVAL '45 days');
> CREATE INDEX ix_sessions_last_verified_at ON sessions (last_verified_at);
> ALTER TABLE sessions DROP COLUMN expires_at;
> ```
>
> Any device deployed from now on creates the table with the correct shape from
> scratch and needs none of this. It is recorded only in case an old database
> backup is ever restored.

### How it gets cleaned up

Rows are removed when a session genuinely ends:

- The operator logs out — the row is deleted.
- The operator logs in again after being asked to — the old row is replaced.

**There is no age-based clean-up, because nothing expires by age.** The table
only ever holds one row per logged-in operator on a single-operator kiosk, so it
does not grow.

---

## Why this table exists (this was a real bug)

Sessions used to be stored **in the backend's memory** — a plain dictionary
inside the running process.

That meant **every restart of the backend wiped every session**. A device reboot,
a `systemctl restart`, or a crash would log every operator out immediately, even
though the system was supposed to keep them logged in indefinitely.

So the long login was never real — the true lifetime was "until the next
restart". Nobody had noticed because it looks identical to a normal session
expiry from the operator's side.

This had to be fixed before anything else, because:

- A login that survives restarts is a core requirement, and it simply was not
  working.
- The revalidation check would have been pointless — it could only ever see
  sessions created since the last restart.
- Extending a session's life is meaningless if a reboot erases it anyway.

### How a session is judged valid

**One field decides it.** There is no expiry date stored anywhere, and no
setting anywhere that controls how long a login lasts:

```
valid = not needs_relogin
```

In words: *a session lasts indefinitely. The only thing that ever ends it is
Keycloak explicitly saying the account is no longer good.* Time never ends a
session — not 45 days, not a year, not any number, because there is no number.

`last_verified_at` is stamped by the revalidation check, and it answers "when
did we last actually hear from Keycloak about this account?" when someone is
reading logs or a database row.

It has exactly one functional role, and it is **not** expiry. Because each
session is verified with Keycloak at most once per day,
`SessionStore.online_sessions_with_refresh_token(skip_if_verified_after=...)`
uses it to exclude any session already confirmed since midnight UTC today.
Keeping that state in the column rather than in memory means the once-a-day rule
survives a backend restart. Nothing about session *validity* branches on it: an
old or missing `last_verified_at` never ends a session.

### Why there is no time limit at all

Signing an operator out because *time had passed*, with no other evidence
anything is wrong, punishes them for the network rather than for anything about
their account — on a device that is supposed to work for weeks without
internet, that is the wrong trade-off. So no field or setting in this system
measures "how long has it been" for the purpose of ending a session; the only
question ever asked is "did Keycloak say no."

A tempting counter-argument is that a time limit acts as a fail-safe: if the
revalidation check ever silently broke, a revoked account might go unnoticed forever.
That is a real concern, but a limit is the wrong answer to it — it would fire on
*every* offline device, not just broken ones. The correct response to a broken
revalidation check is to fix it, which the logs make visible (see
`9 - troubleshooting.md`). A device with no internet is not a device with a
compromised account.

### The `mode` lifecycle — `pending` settles into `online` or `offline`

`mode` is not fixed for the life of a row.

A login accepted from the password saved on this device creates the row as
`mode="pending"`. That is the only honest label at that instant: the operator
is signed in, but nothing has been verified, no refresh token exists, and the
device has not yet tried to reach Keycloak — so it is not yet known whether
this device even has a network. The background check settles it:

| Keycloak's answer | `mode` becomes |
|---|---|
| Confirmed | `online` (via `promote_to_online`) |
| Unreachable | `offline` (via `mark_offline`) |
| Rejected | stays `pending`, but flagged `needs_relogin` |

This three-way distinction is what lets the Home screen say *"Signed in offline
— results will sync later"* only when it is actually true, instead of guessing
while the check is still in flight and risking telling an online operator they
are offline.

On confirmation, `SessionStore.promote_to_online(token, claims)` rewrites the
row in place:

| Field | After promotion |
|---|---|
| `mode` | `online` |
| `refresh_token` | The Keycloak refresh token from the confirming login |
| `first_name`, `email`, `roles` | Filled in from the Keycloak claims and the Qualix profile |
| `last_verified_at` | Stamped with the moment Keycloak confirmed |
| `needs_relogin` | Cleared |

After that the row is indistinguishable from one created by a normal online
login, and the once-a-day check picks it up like any other. `offline-device`
never promotes — the fixed device credential is not a Keycloak account, so there
is nothing to confirm.

If Keycloak instead **rejects** those credentials, the row is not promoted: it
is flagged `needs_relogin` and keeps working until the operator reaches the Home
screen (see below), and the `creds` row is deleted.

If Keycloak cannot be reached, nothing about the account's standing changes —
the row is only settled to `mode="offline"` so the operator can be told they
are working offline. A session left at `pending` (because the background check
never completed at all) is treated exactly like an offline one by the
never-verified sweep below, so it cannot sit unchecked forever.

### Sessions Keycloak can never check on its own

An operator who signed in **offline** has no Keycloak refresh token, so the
daily check has nothing to ask with — that session can never be confirmed
silently, however long the device stays up.

`SessionStore.unverifiable_sessions()` finds exactly these. When any exist, the
revalidation worker probes Keycloak with `keycloak_service.is_reachable()` (a plain GET
on the realm's `.well-known/openid-configuration`):

- **Keycloak reachable** — those sessions are flagged for re-login. Asking the
  operator to type their password once is the *only* way such a session can ever
  be checked at all.
- **Keycloak unreachable** — they are left completely alone. The operator keeps
  working.

The probe is only made when such a session actually exists, so an ordinary
online session costs nothing extra.

---

## The `needs_relogin` column — why a session is flagged, not deleted

The flag is set in three situations: the daily check finds that Keycloak has
disabled someone's account; the daily check finds an offline-signed-in session
it can only ever confirm by asking for a password; or the background check that
follows a login from the saved password is told by Keycloak that those
credentials are no longer valid. In all three the session is **flagged, not
removed**. The token keeps working
normally.

This is deliberate. The operator might be in the middle of a batch scan or a data
collection run, and cutting their session off instantly would **lose that work**.

Instead the flag is acted on only when they next reach the **Home screen** —
somewhere they can only get to *between* tasks. It is checked there and nowhere
else, so it can never interrupt a batch scan or a data collection run, and no
scan data is lost.

At that point the Home screen shows a **popup** ("Please sign in again", with a
single **Sign in again** button) rather than abruptly throwing them back to the
login screen. The popup deliberately has no dismiss option — the session does
have to end — but it is never a dead end, because signing in works offline too:
if the device has no internet, the saved password on the device gets them
straight back in.

---

## Existing tables — how they changed

**`creds`** — stores the hashed password used for the fast path and for offline
login. Still a single row keyed on the username, and still indifferent to which
system verified the password; it has since gained `keycloak_user_id`,
`refresh_token` and `operator_id` columns.

`operator_id` deserves a note, because how it is *maintained* is the point of
it. It is refreshed **only on a fresh online login**, where Qualix's
`/user/keycloak-profile` response supplies it. On an offline or cached login it
is carried forward unchanged rather than overwritten, so a device that has been
off the network for weeks still posts scans under the correct operator. A fresh
value that differs from the stored one is logged at WARNING before being saved,
so a reassignment is visible rather than silent.

Three behaviours worth knowing about:

- It holds **one row only**. Each successful online login replaces it. So the
  fast path and offline login work for the **most recent operator to log in
  online on that device**, not for everyone who has ever used it.
- **Logging out does not clear it.** That is intentional — it is what allows an
  operator to log back in offline after logging out.
- **An explicit Keycloak rejection does delete it.** When the background check
  following a login from this row is told the credentials are no longer valid —
  password changed, account disabled, or no longer a registered Qualix user —
  the row is removed. That stops a stale password from working offline forever,
  and it also forces the next login through Keycloak, since with no row there is
  nothing for the fast path to match. No flag or extra column was needed: the
  absence of the row **is** the rule. Keycloak merely being **unreachable**
  never deletes it — being offline says nothing about the account.

---

## Rolling back

If a device is switched back to `AUTH_PROVIDER=legacy`, the `sessions` table
stays and keeps being used — session storage is shared by both login methods and
is an improvement either way. **There is nothing to undo in the database.**
