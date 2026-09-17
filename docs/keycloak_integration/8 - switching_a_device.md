# Switching a device over (and back)

**What this file is:** the practical steps to move a device from Qualix login to
Keycloak login, what to check afterwards, and how to undo it.

---

## Before you start

Confirm these, because they are the things that actually go wrong:

- [ ] The three client scopes are attached to `qualix-backend` in Keycloak —
      `audience-for-gateway`, `add-asu-be-audience`,
      `qualix-application-permissions` (all **Optional**). See
      `3 - keycloak_configuration.md`.
- [ ] You have the client secret.
- [ ] You know which Qualix environment the gateway points at. The current URL is
      **dev** — make sure that is what you want. **This changes where scan data is
      stored, not just how login works.**
- [ ] The operators who will use this device have Keycloak accounts.

---

## Steps

**1. Edit `eye_compass_be/.env`:**

```bash
AUTH_PROVIDER=keycloak
KEYCLOAK_CLIENT_SECRET=<the secret>
ASSURANCE_API_URL=https://dev.perfeqtfoods.com/api/asu/gateway/assaying-dev/
```

(The other Keycloak settings already have working defaults.)

**2. Restart the backend:**

```bash
sudo systemctl restart eye-compass-backend.service
```

The new database table is created automatically. No SQL to run, no migration.

**3. Watch the startup log:**

```bash
journalctl -u eye-compass-backend.service -f
```

Two lines confirm the switch worked:

```
Auth provider: KEYCLOAK (https://dev.perfeqtfoods.com/keycloak, realm=CentralIAM, client=qualix-backend). Sync via Assurance: ...
Session revalidation worker started (every 6 h).
```

That second line only ever appears in Keycloak mode, so it is a reliable signal.

---

## Checks after switching

**1. Login works and actually used Keycloak.** Log in through the normal screen,
then check the log:

```
Online login accepted for <user> via KEYCLOAK (realm=CentralIAM, client=qualix-backend, roles=[...])
```

The word `KEYCLOAK` and the roles list are the proof — a legacy login says
`QUALIX` and has no roles.

**2. The session survived a restart** (the thing that was broken before):

```bash
sudo systemctl restart eye-compass-backend.service
```

Reload the app. You should **still be logged in**. If you are bounced to the
login screen, sessions are not persisting — stop and investigate.

**3. Commodity list loaded.** Start a new batch and confirm the commodity
dropdown has entries. That data comes through the gateway, so it working proves
the whole sync path works.

**4. Offline login works.** Disconnect the network and log in again with the same
account. You should get in, with a "Signed in offline" message. This only works
if that operator has logged in online on this device at least once.

**5. A scan syncs.** Run a batch through to completion and confirm it reaches
Qualix (History shows it as synced).

---

## Switching back

```bash
AUTH_PROVIDER=legacy
sudo systemctl restart eye-compass-backend.service
```

That is all. The legacy path is untouched by this work and behaves exactly as it
always did.

**Nothing in the database needs undoing.** The `sessions` table is used by both
modes and is an improvement either way.

One side effect: operators will need to log in again after switching, because
their saved password on the device was verified by the other system. Their first
login after the switch must be **online**.

---

## Things that commonly surprise people

**"I switched but nothing changed."** Settings are read once at startup — the
backend must be restarted.

**"The commodity list is different now."** Expected: the gateway points at the
**dev** Qualix environment, which has different data from production. In
particular the surveyor list came back empty in dev, so the Sorter Name dropdown
may have no entries.

**"Someone can't log in offline."** Offline login only works for the operator who
most recently logged in *online* on that specific device. The saved-password
table holds one row and each online login replaces it. This is long-standing
behaviour, not something this work changed.
