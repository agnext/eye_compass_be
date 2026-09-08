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

**Enable the backend to survive a reboot** (not yet done on this dev unit —
still manual today, see `todos.md` item 7 for the remaining "does the whole
device come up clean after a reboot" question):

```bash
sudo cp eye_compass_be/eye-compass-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eye-compass-backend.service
```

**Running the backend directly, without systemd** (useful for watching
tracebacks live, e.g. while chasing the PyCUDA crash in
`9 - post_remediation_session_log.md` §4):

```bash
cd /home/nvidia/eye_compass_new/eye_compass_be
/home/nvidia/.virtualenvs/eye_compass/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

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
