# 7. Deployment & QA Checklist

The architecture is built (React + FastAPI + PostgreSQL, hybrid deployment —
see `5 - infrastructure_and_deployment.md`), and the gaps found in the
September audits have been closed in software (`8 - remediation_log.md`,
`9 - post_remediation_session_log.md`). This document tracks what has and has
not been verified **on the physical machine** — the master list of remaining
work lives in `todos.md`; this page gives the status of the items that touch
hardware specifically.

## What has actually been verified on real hardware (this session)

- **Camera**: a genuine Hikvision GigE camera (`MV-CS023-10GC`, reachable at
  `169.254.143.87`) is physically present on this dev unit, and the backend
  successfully initializes it, negotiates the GigE packet size, loads the
  calibration feature file, and grabs real frames end-to-end through
  `/ws/camera/stream`.
- **Conveyor**: the serial protocol over `/dev/ttyTHS1` has been exercised
  against real hardware — sometimes it acknowledges commands correctly
  (`machine_started`/`all_stoped` returned within tens of milliseconds),
  sometimes nothing answers at all. This is consistent with there not being a
  dedicated belt-controller adapter reliably wired to this specific dev unit
  at all times, not a software defect — the retry-then-fail-safe behavior
  itself has been confirmed correct either way.
- **A process-crashing threading bug** in the camera/inference pipeline (a
  CUDA context pushed on one thread and never popped, causing a hard
  `Aborted (core dumped)` at shutdown) was found and fixed by funneling all
  camera/inference work through one dedicated thread — see `9 -
  post_remediation_session_log.md`.

## What is still open (hardware-dependent)

- **TensorRT engine loading and detection quality** — not yet verified
  per-commodity; see `todos.md`'s thorough-testing item.
- **The Qualix POST reaching a real Qualix endpoint successfully** — the
  request is built and sent correctly by the backend, but an actual
  successful round trip with the real Qualix service has not been confirmed
  in this session; see `todos.md`.
- **The `.optimized` model files for `stem_rice`, `toor`, `masoor_dal`, and
  `chitra_rajma`** are absent from `models/` on this device (a pre-existing
  gap in the legacy tree, not introduced here) — `stem_rice` is the fallback
  model, so this should be resolved before the device runs an unmapped
  commodity.
- **The XAI feature's underlying model file** (`v6_best.pt`) is missing
  device-wide — see `todos.md`.
- **Kiosk/PWA install and boot-time autostart** — the PWA manifest exists
  (`vite.config.js`) but has never been tested as an actually-installed app on
  this or any device, and nothing yet configures the device to launch it
  automatically on power-up. See `10 - pwa_and_deployment_rollout.md` and
  `todos.md`.
- **A real end-to-end run**: Login → New Batch → Start → a detection stops
  the belt → operator classifies the object → Resume → Submit → confirm →
  Qualix sync, run start to finish on physical hardware with a real sample.
  Individual pieces of this have been exercised; the full chain back-to-back
  has not.
- **Migrating an actual production device onto this stack** — see
  `todos.md`'s item on this; no rollout procedure exists yet.

For the full, correctly-ordered list of everything outstanding — not just the
hardware-touching items above — see `todos.md`.
