# 14. Production Deployment Runbook — Setting Up a Device From Start to End

Every production device is currently running the legacy PyQt5 app under
`/home/nvidia/eye_compass`. This procedure installs the new stack **alongside
it** at `/home/nvidia/eye_compass_new` — the same layout as the dev unit. The
legacy folder is left exactly as it is; you stop it but do not move or rename
it. The new backend reads inference code and model files from it via
`EYE_COMPASS_SRC=/home/nvidia/eye_compass`.

Follow it top to bottom; each step says how to confirm it worked before you
move on.

This is an installation procedure, not a design document. For *why* the pieces
are split the way they are, read `5 - infrastructure_and_deployment.md` first —
it explains why the backend is not in Docker. For the kiosk browser's
reasoning, `10 - pwa_and_deployment_rollout.md`.

> **The one difference from the dev unit:** on dev, the frontend container runs
> Vite's **dev server** (`npm run dev`, port 5173). Production serves a **built
> bundle behind Nginx on port 80** instead. Step 7 covers that; it is the only
> part of this stack that genuinely differs between dev and prod. The backend
> and database are identical in both.

---

## 0. What you are installing

Three independent pieces. They do not start as one command, and they do not
depend on each other's startup order.

| Piece | Runs as | Where it listens | Survives reboot via |
|---|---|---|---|
| **PostgreSQL** | Docker container (`db`) | host port 5432 | `restart: always` + Docker enabled at boot |
| **Backend** (FastAPI) | native systemd service | host port 8000 | `systemctl enable` |
| **Frontend** (Nginx + built bundle) | Docker container (`frontend`) | host port 80 | `restart: always` |

All containers use `network_mode: host` — this Jetson's kernel has no
`iptable_raw.ko`, so Docker's bridge networking fails outright. Consequence:
every service binds a real host port directly; there is no port mapping to
adjust.

---

## 1. Stop the legacy app

The legacy PyQt5 app runs from `/home/nvidia/eye_compass`. Leave that folder
exactly where it is — the new backend reads `run_inference.py`, `config.INI`,
`FeatureFile_new.ini`, and the model files from it via
`EYE_COMPASS_SRC=/home/nvidia/eye_compass`. Only the running process needs to
stop.

**Stop the legacy app.** How it runs varies by device — it may be a systemd
service, a cron-launched script, or a manually started process. Find and stop
whatever is running:

```bash
# Check what's running from the legacy tree
ps aux | grep eye_compass | grep -v grep

# If it's a systemd service (common name varies per device):
sudo systemctl stop eye-compass.service      # or whatever the legacy unit is called
sudo systemctl disable eye-compass.service   # prevent it from starting on reboot
```

**Back up the legacy SQLite database** — the migration script in step 9 reads
it, and you want a known-good copy before anything else runs:

```bash
mkdir -p /home/nvidia/eye_compass_new/db_backups
cp /home/nvidia/eye_compass/eye_compass.db \
   /home/nvidia/eye_compass_new/db_backups/eye_compass.db.$(date +%Y%m%d_%H%M%S).bak
```

**Rollback** is straightforward at any point — the legacy folder was never
touched, so it is enough to stop the new stack and restart whatever the legacy
app's own launch mechanism was:

```bash
sudo systemctl stop eye-compass-backend.service
sudo docker compose -f /home/nvidia/eye_compass_new/docker-compose.yml stop
# restart legacy
```

---

## 2. Before you start — collect these

Do not begin until you have all of it. Half of a deployment is worse than none.

**Values you must have in hand:**

- `DEVICE_ID` — exactly 2 characters (A–Z / 0–9), **unique to this physical
  device**. Batch creation refuses to run until it is set. Two devices sharing
  one `DEVICE_ID` will eventually produce identical batch numbers.
- `DEVICE_CODE` — the `device_serial_no` sent to Qualix on every scan.
- `WAREHOUSE_NAME` — sent alongside it.
- `SYNC_SERVICE_USERNAME` / `SYNC_SERVICE_PASSWORD` — the fixed account every
  outbound scan POST authenticates as. Never an operator's own account.
- `EMERGENCY_LOGIN_USERNAME` / `EMERGENCY_LOGIN_PASSWORD` — the break-glass
  login that works with no network and no cached password. Give it its own
  credentials; do not reuse a real operator's.
- `AWS_IDENTITY_POOL_ID` and the S3 bucket/folder, if S3 upload is on.
- A new PostgreSQL password (see step 4 — do not ship the dev default).
- If this device uses Keycloak: `KEYCLOAK_CLIENT_SECRET` and
  `ASSURANCE_API_URL`. Otherwise leave `AUTH_PROVIDER=legacy`.

**Files that must already be on the device:**

- The legacy tree at `/home/nvidia/eye_compass` — still the source of
  `run_inference.py`, `config.INI`, `FeatureFile_new.ini`, and the model files.
  This stack does not replace it; it reads from it. The folder stays at its
  original path; only the legacy process was stopped in step 1.
- `models/` containing the `.optimized` / `.engine` TensorRT files for every
  commodity this device will scan. These are data, not code, and are not in the
  repository.
- Google service-account JSON, if `SHEETS_ENABLED=true`.

---

## 3. Host prerequisites

Confirm each of these on the device before installing anything.

**If Docker is not installed (e.g. fresh Jetson), install Docker CE:**
```bash
cd ~
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo systemctl enable --now docker
sudo usermod -aG docker nvidia
```
*(You may need to log out and log back in for the `nvidia` user group change to take effect).*

```bash
# Docker present and enabled at boot
docker --version
docker compose version
systemctl is-enabled docker          # must print: enabled

# The deployment virtualenv has BOTH stacks in one interpreter
/home/nvidia/.virtualenvs/eye_compass/bin/python -c \
  "import tensorrt, pycuda.driver, torch, cv2, numpy; print('ML stack OK')"
/home/nvidia/.virtualenvs/eye_compass/bin/python -c \
  "import fastapi, sqlalchemy, boto3; print('web stack OK')"

# Camera SDK libraries and the serial port
ls /opt/MVS/lib
ls -l /dev/ttyTHS1 /dev/ttyUSB*      # at least one must be the conveyor
```

If the virtualenv does not exist yet, create it **with system site packages**,
or it will not see JetPack's TensorRT/torch/cv2 — which cannot be installed
with pip on aarch64:

```bash
python3 -m venv --system-site-packages /home/nvidia/.virtualenvs/eye_compass
/home/nvidia/.virtualenvs/eye_compass/bin/pip install -r \
  /home/nvidia/eye_compass_new/eye_compass_be/requirements.txt
```

Then re-run both import checks above. Do not continue until they both pass —
every later step assumes this interpreter is complete.

**Troubleshooting ML Stack Failures:**
If the ML stack check fails with `ModuleNotFoundError: No module named 'pycuda'`, run:
```bash
/home/nvidia/.virtualenvs/eye_compass/bin/pip install pycuda
```

If it fails with `ModuleNotFoundError: No module named 'torch'` (or `tensorrt`, `cv2`), this means they weren't installed globally in the system packages, but were instead installed manually in the legacy environment (e.g. `m38`). Since compiling PyTorch for a Jetson takes hours, simply copy them from the old legacy environment directly into the new one:
```bash
cp -r /home/nvidia/.virtualenvs/m38/lib/python3.*/site-packages/torch* /home/nvidia/.virtualenvs/eye_compass/lib/python3.*/site-packages/
# (Repeat for cv2 or tensorrt if needed)
```

---

## 4. Create `docker-compose.yml` (Database & Frontend)

If the `docker-compose.yml` file is not already present on the device, create it manually:

```bash
cd /home/nvidia/eye_compass_new
nano docker-compose.yml
```

Paste the following configuration into the file. **Important:** Change `POSTGRES_PASSWORD` to a real password before saving, and notice that you can comment/uncomment the `frontend` sections depending on whether you are doing a development or production build:

```yaml
services:
  db:
    image: postgres:18-alpine
    restart: always
    network_mode: host
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: eye_compass
      PGDATA: /var/lib/postgresql/data/pgdata
      PGPORT: 5432
    volumes:
      - postgres_data:/var/lib/postgresql/data

  # Uncomment below for bind mount development
  frontend:
    image: node:20-alpine
    working_dir: /app
    restart: always
    network_mode: host
    volumes:
      - ./eye_compass_fe:/app
    command: sh -c "npm install && npm run dev -- --host 0.0.0.0 --port 5173"

  # Uncomment below for production Docker build instead
  # frontend:
  #   build:
  #     context: ./eye_compass_fe
  #   restart: always
  #   network_mode: host

volumes:
  postgres_data:
```

Save and exit. The database is created automatically the first time the `db` container starts against an empty volume. You do **not** run any `CREATE DATABASE` by hand.

Start the containers (this will spin up both the database and the frontend):

```bash
sudo docker compose up -d
sudo docker compose ps                       # Both must show Up
```

Verify the empty database exists and is reachable:

```bash
sudo docker compose exec db psql -U postgres -d eye_compass -c '\dt'
# Expect: "Did not find any relations." — correct at this point. The backend
# has not started yet, so there are no tables. The backend creates them in step 6.
```

---

## 5. Configure the backend (`.env`)

```bash
cd /home/nvidia/eye_compass_new/eye_compass_be
cp .env.example .env
```

`.env.example` documents every setting inline — read it as you go. The values
that **must** change for production are below; everything else can stay at its
documented default on a first install.

```ini
# Real hardware, never synthetic frames. The systemd unit pins this too, so a
# mistake here cannot put a production device into mock mode — but set it right.
USE_MOCK_CAMERA=false

# Must match the password you set in docker-compose.yml in step 4.
DATABASE_URL=postgresql://postgres:<YOUR_PASSWORD>@localhost:5432/eye_compass

# Point at production Qualix, not dev/qa.
QUALIX_API_URL=https://assaying.qualix.ai/

# Unique to this physical device — see step 1.
DEVICE_ID=XX
DEVICE_CODE=...
WAREHOUSE_NAME=...

# Who syncs, and who can get in when nothing else works. Two different accounts.
SYNC_SERVICE_USERNAME=...
SYNC_SERVICE_PASSWORD=...
EMERGENCY_LOGIN_USERNAME=...
EMERGENCY_LOGIN_PASSWORD=...

# S3 upload of crops/frames. Set the pool id or turn the feature off.
S3_ENABLED=true
AWS_IDENTITY_POOL_ID=...

# The production frontend is served on port 80, not Vite's 5173.
CORS_ORIGINS=http://localhost,http://127.0.0.1
```

**If this device is on Keycloak**, also set `AUTH_PROVIDER=keycloak`,
`KEYCLOAK_CLIENT_SECRET`, and `ASSURANCE_API_URL`, and point `KEYCLOAK_URL` at
the **same environment** as `QUALIX_API_URL`. Pointing one at prod and the
other at dev silently changes where scan data lands. See
`../keycloak_integration/8 - switching_a_device.md`.

Lock the file down — it holds live credentials:

```bash
chmod 600 .env
```

---

## 6. Install and start the backend

```bash
cd /home/nvidia/eye_compass_new/eye_compass_be
sudo cp eye-compass-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eye-compass-backend.service
```

The service unit pins `USE_MOCK_CAMERA=false`, the MVS SDK paths, and the
virtualenv interpreter, and has `Restart=always` — so it comes back after a
crash as well as a reboot.

**The schema is created here, automatically**, on first startup: the backend's
lifespan runs `create_all()` plus idempotent column/constraint checks. There is
no Alembic and no migration command to run.

Verify:

```bash
sudo systemctl status eye-compass-backend.service       # active (running)
sudo journalctl -u eye-compass-backend.service -n 100   # clean startup, no tracebacks
curl http://localhost:8000/                             # responds

# The tables now exist:
cd /home/nvidia/eye_compass_new
sudo docker compose exec db psql -U postgres -d eye_compass -c '\dt'
```

In the journal, confirm the startup lines report the auth provider and the
camera you expect. A device that silently came up in mock mode is the single
most common bad install.

---

## 7. Build and serve the frontend (production mode)

This is the step that differs from the dev unit. **Do not ship the Vite dev
server**: it recompiles on file changes, serves unminified assets, and has none
of production's caching behaviour.

`eye_compass_fe/Dockerfile` already builds the bundle and serves it behind
Nginx, and `nginx.conf` already has the SPA fallback (so a refresh on
`/history` does not 404) and the `/api/` + `/ws/` proxy blocks pointing at
`localhost:8000`.

**Replace the `frontend` service in `/home/nvidia/eye_compass_new/docker-compose.yml`**
with the build-based one — the file already carries it, commented out. The
production form:

```yaml
  frontend:
    build:
      context: ./eye_compass_fe
    restart: always
    network_mode: host
```

Delete (or comment out) the `node:20-alpine` dev-server service above it. Both
cannot run at once.

```bash
cd /home/nvidia/eye_compass_new
sudo docker compose build frontend
sudo docker compose up -d frontend
```

Verify — and check specifically that the proxy reaches the backend, which is
what `network_mode: host` makes work:

```bash
curl -I http://localhost/                 # 200, from Nginx
curl http://localhost/api/                # proxied through to the backend
sudo docker compose logs -f frontend      # no startup errors
```

> `nginx.conf` proxies to `localhost:8000` and relies on sharing the host's
> network namespace. If you ever run this container *without*
> `network_mode: host`, that `localhost` becomes the container itself and every
> API call 502s — replace it with the real backend host first.

**Rebuilding after a frontend code change** is now an explicit step; there is
no hot reload in production:

```bash
sudo docker compose build frontend && sudo docker compose up -d frontend
```

---

## 8. Kiosk browser

Full detail and reasoning in `10 - pwa_and_deployment_rollout.md`; the
production-specific part is the **URL**.

The kiosk launch script (`~/.local/bin/eye-compass-kiosk.sh`) waits for the
frontend to respond, then launches Firefox against it. On the dev unit it
targets `http://localhost:5173` — the Vite dev server. **On production it must
target `http://localhost`** (port 80, Nginx). Edit both the wait-for URL and
the launch URL in that script.

Copy onto the device, if not already present:

- `~/.local/opt/firefox/` — Mozilla's standalone Linux-aarch64 tarball.
  Chromium and the apt `firefox` package are both snaps, and snaps cannot run
  on this kernel at all (no AppArmor).
- `~/.local/opt/firefox/distribution/policies.json` — disables the password
  manager, `about:config`, devtools, and the rest. Without it an operator can
  reach `about:logins` from the save-password prompt and get stuck on a browser
  settings page with no title bar and no keyboard.
- `~/.local/opt/firefox-kiosk-profile/` — the dedicated profile.
- `~/.local/bin/eye-compass-kiosk.sh` and
  `~/.config/autostart/eye-compass-kiosk.desktop`.

Also enable GDM auto-login for user `nvidia` in `/etc/gdm3/custom.conf`, or
nothing starts until someone logs in at the console.

Test without rebooting:

```bash
DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority \
  /home/nvidia/.local/bin/eye-compass-kiosk.sh
cat ~/.local/state/eye-compass-kiosk.log
```

`--kiosk` leaves no on-screen close button. To get out: `Alt+F4`, or from a
terminal:
```bash
pkill -f eye-compass-kiosk.sh && pkill -f firefox-kiosk-profile
```
(Killing only Firefox lets the script's retry loop relaunch it three seconds later, so kill both).

---

## 9. Migrate legacy data

The SQLite database was already backed up in step 1. Now migrate it into
PostgreSQL.

**Preview, then run.** The script opens the SQLite file read-only, never
deletes or overwrites anything in PostgreSQL, and runs as a single transaction
— a failure anywhere writes nothing:

```bash
cd /home/nvidia/eye_compass_new/eye_compass_be
/home/nvidia/.virtualenvs/eye_compass/bin/python \
  scripts/migrate_sqlite_to_postgres.py \
  --sqlite /home/nvidia/eye_compass/eye_compass.db --dry-run
```

Read the table it prints. When the counts look right, re-run without
`--dry-run`. It is safe to run twice — the second run adds nothing.

**The images are files, not database rows**, and the script does not touch
them. Copy the legacy output tree across separately; the folder layout
(`output/<commodity>/<variety>/<image_unique_id>/`) is identical:

```bash
rsync -a /home/nvidia/eye_compass/output/ \
         /home/nvidia/eye_compass_new/eye_compass_be/output/
```

Without this, a migrated batch appears in History but its crop images are
missing.

---

## 10. Final verification

Work through all of it before handing the device over.

**Services:**

```bash
sudo systemctl status eye-compass-backend.service   # active (running)
sudo docker compose ps                              # db + frontend both Up
curl -I http://localhost/                           # 200
curl http://localhost:8000/                         # responds
```

**Reboot test — do not skip this one.** It is the only check that proves the
device survives a power cut unattended:

```bash
sudo reboot
```

After it comes back, with nobody touching anything: both containers up, the
backend service active, and the kiosk browser showing the login screen on the
physical display.

**Functional end-to-end**, on the device's own touchscreen:

1. Log in as a real operator.
2. New Batch → Start → put a real sample through → a detection stops the belt →
   classify the object → Resume → Submit → Confirm.
3. The batch appears in History with its crop images.
4. Its sync status reaches delivered — confirm the record arrived in Qualix.

**Offline behaviour**, which is the whole point of this device:

5. Disconnect the network. Log out, log back in as the same operator — the
   cached-password path must accept it and show the offline notice.
6. Run another batch offline. It must complete and save. Reconnect, and confirm
   the retry worker delivers it without anyone intervening.

**Power-cut recovery:**

7. Start a batch, pull power mid-scan, boot back up. The Home screen must offer
   to continue that batch, and continuing it must keep the objects already
   classified.

---

## 11. Day-to-day operations

```bash
# Start everything (containers come back on their own after a reboot)
cd /home/nvidia/eye_compass_new
sudo docker compose up -d
sudo systemctl start eye-compass-backend.service

# Stop
sudo systemctl stop eye-compass-backend.service
sudo docker compose stop          # 'down' also removes the containers

# After a backend code change — uvicorn runs without --reload, so an edited
# file does nothing until this runs
sudo systemctl restart eye-compass-backend.service

# After a frontend code change — production serves a built bundle
sudo docker compose build frontend && sudo docker compose up -d frontend

# Logs
sudo journalctl -u eye-compass-backend.service -f
sudo docker compose logs -f frontend
sudo docker compose logs -f db
cat ~/.local/state/eye-compass-kiosk.log

# The backend also writes a daily file that survives reboots, which the
# journal here does not (no /var/log/journal — journald runs on tmpfs)
ls /home/nvidia/eye_compass_new/eye_compass_be/logs/

# Database
sudo docker compose exec db psql -U postgres -d eye_compass
```

**Back the database up on a schedule.** The named `postgres_data` volume
survives container recreation and reboots, but nothing protects it from disk
failure:

```bash
sudo docker compose exec -T db pg_dump -U postgres eye_compass \
  | gzip > /home/nvidia/eye_compass_new/db_backups/eye_compass_$(date +%Y%m%d).sql.gz
```

---

## 12. If it goes wrong

| Symptom | Cause to check first |
|---|---|
| Backend won't start, DB connection refused | `DATABASE_URL` password does not match `POSTGRES_PASSWORD`; or the `db` container is not up |
| Backend starts but no camera | `USE_MOCK_CAMERA`, `/opt/MVS/lib` present, camera reachable on its GigE address, nothing else holding it (the SDK grants exclusive access to one process only) |
| Frontend loads, every API call 502s | `frontend` container is not on `network_mode: host`, so `nginx.conf`'s `localhost:8000` points at the container |
| Deep links 404 on refresh | Serving the built bundle without `nginx.conf`'s `try_files` fallback |
| Batch creation refuses to run | `DEVICE_ID` is unset |
| Scans complete but never sync | `SYNC_SERVICE_USERNAME` / `_PASSWORD` blank or wrong — the backend logs this explicitly |
| `docker compose up` fails with "Unable to enable DIRECT ACCESS FILTERING" | A service lost its `network_mode: host`; this kernel has no `iptable_raw.ko` |
| Kiosk shows a blank page or connection error | The script still targets `:5173` (the dev server) instead of port 80 |

**Rolling back to the legacy app** is possible at any point — the legacy folder
at `/home/nvidia/eye_compass` was never touched, and nothing in this procedure
modifies it:

```bash
sudo systemctl stop eye-compass-backend.service
sudo systemctl disable eye-compass-backend.service
sudo docker compose -f /home/nvidia/eye_compass_new/docker-compose.yml stop
# restart legacy's own launch mechanism
```

The legacy SQLite file was never written to — the migration script opens it
read-only, and the backup from step 1 is at
`/home/nvidia/eye_compass_new/db_backups/` as well.
