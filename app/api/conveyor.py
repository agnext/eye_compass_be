import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import serial

from app.services.serial_port import pick_serial_port

logger = logging.getLogger(__name__)
router = APIRouter()

class ConveyorCommand(BaseModel):
    command: str

@router.post("/api/conveyor/command")
def send_conveyor_command(req: ConveyorCommand):
    """
    Sends a serial command to the conveyor belt hardware.
    Typical commands: 'machine_start', 'all_stop', 'FM_detected'
    """
    cmd = req.command
    
    serial_port, _ = pick_serial_port()
    if not serial_port:
        logger.warning(f"No serial port available to send command '{cmd}'")
        return {"success": False, "error": "No serial port available", "command": cmd}

    baud_rate = 9600
    try:
        with serial.Serial(serial_port, baud_rate, timeout=1) as ser:
            ser.write((cmd + "\n").encode())
            logger.info(f"Sent command '{cmd}' to {serial_port}")
            # Optional: Read acknowledgment
            # ack = ser.readline().decode().strip()
            return {"success": True, "command": cmd, "port": serial_port}
    except Exception as e:
        logger.error(f"Failed to send command '{cmd}' to {serial_port}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Serial port error: {str(e)}")
