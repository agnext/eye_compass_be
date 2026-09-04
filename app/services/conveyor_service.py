"""
Conveyor / serial control.

A faithful port of legacy `send_control_command` (main.py:2279-2378) plus the
`machine_start_locked` interlock (main.py:105, 2288-2296, 2621-2623).

Everything here matters for physical safety, so none of it is optional:

  * Commands are whitelisted. Legacy refused anything not in `expected_acks`;
    without that check an arbitrary request body reaches the UART.
  * Every write waits for the expected acknowledgment and retries up to
    SERIAL_MAX_RETRIES times. A write with no ACK is not a success.
  * If all retries fail, `all_stop` is sent as a fail-safe.
  * `machine_start` is refused while the interlock is engaged, i.e. between
    foreign matter being detected and the operator resolving it.

Access is serialised behind a lock: unlike the legacy Qt main thread, several
HTTP requests can arrive at once and must not open the port simultaneously.
"""

import logging
import threading
import time

import serial

from app.core.config import settings
from app.services.serial_port import format_available_ports, pick_serial_port

logger = logging.getLogger(__name__)


# Legacy mapping, verbatim from main.py:2297-2304.
EXPECTED_ACKS = {
    "all_stop": "all_stoped",
    "FM_detected": "stoping_machine",
    "machine_start": "machine_started",
    "camera_on": "camera_on",
    "camera_off": "camera_off",
}


class ConveyorService:
    """Owns the serial port and the machine-start interlock."""

    def __init__(self):
        self._lock = threading.RLock()
        self._machine_start_locked = False
        self._last_error = ""

    # ------------------------------------------------------------------
    # Interlock
    # ------------------------------------------------------------------

    @property
    def machine_start_locked(self) -> bool:
        return self._machine_start_locked

    def lock_machine_start(self, reason: str = "FM_detected"):
        """Engage the interlock.

        Legacy sets this *before* sending FM_detected (main.py:2621-2623) so a
        machine_start arriving in the gap cannot win the race. Callers must
        preserve that ordering.
        """
        self._machine_start_locked = True
        logger.info("machine_start LOCKED (%s)", reason)

    def unlock_machine_start(self, reason: str = "operator"):
        """Release the interlock — operator pressed Submit or Forward."""
        if self._machine_start_locked:
            logger.info("machine_start UNLOCKED (%s)", reason)
        self._machine_start_locked = False

    # ------------------------------------------------------------------
    # Command path
    # ------------------------------------------------------------------

    def send(self, cmd: str, max_retries: int = None) -> bool:
        """Send a control command and wait for its acknowledgment.

        Returns True only when the hardware acknowledged. Mirrors
        main.py:2279-2378 including the fail-safe all_stop.
        """
        max_retries = max_retries or settings.SERIAL_MAX_RETRIES

        if cmd not in EXPECTED_ACKS:
            logger.error("Rejected unknown conveyor command: %r", cmd)
            self._last_error = f"Unknown command: {cmd}"
            return False

        if cmd == "machine_start" and self._machine_start_locked:
            logger.warning(
                "machine_start BLOCKED — locked after FM_detected. "
                "Submit or Forward to unlock."
            )
            self._last_error = "machine_start is locked pending foreign-matter resolution"
            return False

        expected_ack = EXPECTED_ACKS[cmd]

        port, available = pick_serial_port(preferred=settings.SERIAL_PORT or None)
        if not port:
            msg = (
                "No usable serial port found. Set EYE_COMPASS_SERIAL_PORT to one of: "
                f"{format_available_ports(available)}"
            )
            logger.error(msg)
            self._last_error = msg
            return False

        baud = settings.SERIAL_BAUD

        with self._lock:
            acknowledged = False
            for attempt in range(1, max_retries + 1):
                try:
                    with serial.Serial(port, baud, timeout=1) as ser:
                        logger.info(
                            "Attempt %s: sending %r to %s", attempt, cmd, port
                        )
                        ser.write((cmd + "\n").encode())
                        ack = ser.readline().decode("utf-8", errors="replace").strip()

                        # Legacy also accepts the all_stop ack for any command —
                        # the controller reports a stop however it was reached.
                        if ack == expected_ack or ack == EXPECTED_ACKS["all_stop"]:
                            logger.info("Acknowledgment received: %s", ack)
                            acknowledged = True
                            break
                        logger.warning(
                            "Unexpected acknowledgment %r (wanted %r). Retrying...",
                            ack,
                            expected_ack,
                        )
                except Exception as exc:
                    logger.error("Serial error on attempt %s: %s", attempt, exc)

                time.sleep(0.02)

            if acknowledged:
                self._last_error = ""
                return True

            # Fail-safe: we could not confirm the command, so stop the belt.
            logger.error(
                "No acknowledgment for %r after %s retries — sending fail-safe all_stop",
                cmd,
                max_retries,
            )
            self._last_error = f"No acknowledgment for {cmd} after {max_retries} retries"
            if cmd != "all_stop":
                try:
                    with serial.Serial(port, baud, timeout=1) as ser:
                        ser.write(b"all_stop\n")
                except Exception as exc:
                    logger.error("Fail-safe all_stop could not be sent: %s", exc)
            return False

    def status(self) -> dict:
        port, available = pick_serial_port(preferred=settings.SERIAL_PORT or None)
        return {
            "port": port,
            "available_ports": available,
            "baud": settings.SERIAL_BAUD,
            "machine_start_locked": self._machine_start_locked,
            "last_error": self._last_error,
        }


class MockConveyorService(ConveyorService):
    """No hardware: log the command, keep the interlock semantics intact."""

    def send(self, cmd: str, max_retries: int = None) -> bool:
        if cmd not in EXPECTED_ACKS:
            logger.error("Rejected unknown conveyor command: %r", cmd)
            self._last_error = f"Unknown command: {cmd}"
            return False
        if cmd == "machine_start" and self._machine_start_locked:
            logger.warning("[mock] machine_start BLOCKED — interlock engaged")
            self._last_error = "machine_start is locked pending foreign-matter resolution"
            return False
        logger.info("[mock conveyor] %s -> %s", cmd, EXPECTED_ACKS[cmd])
        self._last_error = ""
        return True

    def status(self) -> dict:
        return {
            "port": "(mock)",
            "available_ports": [],
            "baud": settings.SERIAL_BAUD,
            "machine_start_locked": self._machine_start_locked,
            "last_error": self._last_error,
        }


conveyor_service: ConveyorService = (
    MockConveyorService() if settings.USE_MOCK_CAMERA else ConveyorService()
)
