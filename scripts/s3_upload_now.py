#!/usr/bin/env python3
"""Run the S3 upload now instead of waiting for its 3-day schedule.

    ./scripts/s3_upload_now.py --dry-run   # list what would be uploaded; no network, nothing deleted
    ./scripts/s3_upload_now.py             # upload + verify + delete, exactly as the scheduled run
    ./scripts/s3_upload_now.py --keep      # upload + verify, delete nothing

Same safety rules as the scheduled run (app/services/s3_worker.py): folders in
use or changed in the last S3_MIN_AGE_MINUTES are skipped, and a file is deleted
only once S3's copy reads back with the same SHA-256 and size. A real run also
counts as the scheduled one, so the next automatic run is S3_UPLOAD_EVERY_DAYS
from now.

Run it with the backend stopped or idle: this process cannot see a scan running
in the backend, only the folder-age rule protects a batch in progress.
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.services.s3_worker import S3UploaderTask  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="list only; no upload, no delete")
    ap.add_argument("--keep", action="store_true", help="upload and verify, but delete nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if args.keep:
        settings.S3_DELETE_AFTER_UPLOAD = False
    task = S3UploaderTask()
    summary = task.run_once(dry_run=True) if args.dry_run else task.tick(force=True)
    print(json.dumps(summary, indent=2))
    return 0 if (args.dry_run or (summary or {}).get("complete")) else 1


if __name__ == "__main__":
    sys.exit(main())
