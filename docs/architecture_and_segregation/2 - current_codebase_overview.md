# 2. Eye Compass: Legacy Codebase Overview

This document provides a guided tour of the monolithic `eye_compass` codebase — the actual application running on the device today, and the thing every later document in this series is either porting out of, or verifying behavior against. Understanding this monolithic architecture is crucial before breaking it apart into microservices.

See `2b - dependencies_and_hardware.md` next for the full external hardware/software dependency inventory this codebase relies on.

## Hardware Context: What is an NVIDIA Jetson?
An **NVIDIA Jetson** is a small, low-power embedded computer designed specifically for running artificial intelligence (AI) and machine learning workloads at the "edge" (locally, rather than in the cloud). 
* **Why it's used here:** It has a built-in NVIDIA GPU, which is strictly required to process the live video feed from the camera and run the YOLOv7 object tracking models (`agnext_opti`) in real-time without latency. 
* **Architecture:** It runs an Ubuntu-based Linux operating system on an ARM64 (`aarch64`) processor. This is why our Python backend and PostgreSQL database must be compatible with Linux ARM64 to run on this device.

## How the legacy app is actually started

In production, it's fully automatic: `eye_compass.service` (a systemd unit,
`WantedBy=graphical.target`) runs `start_eye_compass_desktop.sh` on every boot,
which in turn calls `run_app.sh`:

```bash
#!/bin/bash
export QT_QPA_PLATFORM=xcb
export DISPLAY=:0
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export PYTHONPATH=/usr/lib/python3.8/dist-packages:$PYTHONPATH
/home/nvidia/.virtualenvs/m38/bin/python /home/nvidia/eye_compass/main.py
```

**Neither of those actually works on this dev unit**, confirmed directly:

```bash
sudo systemctl start eye_compass.service
# Failed to start eye_compass.service: Unit eye_compass.service not found.
```

The `.service` file living in the repo was never actually installed into
`/etc/systemd/system/` — see `5 - infrastructure_and_deployment.md` for how to
install it if you want `systemctl` to work. And `run_app.sh` itself fails:

```bash
cd /home/nvidia/eye_compass && ./run_app.sh
# bash: /home/nvidia/.virtualenvs/m38/bin/python: No such file or directory
```

because the `m38` (Python 3.8) virtualenv it hardcodes no longer exists on
this device. **The command that actually works**, using the unified venv
(Python 3.10, both the ML and web stacks) in its place:

```bash
cd /home/nvidia/eye_compass
QT_QPA_PLATFORM=xcb DISPLAY=:0 MVCAM_COMMON_RUNENV=/opt/MVS/lib \
PYTHONPATH=/opt/MVS/Samples/aarch64/Python/MvImport:/usr/lib/python3.8/dist-packages \
/home/nvidia/.virtualenvs/eye_compass/bin/python /home/nvidia/eye_compass/main.py
```

This runs in the foreground (its window appears on the physical display,
`DISPLAY=:0`) — `Ctrl+C` to stop it.

One other gap found while doing this on the dev unit: `sheet_update.py` was
missing a `get_cpu_id` function, worked around by swapping in a reference copy
of the code (see `9 - post_remediation_session_log.md` §6 for the full detail
and why the original install was preserved as `eye_compass_legacy/` rather
than edited in place).

## 1. The Entry Point and User Interface
**Files: `main.py` & `eye_compass_ui.py`**
* `main.py` is the heart of the application. When the Jetson device turns on, a Linux systemd service (`eye_compass.service`) automatically executes a shell script (`run_app.sh`) which launches `main.py`.
* It initializes the **PyQt5** graphical interface. The layout of the UI (buttons, video display areas) is heavily defined inside `eye_compass_ui.py` (which was likely generated from Qt Designer `.ui` files).
* This is where all the buttons ("Login", "Start Camera", "Stop Tracking") are wired up to trigger backend functions. It even includes custom UI components (like `ImageLabel` in `main.py`) to handle mouse interactions on the video feed.

## 2. Hardware & Camera Interaction
**Files: `GrabImage.py`, `GrabImage1.py`, `GrabImage_Video.py`**
* These files act as the bridge to the physical hardware. They use the **Hikvision MVS SDK** (`MvCameraControl_class`) to connect to the industrial camera over the Jetson's ports.
* When a scan starts, this script runs in a loop, grabbing raw video frames from the camera, converting their color profiles, and passing them off to the Machine Learning models.
* *Segregation impact:* Browsers cannot run this SDK natively, which is why this file will become the core of our new Python FastAPI backend.

## 3. Machine Learning (AI on the Edge)
**Files/Folders: `agnext_opti/` & `infer_onnx.py`**
* As frames are grabbed from the camera, they are fed into a **YOLOv7** object detection model. 
* The `agnext_opti` folder contains the optimized TensorRT/ONNX inference code tailored specifically for the Nvidia Jetson GPU.
* It identifies and tracks objects (like commodities) in real-time, overlaying bounding boxes onto the video frames before they are sent back to the PyQt UI to be displayed to the operator.

## 4. Local Database
**File: `database.py`**
* Manages the local **SQLite** database (`eye_compass.db`).
* It stores two main types of data:
  1. **Configurations:** Downloaded lists of commodities, vendors, and surveyor details.
  2. **Offline Results:** If the internet goes down, scan results are saved here temporarily with a `sync_status='0'`.
* *Segregation impact:* We will completely rewrite this file using an ORM (like SQLAlchemy) to talk to **PostgreSQL** instead of SQLite for better concurrency.

## 5. Network & Cloud Integrations (The Data Flow)
**File: `api_handle.py` (The "Qualix" Bridge)**
* This handles all REST API communications. When the app boots, it authenticates the user and fetches the latest commodity configurations from the Qualix servers.
* When a scan completes, it takes the results from the ML model and POSTs them to the Qualix analysis endpoint.

**File: `s3_upload.py` (A One-Shot Startup Sweep, Not a Watcher)**
* It runs as a `QThread` (`s3Uploading`), started once from `main.py:3148` when
  the background threads are started at boot.
* Despite how it reads at a glance, it does **not** watch the folder for new
  files as they're saved — `run()` walks the entire `output/` tree once,
  uploads anything not already in S3 at the same size, prints `"All images of
  output/ uploaded Successfully..."`, and the thread ends. It never loops.
  So in legacy, a file saved mid-session only gets uploaded the *next* time
  the app restarts, not while it's still running — confirmed directly from a
  real startup log on this dev unit, where that message appears exactly once
  right after boot and never again for the rest of the session.

---

## Summary of the Current Execution Flow
1. **Boot:** The app launches `main.py`, the UI loads, and `api_handle.py` fetches the latest configurations from Qualix.
2. **Action:** The operator clicks "Start". `GrabImage.py` spins up the camera and starts feeding video frames into the ML models (`agnext_opti`).
3. **Display:** The PyQt UI constantly updates its screen with the annotated video frames. It handles mouse clicks on the video via the `ImageLabel` class.
4. **Completion:** When the scan finishes, results are saved locally via `database.py`. `api_handle.py` pushes the results to Qualix. `s3_upload.py`'s upload sweep, however, already ran once at boot and does not run again until the app restarts — so these new files sit locally until then.

*In our new architecture, React will **only** handle Step 3 and drawing the buttons. Everything else will be moved into a headless FastAPI server running in the background.*
