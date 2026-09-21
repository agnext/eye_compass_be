# 5. Infrastructure & Deployment — Why a Hybrid, Not "Everything in Docker"

This document exists to answer one specific question directly: **why does the
frontend and database run in Docker while the backend runs natively on the
host?** This was a deliberate choice, not an inconsistency, and it is worth
understanding before touching `docker-compose.yml` or the backend's systemd
unit.

## The constraint that decided it

As detailed in `2b - dependencies_and_hardware.md`, the backend process needs,
simultaneously, in one Python interpreter:

- The Hikvision MVS SDK's Python bindings, which need real shared libraries
  (`MVCAM_COMMON_RUNENV=/opt/MVS/lib`) and **exclusive** access to a physical
  GigE camera device.
- TensorRT, pycuda, and torch — all provided by NVIDIA's JetPack for this
  exact device and kernel, not installable via `pip` on aarch64, and not
  bundled in any generic Docker base image.
- A real serial port (`/dev/ttyTHS1`) to the conveyor controller.

Putting the backend inside a container means either passing through GPU
devices, character devices, and vendor SDK libraries with exact host/container
version matching (fragile, and this project's own Docker networking already
hit a real wall on this device — see below), or building a custom aarch64
image that bundles JetPack's own CUDA/TensorRT stack, which JetPack does not
make easy to relocate into a container in the first place. Both paths add
real risk of dropped frames, failed camera initialization, or a broken
GPU/inference stack, for a component whose whole job is exactly the low-level
hardware access Docker is trying to abstract away.

The frontend and the database have no such constraint — they are a plain
Node/Vite process and a plain PostgreSQL instance, both of which run
identically in or out of a container.

## The actual split

### Containerized (`docker-compose.yml`)
- **`db`** — `postgres:18-alpine`, with a named volume (`postgres_data`) so
  scan/config data survives container recreation and device reboots.
- **`frontend`** — currently a `node:20-alpine` container bind-mounting the
  `eye_compass_fe` source and running `npm install && npm run dev` directly
  (a live Vite dev server, not a production build) — this is the development
  configuration used throughout this project so far. A production path also
  exists (`Dockerfile`, `nginx.conf` in `eye_compass_fe/`) for serving a real
  built bundle behind Nginx, but it has not yet been the one actually run —
  see `todos.md`.
- **Both services run with `network_mode: host`**, not the usual bridge +
  `ports:` mapping. This was itself forced by the hardware: this Jetson's
  kernel (`5.15.148-tegra`) was built without the `iptable_raw.ko` module,
  which Docker's default bridge networking needs to set up its isolation
  rules — `docker compose up` failed outright with "Unable to enable DIRECT
  ACCESS FILTERING" until host networking was used instead. A consequence:
  containers bind directly to their own default ports (`db` → 5432,
  `frontend`/Vite → 5173), not through any port mapping/translation.

### Native (systemd)
- The FastAPI backend (`eye_compass_be`) runs directly on the host OS's own
  Python (via a dedicated virtualenv, `/home/nvidia/.virtualenvs/eye_compass`,
  which has both the web stack and the JetPack-provided ML stack available —
  see `2b - dependencies_and_hardware.md`), managed by a systemd unit
  (`eye-compass-backend.service`).
- It has direct, unmediated access to the GigE camera, `/dev/ttyTHS1`, and the
  GPU/TensorRT stack — no device passthrough, no version-matching a container
  image's CUDA against the host's, no SDK-inside-a-container fragility.
- It reaches the Dockerized Postgres over `localhost:5432` — since both the
  container and the native process share the host's network namespace
  (`network_mode: host`), this "just works" without any extra Docker network
  configuration.
- It reaches the Dockerized frontend's dev server the same way in reverse:
  Vite's own dev-server proxy (`vite.config.js`) forwards `/api` and `/ws`
  requests to `localhost:8000`, so the browser only ever needs to reach the
  one frontend origin — see `9 - post_remediation_session_log.md` for why that
  same-origin design specifically was chosen (a real, previously-encountered
  bug where a separately-tunneled second port silently swallowed POST
  requests during remote development).

## Configuration (`.env`)

Configuration moved out of legacy's `config.INI` into a `.env` file in the
backend root, with a deliberate precedence order (`app/core/config.py`):
**real process environment → `.env` → legacy `config.INI` (if still present
on the device) → hardcoded legacy defaults.** This lets the systemd unit pin
safety-critical settings (like `USE_MOCK_CAMERA=false`) so a stale developer
`.env` can never silently put a real device into mock mode — an actual bug
found and fixed during remediation (`load_dotenv(override=True)` used to let
exactly that happen; see `8 - remediation_log.md` §8).

Camera tuning values (`CAMERA_EXPOSURE_TIME`, `MVS_SDK_PATH`, etc.) are left
commented out in `.env.example` on purpose: the code already falls back to the
correct legacy defaults if they're absent, and they're there purely as a
convenience for a developer who needs to override them on a different device
without touching Python source.

## What this means for installing on a new/prod device

Bringing up a device from scratch means three independent things, not one
`docker compose up`:
1. `sudo docker compose up -d` (from the folder containing the root
   `docker-compose.yml`) for the database and frontend.
2. The backend's systemd service enabled and started
   (`eye-compass-backend.service`), pointing at the correct virtualenv with
   both stacks installed.
3. The device's browser configured to actually display the frontend on boot.

See `10 - pwa_and_deployment_rollout.md` for step 3 and what "installing this
as a PWA" concretely means, and `todos.md` for the still-open work of
formalizing how an already-running prod device gets its existing legacy
install replaced by this stack.

## Debug mode vs. production mode

**These are the same three services either way** (backend via systemd/venv,
frontend+db via Docker) — "debug" here just means running the backend
manually in a foreground terminal instead of through systemd, so tracebacks
and print/log output are visible live instead of only in `journalctl`. There
is no separate build/config for "debug backend" vs "production backend" —
it's the identical `uvicorn app.main:app` command either way.

**Debug mode** (developing/diagnosing — local dev or Jetson debugging):

Local development (with hot reload):
```bash
python -m uvicorn app.main:app --reload --port 8000
```

Jetson / Remote device debug run:
```bash
cd /home/nvidia/eye_compass_new/eye_compass_be
/home/nvidia/.virtualenvs/eye_compass/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Stop the systemd-managed instance first (`sudo systemctl stop
eye-compass-backend.service`) or this fails with "address already in use" —
both bind the same port 8000.

**Production mode** (unattended device, boot-to-running with no terminal):
```bash
sudo systemctl enable --now eye-compass-backend.service   # one-time
sudo systemctl start eye-compass-backend.service           # every subsequent boot, automatic
```
**Already done on this dev unit** — enabled, started, and verified working
(`journalctl -u eye-compass-backend` showed a clean startup identical to the
manual run, `curl http://localhost:8000/` responded correctly). See
`9 - post_remediation_session_log.md` / `todos.md` item 7 for that
verification.

The frontend has the same debug/production distinction, but only the debug
side has actually been run: `docker-compose.yml`'s `frontend` service runs
Vite's own **dev server** (`npm run dev`, live-reloading, unminified) — this
is what every screen in this project has been tested against, including the
kiosk setup below. A production build (`eye_compass_fe/Dockerfile` +
`nginx.conf`, already present but not yet wired into `docker-compose.yml` or
tried) is still open work — see `todos.md`.

## The exact commands used on this dev unit

**Everyday start** (both containers plus the backend):

```bash
# from /home/nvidia/eye_compass_new
sudo docker compose up -d          # starts db + frontend (network_mode: host)
sudo systemctl start eye-compass-backend.service
```

**Everyday stop:**

```bash
sudo systemctl stop eye-compass-backend.service
sudo docker compose down           # or: docker compose stop, to keep the containers
```

**Restart after a backend code change** (`uvicorn` runs without `--reload`,
so an edited file has no effect until the service is restarted):

```bash
sudo systemctl restart eye-compass-backend.service
```

**The backend surviving a reboot is already set up** on this dev unit —
`eye-compass-backend.service` is installed at `/etc/systemd/system/` and
`enable`d (`Restart=always` besides, so it also comes back after a crash, not
just a reboot). The commands that did this, for reference/a fresh device:

```bash
sudo cp eye_compass_be/eye-compass-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eye-compass-backend.service
```

**Kiosk browser** (the actual on-screen app window — see
`10 - pwa_and_deployment_rollout.md` for the full detail on why this is a
standalone Firefox, not the system's Chromium/Firefox):

```bash
# Launch it manually (e.g. to test without rebooting) — from a plain
# terminal (not the graphical autostart path) DISPLAY/XAUTHORITY aren't set
# by default, so the launch fails with "no DISPLAY environment variable
# specified" unless both are passed explicitly, pointed at the actual
# logged-in graphical session (nvidia's is display :0):
DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority /home/nvidia/.local/bin/eye-compass-kiosk.sh

# See whether it found the backend/frontend ready, and when:
cat ~/.local/state/eye-compass-kiosk.log

# Close it — the script now launches with --kiosk (full-screen, no title
# bar/close button by request), so there is no on-screen way to close the
# window; use Alt+F4, or from any terminal. The launch script relaunches
# Firefox automatically if it closes or crashes (see below), so killing just
# the browser process only closes it for ~3 seconds — stop the launch
# script's own process too if you actually want it to stay closed:
pkill -f eye-compass-kiosk.sh
pkill -f "firefox --profile /home/nvidia/.local/opt/firefox-kiosk-profile"
```

**The browser relaunches itself if closed or crashed.** `eye-compass-kiosk.sh`
runs Firefox inside a `while true` loop (`sleep 3` between attempts) rather
than a single launch, so the device recovers on its own from a crash or an
operator somehow closing the window, instead of being left on the bare GNOME
desktop with no keyboard to get back in with. See `10 -
pwa_and_deployment_rollout.md`'s Kiosk browser section for the full reasoning
and the Firefox `policies.json` lockdown that goes with it.
At graphical login it starts on its own — no command needed, and no
DISPLAY/XAUTHORITY to set manually either (the autostart entry already runs
inside that graphical session) — via
`~/.config/autostart/eye-compass-kiosk.desktop` (standard XDG autostart),
since GDM auto-login for user `nvidia` is already enabled.

The script briefly went through a non-`--kiosk` windowed/maximized variant
(`userChrome.css` hiding just the toolbars, `xulstore.json` forcing
maximized, keeping the native title bar/minimize/close buttons) after
`--kiosk`'s missing close button was flagged as wrong — then reverted back
to `--kiosk` on a later, explicit request for genuine full-screen. The
`userChrome.css`/`xulstore.json` files are still in the profile and harmless
either way; only the `--kiosk` flag in the launch script actually decides
which behavior is active. To go back to the windowed/maximized variant,
remove `--kiosk` from `~/.local/bin/eye-compass-kiosk.sh`'s launch command.

**Tailing logs:**

```bash
sudo docker compose logs -f frontend       # frontend container, live
sudo docker compose logs -f db             # postgres container, live
sudo journalctl -u eye-compass-backend.service -f   # backend, live
sudo journalctl -u eye-compass-backend.service -n 200   # backend, last 200 lines
```

**Checking what's actually up:**

```bash
sudo docker compose ps                     # frontend/db container state
sudo systemctl status eye-compass-backend.service
```

**Connecting to Postgres and looking at its tables** (credentials are the
`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` values from
`docker-compose.yml` above — `postgres` / `password` / `eye_compass` on this
dev unit):

```bash
sudo docker compose exec db psql -U postgres -d eye_compass
```

Then, inside the `psql` prompt:

```sql
\dt                          -- list all tables (the seven ported from legacy)
\d results                   -- describe one table's columns
SELECT * FROM results ORDER BY id DESC LIMIT 5;   -- most recent scan results
\q                            -- quit
```

Since the `db` container binds directly to the host's port 5432
(`network_mode: host`), the same database is also reachable with any local
Postgres client without going through `docker exec` at all, e.g.
`psql -h localhost -U postgres -d eye_compass`.

The `eye-compass-backend.service` unit shown above is what
`ExecStart=/home/nvidia/.virtualenvs/eye_compass/bin/python -m uvicorn
app.main:app --host 0.0.0.0 --port 8000` actually runs in production once
installed — the manual command above is the same line, just invoked directly
instead of through systemd.
