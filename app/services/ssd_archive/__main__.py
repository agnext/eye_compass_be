"""Command line, run from eye_compass_be with the backend's Python.

    python -m app.services.ssd_archive init <ssd-mount-point>
    python -m app.services.ssd_archive status
    python -m app.services.ssd_archive dry-run

Real runs happen only inside the backend, which knows what is in use.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import ArchiveConfig
from .target import find_target, init_target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.ssd_archive",
                                     description="EyeCompass archive SSD")
    parser.add_argument("-v", "--verbose", action="store_true", help="show per-file detail")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="mark a mounted drive as this device's archive SSD")
    p_init.add_argument("mountpoint", type=Path)
    sub.add_parser("status", help="the last run, and whether the SSD is connected")
    sub.add_parser("dry-run", help="report what a run would copy and remove; changes nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)
    cfg = ArchiveConfig.from_settings()

    if args.command == "init":
        return init_target(cfg, args.mountpoint)
    if args.command == "status":
        from .worker import load_state

        state = load_state()
        print(json.dumps(state, indent=2, default=str) if state else "No run recorded yet.")
        target = find_target(cfg, writable=False)
        print(f"Archive SSD: {target or 'not connected'}")
        return 0 if target else 75
    if args.command == "dry-run":
        from .worker import run_once

        summary = run_once(cfg, dry_run=True)
        print(json.dumps(summary, indent=2))
        return 0 if summary["result"] == "ok" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
