#!/usr/bin/env python3
"""
Migrate the legacy SQLite database (eye_compass.db) into PostgreSQL.

Run from the backend root:
    python scripts/migrate_sqlite_to_postgres.py --sqlite /path/to/eye_compass.db --dry-run
    python scripts/migrate_sqlite_to_postgres.py --sqlite /path/to/eye_compass.db

Safe to run against a database that already holds data, and safe to run more
than once:

  * Nothing in PostgreSQL is deleted or overwritten. Each legacy row is added
    only if an equal row is not already there (see KEYS below), so a second
    run adds nothing.
  * The legacy file is opened read-only and is never modified.
  * Everything is one transaction: either every row goes in, or — on any
    error — none does.
  * --dry-run does the whole migration and then rolls it back, so its counts
    are exactly what a real run would add.

Legacy formats handled:

  * `result.result`, `com_details.analysis` and `com_details.variety` were
    written with str() of a Python dict/list (single quotes) and read back with
    eval(). They are parsed with ast.literal_eval, falling back to JSON.
  * Records are identified by (sample_id, date, start_time, stop_time), as
    legacy did (database.py:433-445) — sample_id alone is not unique.
  * `creds.pass` was stored in clear text; the new app compares a SHA-256
    digest (app/api/auth.py:_hash), so it is hashed on the way in. Without
    that, offline login with the migrated account would never succeed.
  * `creds` and `clientinfo` are single-row device settings: they are copied
    only when the target table is empty, so an operator who has already signed
    in on the new app keeps their own cached login.

Not migrated, because legacy has no equivalent: batch_details, scan_progress,
sessions, s3_upload_state. Image folders are files, not database rows — copy
the legacy output/ tree to the new OUTPUT_DIR separately; the layout
(output/<commodity>/<variety>/<image_unique_id>/) is the same.
"""

import argparse
import ast
import json
import os
import sqlite3
import sys

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_ROOT)

from app.api.auth import _hash  # noqa: E402
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

LEGACY_TABLES = ("creds", "clientinfo", "surveyordetails", "branddetails",
                 "vendordetails", "com_details", "result")


def find_sqlite(explicit=None):
    candidates = [explicit] if explicit else DEFAULT_SQLITE_CANDIDATES
    for path in candidates:
        if path and os.path.exists(path):
            return os.path.abspath(path)
    return None


def parse_blob(raw):
    """Legacy blobs are Python reprs; some may be JSON. Try both."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except Exception:
        pass
    try:
        return json.loads(text)
    except Exception:
        return None


def clean(value):
    """Legacy columns are untyped: trim strings, keep None as None."""
    if value is None:
        return None
    return str(value).strip()


def rows_of(cur, name):
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    if cur.fetchone() is None:
        return None
    cur.execute(f'SELECT * FROM "{name}"')
    return cur.fetchall()


def add_missing(db, model, source, key_of, build, stats, name):
    """Add each legacy row whose key is not already in the target table.

    Keys already present in PostgreSQL win: they are either the same row from
    an earlier run, or newer data the new app has written since.
    """
    existing = {key_of(obj) for obj in db.query(model).all()}
    added = 0
    for row in source:
        obj = build(row)
        key = key_of(obj)
        if key in existing:
            continue
        existing.add(key)
        db.add(obj)
        added += 1
    stats[name] = (len(source), added)


def add_if_empty(db, model, source, build, stats, name):
    if db.query(model).count():
        stats[name] = (len(source), 0)
        return
    for row in source:
        db.add(build(row))
    stats[name] = (len(source), len(source))


def build_commodity(row, problems):
    analysis = parse_blob(row["analysis"])
    variety = parse_blob(row["variety"])
    if row["analysis"] and not isinstance(analysis, list):
        problems.append(f"com_details {row['commodity']!r}: analysis could not be read")
    if row["variety"] and not isinstance(variety, list):
        problems.append(f"com_details {row['commodity']!r}: variety could not be read")
    return CommodityDetails(
        commodity=clean(row["commodity"]),
        commodity_id=clean(row["commodity_id"]),
        analysis=analysis if isinstance(analysis, list) else [],
        variety=variety if isinstance(variety, list) else [],
    )


def build_result(row, problems):
    payload = parse_blob(row["result"])
    if not isinstance(payload, dict):
        if row["result"]:
            problems.append(f"result ID {row['ID']}: result blob could not be read; "
                            "kept as raw text under '_unparsed'")
            payload = {"_unparsed": str(row["result"])}
        else:
            payload = {}
    sync = clean(row["sync_status"])
    return Result(
        sample_id=clean(row["sample_id"]),
        commodity=clean(row["commodity"]),
        variety=clean(row["variety"]),
        result=payload,
        date=clean(row["date"]),
        start_time=clean(row["start_time"]),
        stop_time=clean(row["stop_time"]),
        # '0' pending, '1' delivered, '2' rejected by Qualix (never retried).
        # Legacy NULL meant "not sent yet".
        sync_status=sync if sync in ("0", "1", "2") else "0",
    )


def result_key(r):
    return (r.sample_id, r.date, r.start_time, r.stop_time)


def migrate(sqlite_path, dry_run=False):
    print(f"Legacy database : {sqlite_path}")
    print(f"Target          : {engine.url.render_as_string(hide_password=True)}")
    print(f"Mode            : {'DRY RUN (rolled back at the end)' if dry_run else 'WRITE'}\n")

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sources = {name: rows_of(cur, name) for name in LEGACY_TABLES}
    if all(v is None for v in sources.values()):
        print("This file has none of the legacy tables — is it the right database?")
        conn.close()
        return False
    for name, rows in sources.items():
        if rows is None:
            print(f"  (legacy table {name} not present — skipped)")
        sources[name] = rows or []

    db = SessionLocal()
    stats, problems = {}, []
    try:
        # Inside the transaction, so a dry run on an empty database rolls the
        # new tables back too (PostgreSQL DDL is transactional).
        Base.metadata.create_all(bind=db.connection())

        add_if_empty(db, Creds, sources["creds"],
                     lambda r: Creds(user=clean(r["user"]),
                                     password=_hash(r["pass"]) if r["pass"] else None),
                     stats, "creds")
        add_if_empty(db, ClientInfo, sources["clientinfo"],
                     lambda r: ClientInfo(client_name=clean(r["client_name"]),
                                          image_folder_name=clean(r["image_folder_name"])),
                     stats, "clientinfo")
        add_missing(db, SurveyorDetails, sources["surveyordetails"],
                    lambda o: (o.surveyor_id, o.name),
                    lambda r: SurveyorDetails(surveyor_id=clean(r["surveyor_id"]),
                                              name=clean(r["name"])),
                    stats, "surveyordetails")
        add_missing(db, BrandDetails, sources["branddetails"],
                    lambda o: o.brand_name,
                    lambda r: BrandDetails(brand_name=clean(r["brand_name"])),
                    stats, "branddetails")
        add_missing(db, VendorDetails, sources["vendordetails"],
                    lambda o: (o.vendor_name, o.vendor_code),
                    lambda r: VendorDetails(vendor_name=clean(r["vendor_name"]),
                                            vendor_code=clean(r["vendor_code"])),
                    stats, "vendordetails")
        # By commodity name: a commodity already present came from a newer
        # Qualix config download and is kept as it is.
        add_missing(db, CommodityDetails, sources["com_details"],
                    lambda o: o.commodity,
                    lambda r: build_commodity(r, problems),
                    stats, "com_details")
        add_missing(db, Result, sources["result"], result_key,
                    lambda r: build_result(r, problems),
                    stats, "result")

        db.flush()

        # Every legacy record must now be findable in the target.
        want = {result_key(build_result(r, [])) for r in sources["result"]}
        have = {result_key(r) for r in db.query(Result).all()}
        lost = want - have
        if lost:
            raise RuntimeError(f"{len(lost)} legacy result(s) not present after insert")

        if dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception as exc:
        db.rollback()
        print(f"\nMigration FAILED — nothing was written: {exc}")
        return False
    finally:
        db.close()
        conn.close()

    print(f"{'table':<18}{'in legacy':>10}{'added':>8}{'already there':>15}")
    print("-" * 51)
    for name, (total, added) in stats.items():
        print(f"{name:<18}{total:>10}{added:>8}{total - added:>15}")
    print("-" * 51)
    for p in problems:
        print(f"  ! {p}")
    print("Dry run — rolled back, nothing written." if dry_run
          else "Committed. Every legacy result is present in PostgreSQL.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", help="Path to the legacy eye_compass.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the migration and roll it back; report what would be added")
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
