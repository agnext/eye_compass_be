# Deployment and Bug Fix: FM Review Screen Stale Detection Fix

**Date:** 2026-10-15  
**Component:** Frontend (`eye_compass_fe` - `Dashboard.jsx`) & Backend (`eye_compass_be` - `scan.py`)  
**Target Environment:** Production Jetson Device (`eye_compass_new`)  

---

## 1. Issue Summary

**Bug Title:** FM review screen shows `"No pending detection with index N"` and locks up after clicking Submit without selecting a label type.

### Symptoms & Production Impact
* During live batch inspection, when foreign matter was detected, the conveyor halted and prompted the operator to select a type.
* If the operator clicked **Submit** directly without picking a type, the type-picker popup re-opened on the *next* foreign matter detection screen, still bound to the previous detection's index.
* Every subsequent attempt to choose a label sent that stale index to the backend, resulting in `404: No pending detection with index N`.
* The modal remained stuck open, preventing scanning progress until the operator manually tapped the new box directly or triggered a Recover action.

### Root Cause
The selected detection index (`activeBox`) was only cleared upon a successful `/api/scan/label` response. It was not cleared when:
1. The operator clicked **Submit** without choosing a label type.
2. The review queue promoted a new detection underneath the active modal.

---

## 2. Code Changes

### Frontend (`eye_compass_fe/src/pages/Dashboard.jsx`)
* `handleResume`: Explicitly clears `activeBox` when **Submit** is pressed.
* Added an effect that unsets `activeBox` whenever its index is no longer present in the pending detection list or when the review overlay closes.
* Guarded `handleLabel` against duplicate in-flight requests.
* Intercepted `404` errors from `/api/scan/label` to automatically close the stale modal and flash a self-dismissing banner.

### Backend (`eye_compass_be/app/api/scan.py`)
* Sanitized `KeyError` exception string formatting in `label_detection` by unpacking `exc.args[0]` to prevent literal quotes in error messages.

---

## 3. Deployment Procedure Executed

The following deployment steps were run on the Jetson device:

```bash
# 1. Rebuild frontend Docker container (Nginx production build)
cd /home/nvidia/eye_compass_new
sudo docker compose build --no-cache frontend
sudo docker compose up -d --force-recreate frontend

# 2. Restart backend systemd service
sudo systemctl restart eye-compass-backend.service

# 3. Reload browser kiosk
pkill -f eye-compass-kiosk.sh || true
pkill -f firefox || true
nohup /home/nvidia/.local/bin/eye-compass-kiosk.sh >/dev/null 2>&1 &
```

---

## 4. Verification & Health Check Results

* **Docker Containers (`docker compose ps`):**
  * `eye_compass_new-frontend-1`: `Up` (fresh container build)
  * `eye_compass_new-db-1`: `Up` (PostgreSQL healthy)
* **Backend Service (`systemctl status eye-compass-backend.service`):**
  * Status: `active (running)`
* **Health API Check (`GET http://localhost:8000/api/health`):**
  ```json
  {
    "status": "ok",
    "mock_mode": false,
    "camera": {
      "initialized": false,
      "model_loaded": false,
      "stream_clients": 0,
      "stream_engine_running": false
    }
  }
  ```
