# 2b. Dependencies & Hardware Inventory

Everything this application needs to exist and run, outside of its own code —
physical hardware, vendor SDKs, and third-party services. Read this after
`2 - current_codebase_overview.md` and before the segregation documents, since
every one of these is a *constraint* the split had to work around, not
something the split could change.

## Physical hardware

| Item | Detail | Why it matters to the split |
|---|---|---|
| **NVIDIA Jetson** (aarch64, Tegra kernel) | The edge device everything runs on. Ubuntu-based, ARM64. | The backend, PostgreSQL, and every Python dependency (TensorRT, pycuda, torch, OpenCV) must be ARM64-compatible. This rules out generic x86-only Docker images for anything CUDA-related. |
| **Hikvision GigE camera** (`MV-CS023-10GC`, confirmed present, reachable at `169.254.143.87`) | Industrial machine-vision camera connected over Ethernet (GigE Vision), not USB, not a `/dev/videoN` V4L2 device. | Talked to exclusively through Hikvision's own MVS SDK (`MvCameraControl_class`, via `MV_CC_EnumDevices`/`MV_CC_OpenDevice`/etc.) — a proprietary binary SDK, not a standard OS camera driver. Whatever process owns the camera needs that SDK's shared libraries (`MVCAM_COMMON_RUNENV=/opt/MVS/lib`) and Python bindings on its `PYTHONPATH`, and needs `MV_ACCESS_Exclusive` — only one process can hold it at a time. |
| **Conveyor belt controller** | A serial device on `/dev/ttyTHS1` (the Jetson's onboard UART), speaking a small plain-ASCII protocol — not Modbus, not a standardized industrial protocol. | Commands: `machine_start`, `all_stop`, `FM_detected`, `camera_on`, `camera_off`, each with its own expected ACK string (`machine_started`, `all_stoped`, `stoping_machine`, etc.). No adapter is guaranteed to be physically connected on a given dev unit — this port can go unanswered even when the software side is completely correct (see `9 - post_remediation_session_log.md`). |
| A separate Modbus RTU-capable VFD ("Goodrive" drive) | Reachable over `/dev/ttyUSB0` via a USB-to-RS485 adapter (CH340/341 chip). | **Not used by this application at all.** Found during hardware investigation in this session; it's a different physical port, different protocol (Modbus RTU vs. plain ASCII), and nothing in `eye_compass` talks to it. Almost certainly drives the belt or vibrator motor's speed directly, monitored by unrelated standalone scripts (`rpm.py`, `rpm1.py`, `rpm2.py`) that only *read* its output frequency — they don't control anything. Documented here so it's not confused with the conveyor protocol above. |

## Machine learning / GPU stack

- **TensorRT + pycuda + torch + OpenCV** — all provided by NVIDIA's JetPack for
  this device, **not** installable via a normal `pip install` on aarch64. The
  backend's own `requirements.txt` explicitly does not list them and instead
  documents that the interpreter running the backend must already have them
  (verified with `python -c "import tensorrt, pycuda.driver, torch, cv2, numpy"`).
  This is the single biggest reason the backend cannot simply run inside an
  arbitrary Docker container — see `5 - infrastructure_and_deployment.md`.
- **YOLOv7-family TensorRT engines** (`.optimized`/`.engine` files, one or more
  per commodity/variety) — these are data, not code. They must be physically
  present in `MODEL_DIR` on the device; the application does not train or
  generate them.
- **A PyTorch XAI model** (`v6_best.pt`, referenced by `test_xai.py`) — this one
  is genuinely missing on every copy of the legacy tree found on this device
  and on a prod unit checked during this session. See `todos.md`.

## External services

| Service | What it's used for | Where it's implemented |
|---|---|---|
| **Qualix** | The customer's cloud analytics platform. Login (online-first, offline fallback), commodity/vendor/brand/surveyor config sync, and posting completed scan results. | `app/services/sync_service.py`, `app/api/auth.py` |
| **Google Sheets** | A side-channel copy of every synced result. | `app/services/sync_service.py` (`post_to_sheets`) |
| **AWS S3** (via Cognito Identity Pool, bucket `agnext-cognito`) | Background upload of saved crop/frame images. | `app/services/s3_worker.py` |
| **PostgreSQL** | Replaces the legacy SQLite file. Runs in Docker on the device itself — this is a *local* dependency, not a cloud one. | `app/core/database.py`, `docker-compose.yml` |

## Summary: what the backend's own environment must provide

Everything above collapses into one requirement: the process running
`eye_compass_be` needs, at once, in the *same* Python interpreter: the MVS
camera SDK's Python bindings, TensorRT/pycuda/torch (JetPack-provided, not
pip), and a normal web stack (FastAPI, SQLAlchemy, boto3, gspread). No
off-the-shelf Docker base image ships all of that for aarch64 — this
combination is exactly why the backend runs natively in its own virtualenv
(`/home/nvidia/.virtualenvs/eye_compass`) rather than in a container. Continue
to `5 - infrastructure_and_deployment.md` for the full reasoning.
