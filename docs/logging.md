# Logging

**What this file is:** how the backend's logs are formatted, how to read them,
and how to switch the colour off. Applies to the whole backend, not just one
feature.

---

## Reading the logs (with colour)

```bash
cd eye_compass_be
./scripts/logs.py                      # follow live, coloured
./scripts/logs.py -n 200               # last 200 lines
./scripts/logs.py --since "1 hour ago"
./scripts/logs.py --auth               # only login / session / sync activity
./scripts/logs.py --errors             # only warnings and errors
```

Any other arguments are passed straight through to `journalctl`.

Plain `journalctl` still works and shows the same content, just without colour:

```bash
journalctl -u eye-compass-backend.service -f
```

### Why a script instead of the backend just printing colour

**systemd-journald strips ANSI colour codes from anything written to it.** A
service that colours its own output has that colour removed before `journalctl`
ever sees the message — this was tested directly on this device and confirmed.

So colour has to be applied when the logs are **read**, not written.
`scripts/logs.py` runs `journalctl` and colours the output on the way past.
Nothing is stored differently and no log file is duplicated — it is purely a
viewer.

The backend can still colour its own output when run by hand in a terminal
(uvicorn during development), where nothing strips it.

## The format

```
17:28:57  INFO    app.api.auth: [AUTH] TIER 1 SUCCESS — online login for ...
└ time    └ level └ which part of the code  └ flow tag   └ message
```

Colour is by severity, so a problem stands out without having to read every line:

| Level | Colour | Meaning |
|---|---|---|
| DEBUG | cyan | Detail, normally switched off |
| INFO | green | Normal activity |
| WARNING | yellow | Something to look at — the whole line is coloured |
| ERROR | red | Something failed |
| CRITICAL | white on red | Serious failure |

Timestamps and code locations are dimmed so the message itself stands out.
`[TAG]` markers are shown in magenta.

## Flow tags

Lines belonging to a major flow are tagged, so they can be filtered:

| Tag | Covers |
|---|---|
| `[AUTH]` | Logging in — which system checked the password, which tier succeeded |
| `[SESSION]` | Sessions being created, extended, expired or flagged |
| `[REVALIDATE]` | The once-a-day check that accounts are still valid |
| `[SYNC]` | Getting the token used to send scan results, and the full body of every scan POST to Qualix (headers are never logged — the bearer token is in them) |

Useful filters:

```bash
./scripts/logs.py --auth        # all login / session / sync activity
./scripts/logs.py --errors      # warnings and errors only

# or with plain journalctl
journalctl -u eye-compass-backend.service | grep -E "AUTH|SESSION"
journalctl -u eye-compass-backend.service -p warning
```

Because what is stored is plain text, `grep` and other tools work normally —
there are no escape codes in the journal to interfere with matching.

---

## The `LOG_COLOR` setting

This controls whether the backend colours **its own** output, which only matters
when running it by hand in a terminal. Under systemd it makes no difference,
since journald discards the codes either way.

```bash
LOG_COLOR=auto      # default — colour only when attached to a terminal
LOG_COLOR=never     # never colour
LOG_COLOR=always    # force, even when not a terminal
```

To get colour from a service, use `./scripts/logs.py` instead.

---

## What to expect in a healthy startup

```
Eye Compass API starting up...
Auth provider: KEYCLOAK (https://dev.perfeqtfoods.com/keycloak, realm=CentralIAM, client=qualix-backend). Sync via Assurance: ...
Database tables verified.
[SESSION] 1 session(s) restored from the database — operators stay logged in across restarts.
Unsynced-result retry worker started (every 30 min).
Session revalidation worker started (every 6 h).
Application startup complete.
```

Two of those lines are worth knowing by sight:

- **`Auth provider:`** — states plainly whether this device is using Keycloak or
  the old direct-Qualix login. Without it, a successful login looks identical in
  the logs either way.
- **`[SESSION] N session(s) restored`** — proof that sessions survive restarts.
  Sessions used to be held in memory, so every restart logged everybody out; a
  non-zero count here is the visible confirmation that is fixed. See
  `keycloak_integration/5 - database_changes.md`.

---

## Where the code lives

`app/core/logging_setup.py`, applied once from `app/main.py` at startup. No extra
package is required — it is a small formatter using standard ANSI codes.

For what each individual log line means during login, see
`keycloak_integration/11 - user_flows.md`, which lists the expected lines for
every situation.
