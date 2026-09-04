"""Conveyor control endpoints.

Route paths are relative — app/main.py mounts this router under /api/conveyor.
Writing the full path here as well produced /api/conveyor/api/conveyor/command,
which is why nothing the UI sent ever arrived.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.conveyor_service import EXPECTED_ACKS, conveyor_service

logger = logging.getLogger(__name__)
router = APIRouter()


class ConveyorCommand(BaseModel):
    command: str


@router.post("/command")
def send_conveyor_command(req: ConveyorCommand):
    """Send a control command to the conveyor and wait for its acknowledgment.

    Accepted commands: all_stop, machine_start, FM_detected, camera_on, camera_off.
    A command that is not acknowledged is an error, not a success — the fail-safe
    all_stop has already been sent by the time this returns 502.
    """
    cmd = req.command

    if cmd not in EXPECTED_ACKS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command '{cmd}'. Expected one of: {', '.join(sorted(EXPECTED_ACKS))}",
        )

    ok = conveyor_service.send(cmd)
    if not ok:
        status = conveyor_service.status()
        # The interlock is a normal operating state, not a hardware fault.
        if cmd == "machine_start" and status["machine_start_locked"]:
            raise HTTPException(
                status_code=409,
                detail="Conveyor is locked after foreign matter was detected. "
                "Resolve the detection (Submit or Forward) before restarting.",
            )
        raise HTTPException(
            status_code=502,
            detail=status["last_error"] or f"Conveyor did not acknowledge '{cmd}'",
        )

    return {"success": True, "command": cmd, **conveyor_service.status()}


@router.post("/unlock")
def unlock_machine_start():
    """Release the machine-start interlock after the operator resolves a detection.

    Legacy did this from Submit (main.py:1291) and Forward (main.py:856, 877).
    """
    conveyor_service.unlock_machine_start(reason="operator via /api/conveyor/unlock")
    return {"success": True, **conveyor_service.status()}


@router.post("/forward")
def jog_forward(seconds: float = 2.0):
    """Jog the belt forward briefly, then stop.

    Port of move_conveyor_forward (main.py:851-887): unlock, start, wait, stop.
    """
    import time

    conveyor_service.unlock_machine_start(reason="forward jog")
    if not conveyor_service.send("machine_start"):
        raise HTTPException(
            status_code=502,
            detail=conveyor_service.status()["last_error"] or "Could not start conveyor",
        )
    time.sleep(max(0.0, min(seconds, 10.0)))
    conveyor_service.send("all_stop")
    return {"success": True, "jogged_seconds": seconds}


@router.get("/status")
def conveyor_status():
    return conveyor_service.status()
