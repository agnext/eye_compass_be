#!/usr/bin/env python3
"""
Migrate the legacy SQLite database into PostgreSQL.

Run from the backend root:
    python scripts/migrate_sqlite_to_postgres.py [--sqlite /path/to/eye_compass.db] [--dry-run]

Four things the previous version got wrong, all of which lost data:

  1. It parsed the `result` blob with json.loads. Legacy wrote it with
     str(dict) (main.py:2821) and read it back with eval() — a Python repr with
     single quotes. json.loads fails on every row, and the fallback stored
     {"raw_string": ...}, so all history became opaque. This uses
     ast.literal_eval, which is what the format actually is.
  2. It deduplicated on sample_id, which is not unique — the live database has
     79 rows across 10 distinct sample_ids, so 69 would have been dropped.
     Legacy identified a record by (sample_id, date, start_time, stop_time)
     (database.py:433-445), and that is the key used here.
  3. It only migrated `result`. The other six tables — creds, clientinfo,
     surveyordetails, branddetails, vendordetails, com_details — carry the
     config a device needs to run at all.
  4. sys.path pointed at scripts/ rather than the backend root, so the import
     of app.core.database failed before any of the above could matter.
"""

import argparse
import ast
import json
import os
import sqlite3
import sys

# The backend root, not this script's directory.
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_ROOT)

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models.schema import (  # noqa: E402
    BrandDetails,
    ClientInfo,
    CommodityDetails,
    Creds,
    Result,
    SurveyorDetails,
    VendorDetails,
)

DEFAULT_SQLITE_CANDIDATES = [
    os.path.join(BACKEND_ROOT, "..", "eye_compass.db"),
    os.path.join(BACKEND_ROOT, "..", "eye_compass", "eye_compass.db"),
    "/home/nvidia/eye_compass/eye_compass.db",
]


def find_sqlite(explicit=None):
    candidates = [explicit] if explicit else DEFAULT_SQLITE_CANDIDATES
    for path in candidates:
        if path and os.path.exists(path):
            return os.path.abspath(path)
    return None


def parse_blob(raw):
    """Legacy blobs are Python reprs; some may be JSON. Try both."""
    if raw is None:
        return None, "empty"
    if isinstance(raw, (dict, list)):
        return raw, "native"
    text = str(raw).strip()
    if not text:
        return None, "empty"
    try:
        return ast.literal_eval(text), "literal_eval"
    except Exception:
        pass
    try:
        return json.loads(text), "json"
    except Exception:
        return None, "unparseable"


def table_exists(cur, name):
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def rows_of(cur, name):
    if not table_exists(cur, name):
        return []
    cur.execute(f'SELECT * FROM "{name}"')
    return cur.fetchall()


def migrate(sqlite_path, dry_run=False):
    print(f"Legacy database : {sqlite_path}")
    print(f"Target          : {engine.url.render_as_string(hide_password=True)}")
    if dry_run:
        print("Mode            : DRY RUN (nothing will be written)\n")

    Base.metadata.create_all(bind=engine)

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    db = SessionLocal()

    stats = {}
    try:
        # ---------------- reference tables ----------------
        # These are full replacements: they are a cache of the Qualix config,
        # so the legacy contents are the correct starting state.
        simple = [
            ("creds", Creds, lambda r: Creds(user=r["user"], password=r["pass"])),
            (
                "clientinfo",
                ClientInfo,
                lambda r: ClientInfo(
                    client_name=r["client_name"], image_folder_name=r["image_folder_name"]
                ),
            ),
            (
                "surveyordetails",
                SurveyorDetails,
                lambda r: SurveyorDetails(surveyor_id=r["surveyor_id"], name=r["name"]),
            ),
            ("branddetails", BrandDetails, lambda r: BrandDetails(brand_name=r["brand_name"])),
            (
                "vendordetails",
                VendorDetails,
                lambda r: VendorDetails(
                    vendor_name=r["vendor_name"], vendor_code=r["vendor_code"]
                ),
            ),
        ]

        for name, model, build in simple:
            source = rows_of(cur, name)
            if not source:
                stats[name] = 0
                continue
            if not dry_run:
                db.query(model).delete()
                seen = set()
                for row in source:
                    obj = build(row)
                    # branddetails has a uniqueness constraint.
                    if model is BrandDetails:
                        if obj.brand_name in seen:
                            continue
                        seen.add(obj.brand_name)
                    db.add(obj)
                db.commit()
            stats[name] = len(source)

        # ---------------- com_details ----------------
        # analysis and variety were stored as str(list); they are JSON columns now.
        source = rows_of(cur, "com_details")
        if source:
            if not dry_run:
                db.query(CommodityDetails).delete()
            bad = 0
            for row in source:
                analysis, _ = parse_blob(row["analysis"])
                variety, _ = parse_blob(row["variety"])
                if analysis is None and row["analysis"]:
                    bad += 1
                if not dry_run:
                    db.add(
                        CommodityDetails(
                            commodity=row["commodity"],
                            commodity_id=row["commodity_id"],
                            analysis=analysis if isinstance(analysis, list) else [],
                            variety=variety if isinstance(variety, list) else [],
                        )
                    )
            if not dry_run:
                db.commit()
            stats["com_details"] = len(source)
            if bad:
                print(f"  ! com_details: {bad} row(s) had unparseable analysis/variety")
        else:
            stats["com_details"] = 0

        # ---------------- result ----------------
        source = rows_of(cur, "result")
        migrated = skipped = unparseable = 0
        for row in source:
            payload, how = parse_blob(row["result"])
            if payload is None and row["result"]:
                unparseable += 1
                # Keep the row rather than dropping it, but make the failure
                # visible instead of silently storing a wrapper object.
                payload = {"_unparsed": str(row["result"])}

            # Legacy record identity (database.py:433-445), not sample_id alone.
            existing = (
                db.query(Result)
                .filter(
                    Result.sample_id == row["sample_id"],
                    Result.date == row["date"],
                    Result.start_time == row["start_time"],
                    Result.stop_time == row["stop_time"],
                )
                .first()
            )
            if existing:
                skipped += 1
                continue

            if not dry_run:
                db.add(
                    Result(
                        sample_id=row["sample_id"],
                        commodity=row["commodity"],
                        variety=row["variety"],
                        result=payload,
                        date=row["date"],
                        start_time=row["start_time"],
                        stop_time=row["stop_time"],
                        # '0'/'1'/'2' all preserved — '2' means Qualix rejected
                        # the payload and must not be retried forever.
                        sync_status=row["sync_status"],
                    )
                )
            migrated += 1

        if not dry_run:
            db.commit()

        stats["result"] = migrated
        print("\nMigration summary")
        print("-" * 46)
        for name, count in stats.items():
            print(f"  {name:<20} {count:>6}")
        if skipped:
            print(f"  {'(already present)':<20} {skipped:>6}")
        if unparseable:
            print(f"  {'! unparseable blobs':<20} {unparseable:>6}")
        print("-" * 46)
        if dry_run:
            print("Dry run — no changes were written.")
        return True

    except Exception as exc:
        db.rollback()
        print(f"\nMigration failed and was rolled back: {exc}")
        return False
    finally:
        db.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", help="Path to the legacy eye_compass.db")
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing")
    args = parser.parse_args()

    path = find_sqlite(args.sqlite)
    if not path:
        print("Could not find the legacy database. Looked in:")
        for candidate in DEFAULT_SQLITE_CANDIDATES:
            print(f"  {os.path.abspath(candidate)}")
        print("Pass --sqlite /path/to/eye_compass.db")
        sys.exit(1)

    sys.exit(0 if migrate(path, dry_run=args.dry_run) else 1)


if __name__ == "__main__":
    main()
