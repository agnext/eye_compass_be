# 4. Infrastructure & Deployment

The deployment strategy for the `Eye Compass` system was completely overhauled to prioritize reliability, remote updates, and hardware compatibility on the NVIDIA Jetson edge device.

## The Challenge
AI models (via TensorRT) and hardware peripherals (Cameras and Conveyors) require extremely low-level access to the Jetson's GPU and serial ports. Running these inside a Docker container on an ARM64 edge device adds a layer of complexity and overhead that can lead to dropped frames or failed hardware initialization. 

However, running a modern web stack (React, Nginx, PostgreSQL) directly on the host OS leads to "dependency hell" and makes remote updates brittle.

## The Hybrid Deployment Solution
We utilized a hybrid deployment strategy to get the best of both worlds:

### 1. The Containerized Layer (Docker Compose)
A `docker-compose.yml` file is used to orchestrate the non-hardware dependent microservices.
- **`frontend` Container**: Runs the built React application (served via Nginx).
- **`db` Container**: Runs `postgres:15-alpine`. Uses a **Named Volume** (`postgres_data`) to persist all configuration and scan data safely across device reboots without worrying about host OS file permissions.

### 2. The Native Layer (Systemd)
The FastAPI backend (`eye_compass_be`) runs natively on the Jetson's host OS.
- Managed by a standard Linux `systemd` service (`eye_compass.service`).
- Has direct, bare-metal access to `/dev/video0` (Camera), `/dev/ttyUSB0` (Modbus), and the raw NVIDIA GPU drivers for maximum TensorRT inference speed.
- It connects to the Dockerized Postgres database via `localhost:5432` (which is exposed by `docker-compose`).

## Environment Variables (`.env`)
Configuration was moved out of the legacy `config.INI` and into a modern `.env` file within the backend root. 
- Handles database credentials (`DATABASE_URL`).
- Handles offline authentication (`QUALIX_USERNAME` / `QUALIX_PASSWORD`).
- Manages hardware tunings (e.g., `USE_MOCK_CAMERA` for testing on non-Jetson devices like Windows developer laptops).

### Why are some variables commented out?
In the `.env` file, several paths and camera tuning parameters (like `MVS_SDK_PATH` or `CAMERA_EXPOSURE_TIME`) are intentionally commented out. 
- **Default Fallbacks:** The backend code is programmed to automatically use the standard Jetson defaults if it doesn't find these variables.
- **Convenience Overrides:** They are left in the `.env` file purely as a convenience. If a developer needs to run the code on a different machine, or tweak the camera exposure for a specific dark room, they can simply uncomment the line and change the value without having to dig through and modify the Python source code.
