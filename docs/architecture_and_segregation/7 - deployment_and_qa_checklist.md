# 7. Deployment & QA Checklist

Since 100% of the legacy application's UI and architecture have been successfully migrated to the new web-based Full-Stack architecture (React + FastAPI + PostgreSQL), the next phase of the project transitions from *Development* to *Quality Assurance (QA) and Deployment*.

Below is the checklist of required next steps to validate the system on physical hardware.

## 1. Physical Hardware QA
Currently, development and testing have occurred in a standard desktop environment. To finalize the migration, the Docker containers must be deployed onto the actual edge device (e.g., Jetson Nano, IPC).
- **Camera Validation:** Verify that the MVS Camera SDK correctly binds to the Docker container and that the FastAPI WebSocket streams the physical camera feed.
- **Serial Port Validation:** Verify that clicking the green **START** / **STOP** buttons in the React UI correctly sends the `machine_start` and `all_stop` serial bytes out of the hardware's `/dev/ttyUSB0` port to physically control the conveyor belt.

## 2. End-to-End System Run
Run a physical sample of grain (e.g., Wheat) through the physical machine to ensure the entire pipeline triggers correctly in sequence:
- **[ ]** Login securely.
- **[ ]** Enter New Batch Details (12 fields).
- **[ ]** Start Conveyor.
- **[ ]** Verify YOLO Inference is running on the live frames and detections are logged.
- **[ ]** Stop Conveyor.
- **[ ]** Submit Results.
- **[ ]** Verify that the results appear accurately on the `ResultsViewer` screen.
- **[ ]** Verify the data is saved in PostgreSQL and appears on the `History` screen.
- **[ ]** Verify any external cloud synchronizations (Qualix API, Google Sheets).

## 3. Kiosk Mode Operating System Setup
To ensure the Edge device feels like a dedicated appliance (rather than a standard desktop computer), the host OS should be configured for Kiosk Mode:
- **Auto-start Services:** Configure `systemd` to automatically run `docker-compose up -d` on system boot.
- **Fullscreen Browser:** Configure the OS desktop environment to launch a browser (Google Chrome or Mozilla Firefox) on startup.
- **Kiosk Flags:** Launch the browser using kiosk flags (e.g., `chromium-browser --kiosk http://localhost:5143 --disable-restore-session-state`) so it locks the user into the Eye Compass React interface and hides the address bar/desktop.
