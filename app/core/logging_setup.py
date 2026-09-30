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
from datetime import datetime, timedelta

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
    def __init__(self, use_colour: bool, datefmt: str = "%H:%M:%S"):
        super().__init__(datefmt=datefmt)
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


class DailyFileHandler(logging.FileHandler):
    """Writes to `logs/eye_compass_<today>.log`, switching file at midnight.

    Legacy's shape, not `TimedRotatingFileHandler`'s. The stdlib handler writes
    to one fixed name and only *renames* it on rollover, so today's log would be
    `eye_compass.log` and only yesterday's would carry a date — the opposite of
    legacy, which computed the dated name up front and wrote straight into it
    (`logger.py:31-35`). Matching that matters because it is what anyone
    supporting this device already knows to look for.

    The date is checked on the way into each record rather than on a timer:
    there is no scheduler here, the device idles for hours at a stretch, and a
    timer that fires while nothing is being logged would only create an empty
    file for a day that had no events.

    Each record is flushed as it is written, as legacy's ImmediateFlushFileHandler
    did (`logger.py:59-63`). The whole reason for keeping a file next to the
    journal is the case where the device goes down unexpectedly, and a buffered
    handler loses exactly the last few lines — the ones explaining why.
    """

    def __init__(self, directory: str):
        self.directory = directory
        self._day = self._today()
        os.makedirs(directory, exist_ok=True)
        super().__init__(self._path_for(self._day), mode="a", encoding="utf-8",
                         delay=True)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _path_for(self, day: str) -> str:
        return os.path.join(self.directory, f"eye_compass_{day}.log")

    def emit(self, record: logging.LogRecord) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self.baseFilename = os.path.abspath(self._path_for(day))
            if self.stream:
                self.close()
        super().emit(record)
        if self.stream:
            self.stream.flush()


def purge_old_logs(directory: str, keep_days: int) -> None:
    """Delete `eye_compass_<date>.log` files older than `keep_days`.

    Legacy intended to do this and never did: `archive_old_logs` (`logger.py:38`)
    put its zip-and-delete block *after* a `continue`, so it was unreachable and
    every log file it ever wrote is still on disk. Left alone on a device nobody
    prunes by hand that grows without a ceiling, so this port actually runs the
    sweep. `LOG_RETENTION_DAYS=0` turns it off and restores legacy's behaviour.

    Only files matching the exact name pattern are considered, and only ones
    whose date parses — so a hand-copied or renamed file in the same directory
    is never touched.
    """
    if keep_days <= 0 or not os.path.isdir(directory):
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for name in os.listdir(directory):
        if not (name.startswith("eye_compass_") and name.endswith(".log")):
            continue
        stamp = name[len("eye_compass_"):-len(".log")]
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d")
        except ValueError:
            continue
        if when < cutoff:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


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


# Set by configure_logging so main.py can attach the same file to uvicorn's own
# loggers once uvicorn has finished installing its handlers — see attach_to().
file_handler: "DailyFileHandler | None" = None


def configure_logging(level=logging.INFO):
    formatter = ColourFormatter(_colour_enabled())
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()   # replace basicConfig's default handler
    root.addHandler(handler)
    root.setLevel(level)

    _add_file_handler(root)

    # These log every request; useful, but they drown out everything else at
    # DEBUG and add nothing at INFO beyond what uvicorn.access already gives.
    logging.getLogger("multipart").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _add_file_handler(root: logging.Logger) -> None:
    """Add the daily file alongside the stream handler.

    The journal stays the primary way these logs are read (`docs/logging.md`),
    and this does not change that. It exists because journald on this device
    stores nothing across a reboot: `/var/log/journal` is absent, so `Storage=auto`
    resolves to `/run/log/journal`, which is tmpfs. Every line is gone the moment
    the device restarts, and a kiosk is restarted often — including by whatever
    fault someone would then want the logs for. Legacy kept dated files and they
    are still readable months later; this gets that back.

    The date carried in the file's timestamps but not the console's: a line read
    six weeks later has to say which day it is from, while a line being watched
    live does not.

    Deliberately non-fatal. A read-only or full disk must not stop the backend
    from starting on a device whose whole job is the belt — the stream handler
    is already installed by this point, so the failure is itself logged and
    everything continues without the file.
    """
    global file_handler
    try:
        from app.core.config import settings   # local: config must not depend
                                               # on logging setup, or the import
                                               # order in main.py becomes load-
                                               # bearing.
        purge_old_logs(settings.LOG_DIR, settings.LOG_RETENTION_DAYS)
        file_handler = DailyFileHandler(settings.LOG_DIR)
        file_handler.setFormatter(
            ColourFormatter(use_colour=False, datefmt="%Y-%m-%d %H:%M:%S")
        )
        root.addHandler(file_handler)
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Could not open the log file — logging to the journal only: %s", exc
        )


def attach_to(*logger_names: str) -> None:
    """Send these loggers' records to the log file as well.

    uvicorn installs its own handlers on `uvicorn`/`uvicorn.access` with
    propagation off, and it does so *after* this module runs — so the request
    lines and the startup banner reach the console but would otherwise never
    reach the file. Called from the lifespan, which is late enough.

    Only loggers that do NOT propagate are attached to. A propagating logger
    already reaches the file through root, and attaching there as well writes
    every one of its records a second time — and a third, for a child like
    `uvicorn.access` whose parent `uvicorn` was attached to in the same call.
    Whether uvicorn sets `propagate = False` depends on the log config it was
    started with, so this is decided per logger at call time rather than
    assumed.
    """
    if file_handler is None:
        return
    for name in logger_names:
        log = logging.getLogger(name)
        if log.propagate:
            continue
        if file_handler not in log.handlers:
            log.addHandler(file_handler)
