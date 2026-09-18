"""
Log formatting.

The backend's logs are read almost exclusively through
`journalctl -u eye-compass-backend.service`, usually while something is going
wrong on a device. Plain single-colour output makes that harder than it needs to
be: a warning buried in a few hundred INFO lines looks identical to everything
around it.

This sets a consistent shape so the eye can find the same field in every line:

    HH:MM:SS  LEVEL    logger.name: message

**Colour does not survive journald.** systemd-journald strips ANSI escape codes
out of whatever is written to it, so a service logging in colour has its colour
removed before journalctl ever sees the message — verified directly on this
device. Colouring therefore has to happen when the logs are *read*, which is what
`scripts/logs.py` does.

The colour support here is still worth having: it applies when the backend is run
by hand in a terminal (uvicorn during development), where nothing strips it. It
defaults to `auto`, which means it switches itself off under systemd rather than
emitting codes that are guaranteed to be discarded.
"""

import logging
import os
import sys

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

LEVEL_COLOURS = {
    logging.DEBUG: "\033[36m",     # cyan
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",     # red
    logging.CRITICAL: "\033[1;37;41m",  # white on red
}

# Tags used to mark the major flows (see docs/logging.md). Highlighted so a
# login or sync line stands out from routine chatter without having to read it.
TAG_COLOUR = "\033[35m"  # magenta


class ColourFormatter(logging.Formatter):
    def __init__(self, use_colour: bool):
        super().__init__(datefmt="%H:%M:%S")
        self.use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, self.datefmt)
        level = record.levelname
        name = record.name
        message = record.getMessage()

        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        if not self.use_colour:
            return f"{time_str} {level:<7} {name}: {message}"

        colour = LEVEL_COLOURS.get(record.levelno, "")

        # A leading [TAG] marks a flow step; colour it so it can be picked out
        # at a glance when scrolling.
        if message.startswith("["):
            end = message.find("]")
            if 0 < end <= 12:
                message = f"{TAG_COLOUR}{message[:end + 1]}{RESET} {message[end + 1:].lstrip()}"

        return (
            f"{DIM}{time_str}{RESET} "
            f"{colour}{BOLD}{level:<7}{RESET} "
            f"{DIM}{name}{RESET}: "
            f"{colour if record.levelno >= logging.WARNING else ''}{message}"
            f"{RESET if record.levelno >= logging.WARNING else ''}"
        )


def _colour_enabled() -> bool:
    """LOG_COLOR: auto (default) | always | never.

    `auto` means "colour only when writing to a terminal". Under systemd there
    is no terminal, so colour switches off — which is correct, because journald
    would strip the codes anyway and emitting them achieves nothing. Running
    uvicorn by hand in a terminal does get colour.
    """
    setting = (os.getenv("LOG_COLOR") or "auto").strip().lower()
    if setting in ("never", "off", "false", "0"):
        return False
    if setting in ("always", "on", "true", "1"):
        return True
    return sys.stderr.isatty()


def configure_logging(level=logging.INFO):
    formatter = ColourFormatter(_colour_enabled())
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()   # replace basicConfig's default handler
    root.addHandler(handler)
    root.setLevel(level)

    # These log every request; useful, but they drown out everything else at
    # DEBUG and add nothing at INFO beyond what uvicorn.access already gives.
    logging.getLogger("multipart").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
