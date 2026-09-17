#!/usr/bin/env python3
"""
Coloured log viewer for the backend.

systemd-journald strips ANSI escape codes out of anything written to it, so the
backend cannot colour its own output when running as a service — the codes are
gone before journalctl ever sees them. Colouring therefore has to happen when
the logs are *read*, which is what this does: it runs journalctl and colours the
output on the way past.

Usage:
    ./scripts/logs.py                 # follow live (default)
    ./scripts/logs.py -n 200          # last 200 lines
    ./scripts/logs.py --since "1 hour ago"
    ./scripts/logs.py --auth          # only login / session / sync activity
    ./scripts/logs.py --errors        # only warnings and errors

Any other arguments are passed straight through to journalctl.
"""

import re
import subprocess
import sys

SERVICE = "eye-compass-backend.service"

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

LEVEL_COLOURS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;37;41m",
}

TAG_COLOUR = "\033[35m"
SUCCESS_COLOUR = "\033[1;32m"
FAIL_COLOUR = "\033[1;31m"

# journalctl prefix, then the backend's own "HH:MM:SS LEVEL logger: message"
LINE = re.compile(
    r"^(?P<prefix>\w{3} \d{2} [\d:]+ \S+ \S+?\[\d+\]: )?"
    r"(?P<time>\d{2}:\d{2}:\d{2}) +"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) +"
    r"(?P<logger>[\w.]+): "
    r"(?P<message>.*)$"
)

# Phrases worth making obvious at a glance while scanning a busy log.
HIGHLIGHTS = [
    (re.compile(r"(TIER \d SUCCESS|session\(s\) restored|Extended for|token obtained)"), SUCCESS_COLOUR),
    (re.compile(r"(ALL TIERS FAILED|REJECTED|FAILED|Invalid|rejected)"), FAIL_COLOUR),
]


def colourise(line: str) -> str:
    match = LINE.match(line.rstrip("\n"))
    if not match:
        return line.rstrip("\n")

    level = match.group("level")
    colour = LEVEL_COLOURS.get(level, "")
    message = match.group("message")

    # [TAG] at the start of a message marks a flow step.
    if message.startswith("["):
        end = message.find("]")
        if 0 < end <= 12:
            message = f"{TAG_COLOUR}{BOLD}{message[:end + 1]}{RESET} {message[end + 1:].lstrip()}"

    for pattern, phrase_colour in HIGHLIGHTS:
        message = pattern.sub(lambda m: f"{phrase_colour}{m.group(0)}{RESET}", message)

    if level in ("WARNING", "ERROR", "CRITICAL"):
        message = f"{colour}{message}{RESET}"

    return (
        f"{DIM}{match.group('time')}{RESET} "
        f"{colour}{BOLD}{level:<7}{RESET} "
        f"{DIM}{match.group('logger')}{RESET}: {message}"
    )


def main():
    args = sys.argv[1:]
    grep = None

    if "--auth" in args:
        args.remove("--auth")
        grep = "AUTH|SESSION|REVALIDATE|SYNC|Auth provider"
    if "--errors" in args:
        args.remove("--errors")
        args += ["-p", "warning"]

    # Default to following live, as that is what this is usually used for.
    if not any(a in args for a in ("-n", "--lines", "--since", "--until", "-f", "--follow")):
        args.append("-f")

    cmd = ["journalctl", "-u", SERVICE, "--no-pager", "-o", "short"] + args
    if grep:
        cmd += ["--grep", grep]

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
        for line in process.stdout:
            print(colourise(line), flush=True)
    except KeyboardInterrupt:
        pass
    except FileNotFoundError:
        sys.exit("journalctl not found — is this running on the device?")


if __name__ == "__main__":
    main()
