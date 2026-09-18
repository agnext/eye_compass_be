# 3 — Keycloak configuration

**What this file is:** what had to be set up inside Keycloak, why each piece was
needed, and how the non-obvious parts were discovered. Read this before changing
anything in Keycloak.

---

## The settings in use

| Setting | Value |
|---|---|
| Keycloak URL | `https://dev.perfeqtfoods.com/keycloak` |
| Realm | `CentralIAM` |
| Client | `qualix-backend` |
| Login method | Direct Access Grants (username/password sent straight to Keycloak) |

## Why `qualix-backend` and not something else

The realm has two existing clients. Neither was ideal, and they fail for
**opposite** reasons:

| Client | Accepts username/password directly? | Has what the gateway needs? |
|---|---|---|
| `qualix-frontend` (used by the Qualix website) | **No** — browser redirect only | Yes |
| `qualix-backend` | **Yes** | No — had to be added |

Eye Compass has its own login form, so it must send the password directly.
`qualix-frontend` refuses that outright (*"Client not allowed for direct access
grants"*), so `qualix-backend` was the only usable option — and the missing
pieces were added to it.

**`qualix-backend` is the client Eye Compass uses, decided for good, not a
placeholder.** Creating a separate client was considered and dropped: it would
also require an Assurance-side change the team wants to avoid, and reusing this
client is simpler with no real downside for how the device is operated.

---

## The client scopes that had to be added

A "client scope" in Keycloak is a **reusable set of rules** that decides what
information gets written into a login token. Three were attached to
`qualix-backend`, all as **Optional**:

| Scope | What it adds | Why it is needed |
|---|---|---|
| `audience-for-gateway` | Puts `gateway-client` in the token | Without it the Assurance gateway refuses the token |
| `add-asu-be-audience` | Puts `asu-be` in the token | Same — the gateway checks for both |
| `qualix-application-permissions` | Adds the list of allowed actions (`scan_visio`, `scan_history`, …) | Qualix itself checks these when a scan is posted |

Plus one built-in scope that was already available:

| Scope | Why |
|---|---|
| `offline_access` | Gives a long-lived login. Without it the session dies after 1 day — see below |

### Why "Optional" and not "Default"

`qualix-backend` is a **shared** client — other systems use it too.

- **Default** would change the tokens of *everyone* using that client.
- **Optional** changes nothing for anyone else; the scope is only applied when an
  application specifically asks for it.

Eye Compass asks for them by name (see `KEYCLOAK_SCOPE` in
`6 - environment_variables.md`). Because they are Optional, **that list must match
Keycloak exactly** — asking for a scope that is not attached makes the whole
login fail with `invalid_scope`.

---

## The 1-day trap (important)

The realm's session settings are:

| Setting | Value |
|---|---|
| SSO Session Idle | 10 hours |
| **SSO Session Max** | **1 day** |
| Offline Session Idle | 30 days |
| Offline Session Max Limited | Disabled (no limit) |

A **normal** login is killed after **1 day**, no matter what. That would have
silently capped every session at one day, no matter what the device intended — the
kind of problem that works perfectly in a short test and fails a week later in
the field.

The fix is requesting the `offline_access` scope, which produces a different kind
of long-lived login governed by the bottom two rows: no absolute limit, as long
as it is used at least once every 30 days. The daily check does exactly that.

This is verifiable — the saved refresh token decodes to `typ: Offline` with no
expiry date. If that ever reads `Refresh` instead of `Offline`, the 1-day cap is
back.

---

## How the audience scopes were found (worth knowing)

These two scopes were very hard to find, and the reason matters if anyone has to
debug something similar.

The Qualix website's token worked; ours did not. Comparing the two tokens showed
the difference — ours was missing `gateway-client` and `asu-be` — but **not where
they came from**. Checked and ruled out:

- The client's own dedicated mappers — empty.
- `qualix-application-permissions` — attaching it added the permissions list but
  **no audience**, disproving the obvious assumption that both lived together.
- Requesting different scope combinations — no effect.

The answer appeared in the **refresh token**. An access token's `scope` field
listed only:

```
openid email profile qualix-application-permissions
```

but the refresh token listed the full internal set:

```
openid email acr audience-for-gateway profile qualix-application-permissions
web-origins roles add-asu-be-audience basic
```

Both audience scopes have *"Include in token scope"* switched **off**, so they do
their work invisibly and never appear in the access token. That is a normal
Keycloak pattern, but it means **comparing access tokens will never reveal them**.

**Lesson:** when a token seems to be missing something and the access token gives
no clue, decode the refresh token — it shows the complete list.

---

## Confirmed facts about the realm

- **MFA/2FA is not enforced.** This matters because the direct username/password
  method cannot handle a two-factor prompt. If MFA is ever turned on for these
  accounts, this login method stops working entirely and the redirect-based login
  page becomes mandatory (see `10 - open_items.md`).
- **Any Keycloak user can log in.** There is no restriction by role or customer.
  This matches how the system has always behaved — the legacy app accepted any
  valid Qualix login too — so it is not a new looseness introduced here.
- **Operators type their full username** (e.g. `cgi.op3@agnext.in`). Unlike the
  old Qualix login, nothing is appended automatically.
