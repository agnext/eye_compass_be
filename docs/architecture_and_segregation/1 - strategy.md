# 1. Strategy — Why and How This Was Segregated

This is the first document in a series. Read them in order (1 → 10) for a
complete picture of the legacy application, why and how it was split into a
frontend and a backend, how its behavior was verified to match the original,
and what is left before a real device can run this instead of the legacy app.

## The one rule that governed every decision

**Segregation was the only intended change.** The legacy PyQt5 desktop
application (`eye_compass`) is a single Python process that does everything:
draws the UI, talks to the camera, runs inference, drives the conveyor,
manages the database, and syncs to the cloud. The goal of this project was
**not** to redesign how the machine behaves — it was to split that one process
into a browser-based frontend (`eye_compass_fe`) talking over HTTP/WebSocket to
a backend (`eye_compass_be`) that owns the hardware and business logic. Every
button, every safety interlock, every retry policy, every quirk of the
original was meant to keep working exactly as before; only *where the code
lives* was supposed to change.

In practice, the first pass at this split did not fully hold to that rule —
see `8 - remediation_log.md` and `9 - post_remediation_session_log.md` for the
places where behavior had drifted from legacy (missing packages, wrong ports,
a completely unimplemented detection loop) and had to be brought back in line
by reading the legacy source line-by-line and porting the actual behavior,
not just the intent.

## The legacy application, in one paragraph

`eye_compass` is a PyQt5 desktop app that runs directly on an NVIDIA Jetson
edge device. It drives a physical Hikvision GigE industrial camera over the
Hikvision MVS SDK, runs YOLOv7-family TensorRT models to detect foreign matter
in a stream of commodity grain moving under the camera on a conveyor belt,
talks to the belt's controller over a plain serial line to start/stop it and
freeze it the instant something is detected, stores results in a local SQLite
database, and syncs completed scans to Qualix (the customer's cloud analytics
platform), Google Sheets, and an AWS S3 bucket. See
`2 - current_codebase_overview.md` for the full tour, and
`2b - dependencies_and_hardware.md` for the complete inventory of everything
this application depends on to run.

## The target architecture

- **Frontend (`eye_compass_fe`)** — a React + Vite single-page app, configured
  as an installable PWA, that renders every screen the operator sees and talks
  to the backend over `fetch`/RTK Query and a WebSocket for the live camera
  feed. It has zero direct hardware access — that is the entire point of the
  split. See `3 - frontend_setup_walkthrough.md`.
- **Backend (`eye_compass_be`)** — a FastAPI application that owns everything
  hardware- and business-logic-related: the camera, the TensorRT inference,
  the serial conveyor protocol, the detection/interlock state machine, the
  database, and the Qualix/Sheets/S3 sync workers. See
  `4 - backend_segregation.md`.
- **Database** — PostgreSQL, replacing the legacy SQLite file, for the
  concurrency and crash-safety reasons explained in
  `5 - infrastructure_and_deployment.md`.
- **Deployment** — a hybrid of Docker (frontend, database) and a native
  systemd service (backend), not "everything in Docker." The reasoning for
  that specific split is the main subject of
  `5 - infrastructure_and_deployment.md`, since it was a deliberate,
  non-obvious choice made for this specific device.

## What was explicitly *not* part of this project

- ~~Authentication was not moved to an external identity provider (Keycloak,
  OIDC, etc.). An earlier planning draft of this document proposed that; it
  was never built. The actual implementation is a simple server-side bearer
  token issued at login (`app/core/security.py`), matching the legacy app's
  own login flow (online-first against Qualix, offline fallback against a
  locally cached credentials table) rather than replacing it with something
  new.~~

  **Reversed after the segregation work — see `enhancements.md`.** Kept above
  rather than deleted, per this doc set's convention of recording reversed
  decisions instead of erasing them. Operator login can now be pointed at this
  organization's Keycloak instance via `AUTH_PROVIDER=keycloak`; the original
  reasoning was sound at the time, but Keycloak turned out to already front
  Qualix org-wide through the Assurance gateway, so this is adopting existing
  infrastructure rather than introducing something new. The bearer-token
  design above is unchanged and still what actually authorizes requests —
  Keycloak only replaces *who verifies the password at login*. `AUTH_PROVIDER`
  defaults to `legacy`, so a device that is not switched over behaves exactly
  as this paragraph originally described.
- No message broker (Redis Pub/Sub, RabbitMQ) was introduced to fan data out
  to other systems. The legacy app's own sync targets — Qualix, Google Sheets,
  S3 — were ported as backend background workers, nothing more.
- The machine's actual physical behavior — what the conveyor, camera, and
  detection pipeline *do* — was not redesigned. Every place this document set
  says "ported," it means the legacy Python was read and the same decisions
  were reproduced in the new codebase, line-by-line where necessary, not
  reinvented from a spec.

## Where to go next

Continue to `2 - current_codebase_overview.md` for a tour of the legacy
codebase, then `2b - dependencies_and_hardware.md` for the full hardware/
software dependency inventory, before reading how the split was actually
carried out.
