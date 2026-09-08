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

To start it by hand instead (e.g. after stopping it to compare against this
port, as in `9 - post_remediation_session_log.md` §6):

```bash
sudo systemctl start eye_compass.service      # the normal path
# or, directly, from a graphical session on the device itself:
cd /home/nvidia/eye_compass && ./run_app.sh
```

Two real gaps found while doing this on the dev unit: the `m38` (Python 3.8)
virtualenv the script hardcodes no longer exists on this device — the unified
`/home/nvidia/.virtualenvs/eye_compass` venv (Python 3.10, both the ML and web
stacks) was used in its place — and `sheet_update.py` was missing a
`get_cpu_id` function, worked around by swapping in a reference copy of the
code (see `9 - post_remediation_session_log.md` §6 for the full detail on both
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

**File: `s3_upload.py` (The Silent Background Worker)**
* It runs as a `QThread` (a background thread managed by PyQt). 
* It constantly watches the local `output/` folder. Whenever raw images or data from a scan are saved there, it silently uploads them to the `agnext-cognito` AWS S3 bucket in chunks using AWS Cognito credentials.

---

## Summary of the Current Execution Flow
1. **Boot:** The app launches `main.py`, the UI loads, and `api_handle.py` fetches the latest configurations from Qualix.
2. **Action:** The operator clicks "Start". `GrabImage.py` spins up the camera and starts feeding video frames into the ML models (`agnext_opti`).
3. **Display:** The PyQt UI constantly updates its screen with the annotated video frames. It handles mouse clicks on the video via the `ImageLabel` class.
4. **Completion:** When the scan finishes, results are saved locally via `database.py`. `api_handle.py` pushes the results to Qualix, and `s3_upload.py` quietly uploads the raw image files to AWS S3 in the background.

*In our new architecture, React will **only** handle Step 3 and drawing the buttons. Everything else will be moved into a headless FastAPI server running in the background.*
