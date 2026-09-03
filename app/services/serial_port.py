import glob
import os
from typing import List, Optional, Tuple


def list_existing_serial_ports() -> List[str]:
    """
    Best-effort list of serial devices present on the system.
    We intentionally don't rely on pyserial's list_ports here because it often
    doesn't report Jetson built-in UART nodes (ttyTHS*), while the device files
    still exist and are openable.
    """
    patterns = [
        "/dev/ttyTHS*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/ttyS*",
    ]
    ports: List[str] = []
    for pat in patterns:
        ports.extend(glob.glob(pat))
    # Remove obvious non-device matches and keep stable ordering
    ports = [p for p in ports if os.path.exists(p)]
    return sorted(set(ports))


def pick_serial_port(
    preferred: Optional[str] = None,
    env_var: str = "EYE_COMPASS_SERIAL_PORT",
) -> Tuple[Optional[str], List[str]]:
    """
    Pick a serial port to use.

    Order of precedence:
    - explicit `preferred` argument (if it exists)
    - environment variable `env_var` (if it exists)
    - first existing port from a Jetson-friendly candidate list
    - otherwise returns (None, available_ports)
    """
    available = list_existing_serial_ports()

    if preferred and os.path.exists(preferred):
        return preferred, available

    env_val = os.getenv(env_var)
    if env_val and os.path.exists(env_val):
        return env_val, available

    candidates = [
        # Prefer USB serial first for current deployment.
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
        "/dev/ttyACM0",
        "/dev/ttyACM1",
        # Jetson UART nodes (common)
        "/dev/ttyTHS1",
        "/dev/ttyTHS0",
        "/dev/ttyTHS2",
        "/dev/ttyTHS3",
        # Fallback to standard UARTs
        "/dev/ttyS0",
        "/dev/ttyS1",
    ]

    for c in candidates:
        if os.path.exists(c):
            return c, available

    # Last-ditch: any available port
    if available:
        return available[0], available

    return None, available


def format_available_ports(available: List[str]) -> str:
    if not available:
        return "(none found under /dev/ttyTHS*, /dev/ttyS*, /dev/ttyUSB*, /dev/ttyACM*)"
    return ", ".join(available)

