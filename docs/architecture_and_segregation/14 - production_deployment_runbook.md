# 14. Production Deployment Runbook

Set up the new eye_compass stack on a Jetson device (JetPack 5 or 6).
Follow top to bottom; each step says how to verify before moving on.

For architecture rationale see `5 - infrastructure_and_deployment.md`.
For kiosk design see `10 - pwa_and_deployment_rollout.md`.
For JetPack 5 / Python 3.8 specifics see `15 - jetpack5_python38_compatibility.md`.

---

## 0. What you are installing

| Piece | Runs as | Port | Survives reboot via |
|---|---|---|---|
| **PostgreSQL** | Docker container (`db`) | 5432 | `restart: always` |
| **Backend** (FastAPI) | systemd service | 8000 | `systemctl enable` |
| **Frontend** (Nginx) | Docker container (`frontend`) | 80 | `restart: always` |

All containers use `network_mode: host` — the Jetson kernel has no
`iptable_raw.ko`, so Docker bridge networking does not work.

---

## 1. Stop the legacy app

The legacy PyQt5 app at `/home/nvidia/eye_compass` must be stopped but
**not removed** — the new backend reads `run_inference.py`, `config.INI`,
`FeatureFile_new.ini`, and model files from it.

```bash
# Find and stop whatever is running
ps aux | grep eye_compass | grep -v grep
systemctl list-units --type=service | grep -i eye

# If managed by systemd:
sudo systemctl stop <SERVICE_NAME>
sudo systemctl disable <SERVICE_NAME>

# If started manually (SIGKILL — PyQt5 ignores SIGTERM):
kill -KILL $(pgrep -f "eye_compass/main.py" | head -1)

# If it is restarting automatically, find its systemd service name:
systemctl list-units --type=service | grep -i eye
# (It might be called eye-compass.service, run_app.service, etc.)

# Stop and disable it so it stays dead:
sudo systemctl stop <SERVICE_NAME>
sudo systemctl disable <SERVICE_NAME>
```

**Legacy cron jobs** (`crontab -l`) — leave them running during migration.
Disable them once the new stack's S3 upload is verified.

**Back up the legacy SQLite database:**

```bash
mkdir -p /home/nvidia/eye_compass_new/db_backups
cp /home/nvidia/eye_compass/eye_compass.db \
   /home/nvidia/eye_compass_new/db_backups/eye_compass.db.$(date +%Y%m%d_%H%M%S).bak
```

**Rollback** is straightforward at any point — the legacy folder was never
touched. If you need to revert to the legacy app, turn the new stack off and
turn the legacy stack back on:

1. **Stop the new stack:**
```bash
sudo systemctl stop eye-compass-backend.service
sudo systemctl disable eye-compass-backend.service
sudo docker compose -f /home/nvidia/eye_compass_new/docker-compose.yml down
pkill -f eye-compass-kiosk.sh && pkill -f firefox-kiosk-profile
mv ~/.config/autostart/eye-compass-kiosk.desktop ~/.config/autostart/eye-compass-kiosk.desktop.disabled
```

2. **Start the legacy app back up:**
```bash
sudo systemctl enable <SERVICE_NAME>   # the legacy unit from above
sudo systemctl start <SERVICE_NAME>
```

If the legacy app was not managed by systemd, start it manually:
```bash
cd /home/nvidia/eye_compass
DISPLAY=:0 \
XAUTHORITY=/home/nvidia/.Xauthority \
PYTHONPATH=/usr/lib/python3.8/dist-packages \
MVCAM_COMMON_RUNENV=/opt/MVS/lib \
  nohup /home/nvidia/.virtualenvs/m38/bin/python main.py \
    >> /tmp/legacy_stdout.log 2>&1 &
```

> **Note on the legacy command:** It must run from `/home/nvidia/eye_compass`. It uses the `m38` venv (where the legacy ML stack lives). It injects `PYTHONPATH` for system TensorRT, sets `MVCAM_COMMON_RUNENV` for the camera SDK (needed over SSH), and sets `DISPLAY=:0` to send the Qt GUI to the device's screen.

---

## 2. Collect these values first

Do not begin until you have all of them.

- `DEVICE_ID` — exactly 2 characters (A–Z / 0–9), unique per device
- `DEVICE_CODE` — the `device_serial_no` sent to Qualix
- The conveyor serial port — confirm it with the probe in section 12
  ("Belt does not start"); do not copy it from another device
- `WAREHOUSE_NAME`
- `SYNC_SERVICE_USERNAME` / `SYNC_SERVICE_PASSWORD`
- `EMERGENCY_LOGIN_USERNAME` / `EMERGENCY_LOGIN_PASSWORD`
- `AWS_IDENTITY_POOL_ID` and S3 bucket/folder (if S3 is on)
- A new PostgreSQL password (do not ship the dev default)
- If Keycloak: `KEYCLOAK_CLIENT_SECRET` and `ASSURANCE_API_URL`

**Files that must already be on the device:**

- Legacy tree at `/home/nvidia/eye_compass` (source of inference code + models)
- `models/` with `.optimized` / `.engine` TensorRT files for each commodity
- Google service-account JSON (if `SHEETS_ENABLED=true`)

---

## 3. Host prerequisites

**Install Docker if not present:**
```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo systemctl enable --now docker
sudo usermod -aG docker nvidia   # log out/in for group to take effect
```

**Verify:**
```bash
docker --version && docker compose version
systemctl is-enabled docker   # enabled
```

**Create the deployment virtualenv** (do not reuse the legacy `m38` venv):

If the virtualenv does not exist yet, you must create it. **Do not reuse the legacy environment (e.g. `m38`)**. We create a brand new, isolated environment (`eye_compass`) so we do not pollute the legacy app with web dependencies, ensuring safe rollbacks.

Create it **with system site packages**, or it will not see JetPack's TensorRT/torch/cv2 — which cannot be installed with pip on aarch64:

```bash
python3 -m venv --system-site-packages /home/nvidia/.virtualenvs/eye_compass
/home/nvidia/.virtualenvs/eye_compass/bin/pip install -r \
  /home/nvidia/eye_compass_new/eye_compass_be/requirements.txt
```

**Verify both stacks are visible:**

```bash
/home/nvidia/.virtualenvs/eye_compass/bin/python -c \
  "import tensorrt, pycuda.driver, torch, cv2, numpy; print('ML stack OK')"
/home/nvidia/.virtualenvs/eye_compass/bin/python -c \
  "import fastapi, sqlalchemy, boto3; print('web stack OK')"
```

If `torch`/`tensorrt`/`cv2` are missing — they live in the legacy `m38` venv.
Add a `.pth` file so `eye_compass` can see them:

```bash
echo '/home/nvidia/.virtualenvs/m38/lib/python3.8/site-packages' \
  > /home/nvidia/.virtualenvs/eye_compass/lib/python3.8/site-packages/zz_m38_ml_stack.pth
```

If `pycuda` is missing: `pip install pycuda`.

**JetPack 5 only (Python 3.8) — extra packages:**

```bash
/home/nvidia/.virtualenvs/eye_compass/bin/pip install eval_type_backport tqdm
```

See `15 - jetpack5_python38_compatibility.md` for what these fix and the
two code-level fixes already in the repo (`asyncio.to_thread` polyfill,
`from __future__ import annotations`).

**Camera SDK:**
```bash
ls /opt/MVS/lib                          # must exist
ls -l /dev/ttyTHS* /dev/ttyUSB* 2>&1     # candidates only — which one is the
                                         # conveyor is settled in section 12
```

---

## 4. Create `docker-compose.yml`

```bash
cd /home/nvidia/eye_compass_new
nano docker-compose.yml
```

**Change `POSTGRES_PASSWORD` before saving.** Comment/uncomment the frontend
section depending on dev vs prod:

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

  # Development — bind-mount + Vite dev server
  frontend:
    image: node:20-alpine
    working_dir: /app
    restart: always
    network_mode: host
    volumes:
      - ./eye_compass_fe:/app
    command: sh -c "npm install && npm run dev -- --host 0.0.0.0 --port 5173"

  # Production — built bundle behind Nginx (uncomment this, comment above)
  # frontend:
  #   build:
  #     context: ./eye_compass_fe
  #   restart: always
  #   network_mode: host

volumes:
  postgres_data:
```

```bash
sudo docker compose up -d
sudo docker compose ps   # both Up
```

---

## 5. Configure `.env`

```bash
cd /home/nvidia/eye_compass_new/eye_compass_be
cp .env.example .env
```

Key production values:

```ini
USE_MOCK_CAMERA=false
DATABASE_URL=postgresql://postgres:<PASSWORD>@localhost:5432/eye_compass
QUALIX_API_URL=https://assaying.qualix.ai/
DEVICE_ID=XX
DEVICE_CODE=...
WAREHOUSE_NAME=...
SYNC_SERVICE_USERNAME=...
SYNC_SERVICE_PASSWORD=...
EMERGENCY_LOGIN_USERNAME=...
EMERGENCY_LOGIN_PASSWORD=...
S3_ENABLED=true
AWS_IDENTITY_POOL_ID=...
CORS_ORIGINS=http://localhost,http://127.0.0.1
EYE_COMPASS_SERIAL_PORT=/dev/ttyTHS0
```

`EYE_COMPASS_SERIAL_PORT` is device-specific: `/dev/ttyTHS0` on the prod
JetPack 5 unit, a `/dev/ttyUSB*` on units with a USB-serial adapter. Set it
from the probe in section 12, not from another device's `.env`.

If Keycloak: also set `AUTH_PROVIDER=keycloak`, `KEYCLOAK_CLIENT_SECRET`,
`ASSURANCE_API_URL`.

If the Postgres password contains `@`, URL-encode it as `%40` in
`DATABASE_URL`.

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

The backend creates all database tables on first startup automatically.

**Verify:**
```bash
sudo systemctl status eye-compass-backend.service   # active (running)
sudo journalctl -u eye-compass-backend.service -n 50
curl http://localhost:8000/
cd /home/nvidia/eye_compass_new
sudo docker compose exec db psql -U postgres -d eye_compass -c '\dt'
```

Check the journal for the correct auth provider and `USE_MOCK_CAMERA=false`.

---

## 7. Build the production frontend

Replace the `frontend` service in `docker-compose.yml` with:

```yaml
  frontend:
    build:
      context: ./eye_compass_fe
    restart: always
    network_mode: host
```

```bash
cd /home/nvidia/eye_compass_new
sudo docker compose build frontend
sudo docker compose up -d frontend
```

**Verify:**
```bash
curl -I http://localhost/             # 200 from Nginx
curl http://localhost/api/            # proxied to backend
```

---

## 8. Kiosk browser

Copy onto the device (if not already present):

- `~/.local/bin/eye-compass-kiosk.sh` — kiosk launcher with retry loop
- `~/.config/autostart/eye-compass-kiosk.desktop` — GNOME autostart entry
- `~/.local/opt/firefox-kiosk-profile/` — dedicated profile

The kiosk script URLs must point at `http://localhost` (port 80), not `:5173`.

**Check whether the standalone Firefox tarball is needed:**
```bash
readlink -f /usr/bin/firefox
snap list | grep firefox
```
If `/usr/bin/firefox` is a real binary (not a snap), the standalone tarball at
`~/.local/opt/firefox/` is not needed.

**Enable GDM auto-login** in `/etc/gdm3/custom.conf`:
```ini
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=nvidia
```

**Test without rebooting:**
```bash
DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority \
  /home/nvidia/.local/bin/eye-compass-kiosk.sh
```

Use `/run/user/1000/gdm/Xauthority` (the live GDM session), not
`~/.Xauthority`.

To exit kiosk mode: `pkill -f eye-compass-kiosk.sh && pkill -f firefox-kiosk-profile`

---

## 9. Migrate legacy data

Migrate from a **copy** of the legacy SQLite file, never the live one. The
script opens its source read-only, but working from a locked copy means a
mistake cannot reach the file the legacy app needs for a rollback.

```bash
# 1. Copy the legacy DB and lock it read-only
TS=$(date +%Y%m%d_%H%M%S)
SRC=/home/nvidia/eye_compass_new/db_backups/migration_source_${TS}.db
mkdir -p /home/nvidia/eye_compass_new/db_backups
cp -p /home/nvidia/eye_compass/eye_compass.db "$SRC"
chmod 444 "$SRC"

# 2. Confirm the copy is byte-identical
md5sum /home/nvidia/eye_compass/eye_compass.db "$SRC"
```

```bash
cd /home/nvidia/eye_compass_new/eye_compass_be

# 3. Dry run — reports exactly what a real run would add, then rolls back
/home/nvidia/.virtualenvs/eye_compass/bin/python \
  scripts/migrate_sqlite_to_postgres.py --sqlite "$SRC" --dry-run

# 4. Real run, once the counts look right
/home/nvidia/.virtualenvs/eye_compass/bin/python \
  scripts/migrate_sqlite_to_postgres.py --sqlite "$SRC"
```

The whole migration is one transaction — on any error nothing is written. It
is also idempotent: a second run adds 0 rows.

**Config and credential tables are copied only when the target is empty.** If
the backend has already synced config from Qualix and an operator has already
signed in, those rows stay as they are and only `result` is migrated. That is
the expected outcome, not a partial failure.

**Verify afterwards** — the totals must match the legacy file, and the legacy
file itself must be unchanged:

```bash
curl -s "http://localhost:8000/api/history/?limit=1&days=0" | head -c 200
md5sum /home/nvidia/eye_compass/eye_compass.db   # same as step 2
```

**Do not copy the legacy `output/` tree** while the legacy S3 cron jobs are
still running (`crontab -l`). Those crons upload each batch folder and then
delete it, so copied images would be uploaded a second time and then removed
from the new tree. Migrated batches therefore show in History with no crop
images; the API reports `on_device: false` for them, which is correct.

---

## 10. Final verification

**Services:**
```bash
sudo systemctl status eye-compass-backend.service
sudo docker compose ps
curl -I http://localhost/
curl http://localhost:8000/
```

**Reboot test — do not skip.** After `sudo reboot`, with nobody touching
anything: containers up, backend active, kiosk showing login on the display.

**End-to-end on the touchscreen:**
1. Log in → New Batch → scan a real sample → detect → classify → Submit
2. Batch appears in History with crop images
3. Sync status reaches delivered; confirm in Qualix

**Offline test:**
4. Disconnect network → log out → log back in (cached password)
5. Run a batch offline → reconnect → retry worker delivers it

---

## 11. Day-to-day operations

```bash
# Start everything
sudo docker compose -f /home/nvidia/eye_compass_new/docker-compose.yml up -d
sudo systemctl start eye-compass-backend.service

# Stop
sudo systemctl stop eye-compass-backend.service
sudo docker compose stop

# Restart backend after code change
sudo systemctl restart eye-compass-backend.service

# Rebuild frontend after code change
sudo docker compose build frontend && sudo docker compose up -d frontend

# Logs
sudo journalctl -u eye-compass-backend.service -f
sudo docker compose logs -f frontend
cat ~/.local/state/eye-compass-kiosk.log
ls /home/nvidia/eye_compass_new/eye_compass_be/logs/

# Database shell
sudo docker compose exec db psql -U postgres -d eye_compass

# Database backup
sudo docker compose exec -T db pg_dump -U postgres eye_compass \
  | gzip > /home/nvidia/eye_compass_new/db_backups/eye_compass_$(date +%Y%m%d).sql.gz
```

---

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| Backend won't start, DB connection refused | `DATABASE_URL` password ≠ `POSTGRES_PASSWORD`; or `db` container not up |
| Backend starts but no camera | `USE_MOCK_CAMERA` value; `/opt/MVS/lib` present; camera on GigE; no other process holding it |
| Every API call 502s | `frontend` container missing `network_mode: host` |
| Deep links 404 on refresh | Nginx `try_files` fallback missing |
| Batch creation refuses | `DEVICE_ID` unset |
| Start fails, `502`, "belt did not respond" | Wrong serial port — see **Belt does not start** below |
| Scans never sync | `SYNC_SERVICE_USERNAME`/`_PASSWORD` blank or wrong |
| `docker compose up` fails, "DIRECT ACCESS FILTERING" | A service lost `network_mode: host` |
| Kiosk blank page | Script still targets `:5173` instead of port 80 |
| Kiosk Firefox dies, `Fatal IO error 11` | Legacy app also running on `:0` — stop it first |
| Kiosk desktop but no browser | Kiosk script died; check `~/.local/state/eye-compass-kiosk.log` |

### Belt does not start

**Symptom.** Pressing Start on the scan screen shows *"The belt did not
respond to the start command"*, and the backend log shows this, three times,
followed by a fail-safe stop:

```
Attempt 1: sending 'machine_start' to /dev/ttyTHS1
Unexpected acknowledgment '' (wanted 'machine_started')
No acknowledgment ... after 3 retries — sending fail-safe all_stop
```

An **empty** acknowledgment (`''`) with the port opening cleanly means the
backend is writing to a serial port nothing is listening on. The usual cause
is `EYE_COMPASS_SERIAL_PORT` naming the wrong port. `/dev/ttyTHS0` and
`/dev/ttyTHS1` are both real Tegra UARTs that open without error whether or
not the controller is wired to them, so nothing fails loudly. Check this
before suspecting the hardware.

**Fix.**

1. Stop the backend, because two processes can open the same port and steal
   each other's bytes, which makes the probe unreliable:

   ```bash
   echo nvidia | sudo -S systemctl stop eye-compass-backend.service
   ```

2. Probe every candidate. `all_stop` is safe — it only stops the belt. The
   port that answers `all_stoped` is the conveyor:

   ```bash
   for p in /dev/ttyTHS0 /dev/ttyTHS1 /dev/ttyUSB0 /dev/ttyACM0; do
     [ -e "$p" ] || continue
     printf '%s -> ' "$p"
     /home/nvidia/.virtualenvs/eye_compass/bin/python -c 'import sys,serial; s=serial.Serial(sys.argv[1],9600,timeout=1); s.write(b"all_stop\n"); print(repr(s.readline().decode(errors="replace").strip()))' "$p"
   done
   ```

   Expect one line to print `'all_stoped'` and the rest `''`.

3. Put the answering port in `.env`, then start the backend:

   ```bash
   cd /home/nvidia/eye_compass_new/eye_compass_be
   sed -i 's|^EYE_COMPASS_SERIAL_PORT=.*|EYE_COMPASS_SERIAL_PORT=/dev/ttyTHS0|' .env   # use the port you found
   echo nvidia | sudo -S systemctl start eye-compass-backend.service
   ```

4. Confirm in the log — the startup `all_stop` must now be acknowledged:

   ```bash
   journalctl -u eye-compass-backend.service --since "1 min ago" --no-pager | grep conveyor_service
   # want: Acknowledgment received: all_stoped
   ```

**If no port answers.** The software is not the problem. Check the controller
board has power and the serial cable is seated, then power-cycle the board
and run the probe again. On a unit with a USB-serial adapter, also check
`lsusb` lists it and `ls /dev/ttyUSB*` shows a device.

**Known values.** Prod JetPack 5 unit: `/dev/ttyTHS0` (legacy hardcodes this
at `main.py:2426`). Baud is 9600 on every unit seen so far.

---

## 13. Rollback to legacy

The legacy folder was never touched. To revert:

```bash
# Stop the new stack + kiosk
sudo systemctl stop eye-compass-backend.service
sudo systemctl disable eye-compass-backend.service
sudo docker compose -f /home/nvidia/eye_compass_new/docker-compose.yml down
pkill -f eye-compass-kiosk.sh && pkill -f firefox-kiosk-profile
mv ~/.config/autostart/eye-compass-kiosk.desktop ~/.config/autostart/eye-compass-kiosk.desktop.disabled
```

Then start legacy (systemd or manual). If manual, run `cd` **separately**
from the launch command:

```bash
cd /home/nvidia/eye_compass
```

```bash
DISPLAY=:0 \
XAUTHORITY=/home/nvidia/.Xauthority \
PYTHONPATH=/usr/lib/python3.8/dist-packages \
MVCAM_COMMON_RUNENV=/opt/MVS/lib \
  nohup /home/nvidia/.virtualenvs/m38/bin/python main.py \
    >> /tmp/legacy_stdout.log 2>&1 &
```

To return to the new stack later: rename the kiosk autostart entry back,
re-enable the backend service, bring containers up.
