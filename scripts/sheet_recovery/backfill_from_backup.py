"""Backfill batches from a legacy device that never reached the Raw_Data tab.

    cd eye_compass_be/scripts/sheet_recovery
    PY=/home/nvidia/.virtualenvs/eye_compass/bin/python   # the backend's own venv

    $PY -I backfill_from_backup.py --db data/blr004.db          # dry run
    $PY -I backfill_from_backup.py --db data/blr004.db --send   # append them

--db is a copy of the device's eye_compass.db, taken by hand — see the README.
Everything runs from that copy, so the device is never touched and need not be
reachable. The same script works for any legacy device's database.

Why rows are missing: the legacy app calls gspread's append_row, which asks
Google to find "the table" and add after it. On this spreadsheet that detection
settled on row 4017 and OVERWROTE it, once per batch, instead of appending.
Fifty batches were lost that way. (sheet_update.py.new fixes the device; this
script recovers what was already lost.)

Where the rows go: appended at the end of Raw_Data, oldest batch first. The tab
is not in time order anyway — four devices append to it independently — and
inserting mid-sheet would shift every row below, breaking the other tabs that
reference them.

Nothing here is destructive: every write goes to a row verified empty first,
and is read back afterwards. Re-running is safe; rows already in the sheet are
skipped.
"""
import argparse
import ast
import logging
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))   # eye_compass_be
DATA = os.path.join(HERE, "data")                  # pulled copies, not in git
TAB = "Raw_Data"
PACE_S = 15                 # ~4 Sheets calls per row; the quota is 60 reads/min
SHEET_WRITE_ATTEMPTS = 3

# scan_data key -> sheet column header, as the device's own process_raw_data
# writes them (sheet_update.py:125-160).
SCAN_COLUMNS = [
    ("sample_id", "sample_id"),
    ("commodity_name", "Commodity"),
    ("variety_name", "Variety"),
    ("weight", "weight"),
    ("brand", "brand"),
    ("vendor_name", "vendor_name"),
    ("vendor_code", "vendor_code"),
    ("manufacturing_date", "manufacturing_date"),
    ("receiving_date", "receiving_date"),
    ("site_code", "site_code"),
    ("process_start_time", "start_time"),
    ("process_end_time", "end_time"),
    ("device_id", "Device_id"),
    ("epoch_timestamp", "Epoch_Timestamp"),
]

log = logging.getLogger("backfill-004")


def db_batches(path, since):
    """Every batch in the device DB, oldest first, duplicates removed.

    The device re-ran one batch into the table twice (IDs 230/231, identical
    sample_id and start time); without this they would become two sheet rows.
    """
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT ID, date, start_time, result FROM result "
        "WHERE date >= ? ORDER BY date, start_time, ID", (since,)
    ).fetchall()
    con.close()

    out, seen = [], set()
    for r in rows:
        try:
            data = ast.literal_eval(r["result"])
        except (ValueError, SyntaxError) as exc:
            log.error("ID %s (%s %s): result column is not readable — %s",
                      r["ID"], r["date"], r["start_time"], exc)
            continue
        scan = data.get("scan_data") or {}
        key = (scan.get("sample_id", ""), scan.get("process_start_time", ""))
        if key in seen:
            log.info("ID %s %s: same batch as an earlier row — skipping the copy",
                     r["ID"], key[0])
            continue
        seen.add(key)
        out.append((r["ID"], r["date"], r["start_time"], scan, data.get("analysis") or []))
    return out


def build_row(header, scan, analysis):
    """One sheet row, placed by COLUMN HEADER rather than by position.

    Same values the device's process_raw_data produces (checked batch by batch
    in --verify), but a column added or moved in the sheet cannot shift the
    rest: anything the header asks for and this batch does not have is left
    blank, and anything the batch has that the header does not is reported
    rather than appended to the end of the row.
    """
    by_header = {}
    for key, column in SCAN_COLUMNS:
        value = scan.get(key)
        by_header[column] = "" if value is None else value
    for item in analysis:
        by_header[item["analysisName"]] = item["totalAmount"]

    row, placed = [], set()
    for column in header:
        name = column.strip()
        if name in by_header:
            row.append(by_header[name])
            placed.add(name)
        elif name in analysis_dict_names():
            # A count this batch did not report. Zero, not blank: that is what
            # the device writes and what the rows already in the sheet hold,
            # and the charts sum these columns.
            row.append(0)
        else:
            # A column this device never measured (Plant Matter and the three
            # after it). Blank, exactly as its own rows already look.
            row.append("")
    missing = sorted(set(by_header) - placed)
    return row, missing


def legacy_row(scan, analysis):
    """What sheet_update.process_raw_data would have sent, for comparison."""
    analysis_dict = {a["analysisName"]: a["totalAmount"] for a in analysis}
    row = [scan.get(k, "") for k, _ in SCAN_COLUMNS[:12]]
    for name in ["Others", "Husk", "Frame Count", "Blower FO", "Magnetic FO",
                 "FM Stop Count", "Manual Stop Count", "FM Stop Time",
                 "Manual Stop Time", "Total Stop Time", "total_fo_detected",
                 "Metal Fragments", "Mud balls", "Thread", "Feathers",
                 "Dried Leaves", "Plastic Pieces", "Insects", "Sticks",
                 "Stones", "Paper", "Toffee wrappers", "Rubber", "Jute Fibers",
                 "Glass pieces", "FM", "NON-FM"]:
        row.append(analysis_dict.get(name, 0))
    extra = [a["analysisName"] for a in analysis if a["analysisName"] not in analysis_dict_names()]
    row.append(scan.get("device_id"))
    row.append(scan.get("epoch_timestamp"))
    return row, extra


def analysis_dict_names():
    return {"Others", "Husk", "Frame Count", "Blower FO", "Magnetic FO",
            "FM Stop Count", "Manual Stop Count", "FM Stop Time",
            "Manual Stop Time", "Total Stop Time", "total_fo_detected",
            "Metal Fragments", "Mud balls", "Thread", "Feathers",
            "Dried Leaves", "Plastic Pieces", "Insects", "Sticks", "Stones",
            "Paper", "Toffee wrappers", "Rubber", "Jute Fibers",
            "Glass pieces", "FM", "NON-FM"}


def open_tab():
    """The Raw_Data tab, through the backend's own credentials."""
    sys.path.insert(0, BACKEND)
    import gspread
    from google.oauth2.service_account import Credentials
    from app.core.config import settings

    path = settings.resolve_sheets_credentials()
    if not path:
        sys.exit("credentials file not found — check SHEETS_CREDENTIALS_FILE")
    if len(settings.SHEETS_SPREADSHEET_ID) != 44:
        sys.exit("SHEETS_SPREADSHEET_ID is %d characters, expected 44"
                 % len(settings.SHEETS_SPREADSHEET_ID))
    creds = Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    client.set_timeout(30)
    # By title, not sheet1: a reordered tab would otherwise send these rows
    # into whichever tab happened to be first.
    return client.open_by_key(settings.SHEETS_SPREADSHEET_ID).worksheet(TAB)


def write_row_at_end(sheet, row, label):
    """Append `row` after the last used row and prove it landed. Row number, or 0.

    The same routine the device is being patched to use: count the rows, refuse
    a row that already holds anything, write to an explicit range, read it back.
    """
    from gspread.utils import rowcol_to_a1

    last_column = rowcol_to_a1(1, len(row)).rstrip("1")
    for attempt in range(1, SHEET_WRITE_ATTEMPTS + 1):
        target = len(sheet.get_all_values()) + 1
        existing = sheet.get("A%d:%s%d" % (target, last_column, target))
        if existing and any(str(c).strip() for line in existing for c in line):
            log.warning("row %s was taken by another writer (attempt %s/%s) — recounting",
                        target, attempt, SHEET_WRITE_ATTEMPTS)
            continue

        sheet.update(values=[row], range_name="A%d" % target)

        written = sheet.row_values(target)
        if written and written[0] == str(row[0]):
            log.info("%s -> row %s", label, target)
            return target
        log.warning("wrote %s to row %s but read back %r (attempt %s/%s)",
                    label, target, (written or [""])[0], attempt, SHEET_WRITE_ATTEMPTS)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True,
                    help="your copy of the device's eye_compass.db")
    ap.add_argument("--since", default="2026-01-01", help="earliest batch date (YYYY-MM-DD)")
    ap.add_argument("--send", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--limit", type=int, help="stop after this many rows")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("urllib3", "google", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not os.path.exists(args.db):
        sys.exit("no such database: %s (copy it off the device first — see README)" % args.db)
    batches = db_batches(args.db, args.since)
    if not batches:
        sys.exit("no batches in %s since %s" % (args.db, args.since))
    devices = {s.get("device_id") for _, _, _, s, _ in batches if s.get("device_id")}
    log.info("%s: %d batches since %s, device id %s",
             args.db, len(batches), args.since, ", ".join(sorted(devices)) or "(none recorded)")
    if len(devices) > 1:
        sys.exit("this database holds more than one device id — stopping")

    sheet = open_tab()
    header = sheet.row_values(1)
    values = sheet.get_all_values()
    log.info("sheet: tab %r, %d rows, %d columns", sheet.title, len(values), len(header))

    # A batch is already in the sheet when a row of THIS device starts at the
    # same second. Not sample_id: operators reuse batch names freely — 98 of
    # these batches are called "sonamasooririce" — so one row of that name
    # would hide every one of them. Rows that predate the Device_id column are
    # counted as this device's, which can only cause a batch to be left out,
    # never written twice.
    device = (devices or {""}).copy().pop()
    start_col = header.index("start_time")
    device_col = header.index("Device_id")

    def cell(row, i):
        return row[i] if len(row) > i else ""

    in_sheet_starts = {cell(r, start_col) for r in values[1:]
                       if cell(r, device_col) in ("", device)}

    todo, problems = [], 0
    for batch_id, date, start, scan, analysis in batches:
        sample_id = scan.get("sample_id", "")
        start_time = scan.get("process_start_time", "")
        if start_time in in_sheet_starts:
            continue
        row, missing = build_row(header, scan, analysis)
        legacy, extra = legacy_row(scan, analysis)
        notes = []
        if missing:
            notes.append("no column for %s" % missing)
        if extra:
            notes.append("analysis the sheet has no column for: %s" % extra)
        if [str(v) for v in row[:len(legacy)]] != [str("" if v is None else v) for v in legacy]:
            notes.append("differs from what the device would have written")
        if not start_time:
            notes.append("no start time")
        problems += bool(notes)
        log.info("  %-5s %s %s %-34s %s", "CHECK" if notes else "PLAN", date, start,
                 sample_id, "; ".join(notes) or "")
        todo.append((sample_id, row, notes))

    if args.limit:
        todo = todo[:args.limit]
    log.info("%d batches missing from the sheet, %d with something to look at",
             len(todo), problems)
    if not todo:
        return
    log.info("they would be appended after row %d, oldest first", len(values))

    if not args.send:
        log.info("dry run — nothing written. Re-run with --send to append them.")
        return
    if problems:
        sys.exit("refusing to send while any batch is flagged above")

    sent = 0
    for n, (sample_id, row, _) in enumerate(todo, 1):
        for attempt in range(1, 6):
            try:
                target = write_row_at_end(sheet, row, sample_id)
            except Exception as exc:
                target = 0
                log.warning("%s: %s", sample_id, exc)
            if target:
                break
            wait = 70 * attempt
            log.warning("%s not written (attempt %d) — waiting %ds (rate limit?)",
                        sample_id, attempt, wait)
            time.sleep(wait)
        if not target:
            # Stopping keeps the remaining batches in scan order; re-running
            # picks up exactly where this left off.
            sys.exit("[%d/%d] FAILED %s — stopping; %d were written"
                     % (n, len(todo), sample_id, sent))
        sent += 1
        log.info("[%d/%d] sent %s", n, len(todo), sample_id)
        time.sleep(PACE_S)
    log.info("DONE: %d of %d written", sent, len(todo))


if __name__ == "__main__":
    main()
