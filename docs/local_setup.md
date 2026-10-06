# Local Setup — Windows / Mac Laptop

Two ways to run this on a laptop (no Jetson, no camera):

- **Backend only** — fastest. Just the FastAPI server + a Postgres you bring
  yourself. Use this if you're working on backend code and don't need the UI.
- **Full stack** — backend + frontend + Postgres, with the frontend and DB in
  Docker. Use this if you want to click through the UI end-to-end.

Both paths share the backend steps below.

---

## A. Backend only

### 1. Prerequisites

- Python 3.10+
- A running Postgres (local install, Docker, or a shared dev DB). Note its
  host, port, user, password and database name — you'll put them in `.env`.

### 2. Install

```bash
cd eye_compass_be
python -m venv venv

# Activate the venv:
# Windows PowerShell:
venv\Scripts\Activate.ps1
# Windows cmd:
venv\Scripts\activate.bat
# Mac / Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure `.env`

```powershell
# PowerShell
Copy-Item .env.example .env
```

```bash
# cmd.exe
copy .env.example .env
```

```bash
# Mac / Linux
cp .env.example .env
```

Edit `.env` and set:

| Variable | Value | Why |
| --- | --- | --- |
| `USE_MOCK_CAMERA` | `true` | Skips TensorRT and the MVS camera SDK — the backend uses synthetic frames so it starts on a machine with no Jetson hardware. |
| `DATABASE_URL` | `postgresql://<user>:<pass>@<host>:<port>/<dbname>` | Point this at whatever Postgres you're using. |
| `DEVICE_ID` | any 2 chars, e.g. `XY` | Batch creation errors out if this is blank. |
| `SYNC_SERVICE_USERNAME` / `SYNC_SERVICE_PASSWORD` | any dummy values | Sync to Qualix will fail (no real credentials), but the rest of the app runs fine. Expect auth-failure lines in the log — ignore them. |

Everything else in `.env.example` has a sensible default for laptop use.

### 4. Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Tables are created automatically on first start (`Base.metadata.create_all` in
`app/main.py`) — no migration step. The API is at **http://localhost:8000**.

---

## B. Full stack (backend + frontend + Postgres)

The frontend and Postgres run in Docker; the backend still runs natively on
the host so you can restart and debug it without rebuilding an image.

### 1. Prerequisites

- Everything from section A
- Docker Desktop (Windows or Mac)
- The `docker-compose.windows.yml` file **placed at the project root** (the
  folder that contains `eye_compass_be/` and `eye_compass_fe/`). If it isn't
  there already, copy or create it there using the contents below.

<details>
<summary><code>docker-compose.windows.yml</code> contents</summary>

```yaml
# Standalone compose file for local Windows/Mac dev.
#
# docker-compose.yml uses network_mode: host, which is Jetson-specific and
# only works there because that's native Linux Docker. Docker Desktop on
# Windows/Mac doesn't expose host-mode container ports to the host the same
# way, and here port 5432 is also already taken by a native Windows Postgres
# service. This file is NOT an override merged with docker-compose.yml
# (network_mode can't be unset via merge) — run it on its own:
# `docker compose -f docker-compose.windows.yml up -d`.
services:
  db:
    image: postgres:18-alpine
    restart: always
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: eye_compass
      PGDATA: /var/lib/postgresql/data/pgdata
    ports:
      - "5434:5432"
    volumes:
      - postgres_data_windows:/var/lib/postgresql/data

  frontend:
    image: node:20-alpine
    working_dir: /app
    restart: always
    ports:
      - "5173:5173"
    environment:
      # Backend runs natively on the Windows host, not in this compose file.
      # host.docker.internal is Docker Desktop's DNS name for the host machine
      # from inside a bridge-networked container.
      BACKEND_HOST: host.docker.internal
    volumes:
      - ./eye_compass_fe:/app
    command: sh -c "npm install && npm run dev -- --host 0.0.0.0 --port 5173"

volumes:
  postgres_data_windows:
```

</details>

> Why a separate `docker-compose.windows.yml` rather than the plain
> `docker-compose.yml`? The default compose file uses `network_mode: host`,
> which is Jetson-specific (the device's kernel is missing `iptable_raw.ko`,
> so Docker's bridge networking won't start). Docker Desktop doesn't expose
> host-mode ports the same way, so Windows/Mac needs its own file.

### 2. Start the DB and frontend

From the project root:

```bash
docker compose -f docker-compose.windows.yml up -d
```

That brings up:

- **Postgres 18** on `localhost:5434` (not the default 5432, so it won't
  clash with a local Postgres service a laptop may already have running).
- **Vite dev server** on `localhost:5173`, bind-mounted against
  `eye_compass_fe/` so file edits hot-reload. The container reaches the
  natively-running backend via `host.docker.internal`.

### 3. Backend setup

Follow section **A** above (install, `.env`, run), but set:

```
DATABASE_URL=postgresql://postgres:password@localhost:5434/eye_compass
```

to match the Docker Postgres.

### 4. Open the UI

**http://localhost:5173**

### Shutting down

```bash
docker compose -f docker-compose.windows.yml down
```

Add `-v` to also wipe the Postgres volume for a clean slate next time.

---

## Common issues

- **Frontend loads but every API call fails** — the backend isn't up, or it's
  not on port 8000. The frontend container reaches the host via
  `host.docker.internal:8000`; if you changed the backend port, update the
  frontend's dev proxy to match.
- **Postgres "port already in use"** — something else on the laptop is on
  5434. Edit the port in `docker-compose.windows.yml` and the matching
  `DATABASE_URL`.
- **`USE_MOCK_CAMERA` left as false** — the backend tries to import TensorRT
  / the MVS SDK and crashes on startup. Set it to `true`.
