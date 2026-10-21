"""Offline checks for backfill_from_backup: nothing here touches Google.

    cd eye_compass_be/scripts/sheet_recovery
    /home/nvidia/.virtualenvs/eye_compass/bin/python -I test_backfill.py --db data/blr004.db

Replays a whole backfill against a stand-in sheet that records every call, so
the failure modes that matter can be forced and the outcome checked: a row
taken by another writer between counting and writing, and a write that reports
success but leaves nothing behind.

The stand-in starts from the Raw_Data header as the live tab has it, plus a few
rows standing in for the thousands already there. Only the device database is
real; --db is your copy of it.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_from_backup as b

logging.basicConfig(level=logging.ERROR)

# The live tab's header, 21 Oct. The backfill places every value by header, so
# this is also what decides each row's width and column order.
HEADER = [
    "sample_id", "Commodity ", "Variety", "weight", "brand", "vendor_name",
    "vendor_code", "manufacturing_date", "receiving_date", "site_code",
    "start_time", "end_time", "Others", "Husk", "Frame Count", "Blower FO",
    "Magnetic FO", "FM Stop Count", "Manual Stop Count", "FM Stop Time",
    "Manual Stop Time", "Total Stop Time", "total_fo_detected",
    "Metal Fragments", "Mud balls", "Thread", "Feathers", "Dried Leaves",
    "Plastic Pieces", "Insects", "Sticks", "Stones", "Paper",
    "Toffee wrappers", "Rubber", "Jute Fibers", "Glass pieces", "FM", "NON-FM",
    "Device_id", "Epoch_Timestamp", "Plant Matter", "Animal Matter",
    "Infestation", "Mould or Fungus",
]
# Rows already in the tab. The third is a batch from the device database, so
# the "already there" check has something real to skip.
EXISTING = [
    ["OLDBATCH1"] + [""] * (len(HEADER) - 1),
    ["OLDBATCH2"] + [""] * (len(HEADER) - 1),
    None,          # filled in below, once the database has been read
]


class FakeSheet:
    """Enough of a gspread worksheet for write_row_at_end, with a tripwire."""

    def __init__(self, values, steal_at=None, drop=()):
        self.values = [list(r) for r in values]
        self.title = "Raw_Data"
        self.steal_at = steal_at      # a row number another writer grabs first
        self.drop = set(drop)         # attempts whose write silently vanishes
        self.writes = 0

    def _pad(self, n):
        while len(self.values) < n:
            self.values.append([])

    def get_all_values(self):
        return self.values

    def row_values(self, n):
        return self.values[n - 1] if n <= len(self.values) else []

    def get(self, a1):
        row = int(a1.split(":")[0][1:])
        return [self.values[row - 1]] if row <= len(self.values) else []

    def update(self, values, range_name):
        row = int(range_name[1:])
        self.writes += 1
        if self.steal_at == row:
            self._pad(row)
            self.values[row - 1] = ["someone else"]
            self.steal_at = None
            return
        if self.writes in self.drop:
            return
        self._pad(row)
        self.values[row - 1] = [str(v) for v in values[0]]


ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True, help="your copy of the device's eye_compass.db")
args = ap.parse_args()
if not os.path.exists(args.db):
    sys.exit("no such database: %s" % args.db)

batches = b.db_batches(args.db, "2026-01-01")
assert batches, "no batches in the database"
print("0. %d batches in %s" % (len(batches), args.db))

# One real batch is placed in the sheet already, so it must be skipped below.
already = batches[0]
EXISTING[2] = b.build_row(HEADER, already[3], already[4])[0]
sheet_rows = [HEADER] + EXISTING

start_col = HEADER.index("start_time")
in_starts = {r[start_col] for r in sheet_rows[1:] if len(r) > start_col}
todo = [(s.get("sample_id", ""), b.build_row(HEADER, s, a)[0], s, a)
        for _, _, _, s, a in batches
        if s.get("process_start_time") not in in_starts]
assert len(todo) == len(batches) - 1, (len(todo), len(batches))
print("1. %d to write; the one already in the sheet was skipped" % len(todo))

# Reused batch names must not hide anything: the sheet row above is called
# sonamasooririce, and so are 97 batches that are NOT in the sheet. Matching on
# sample_id would have dropped all of them.
same_name = [s for _, _, _, s, _ in batches
             if s.get("sample_id") == already[3].get("sample_id")]
assert len(same_name) > 1, "the database no longer has a reused batch name to check"
assert len([t for t in todo if t[0] == already[3].get("sample_id")]) == len(same_name) - 1
print("1b. %d other batches share that batch name and are still written"
      % (len(same_name) - 1))

# Every row is the width of the header, and carries a single device.
dev = HEADER.index("Device_id")
devices = {row[dev] for _, row, _, _ in todo} - {""}
assert len(devices) == 1, devices
for sample_id, row, _, _ in todo:
    assert len(row) == len(HEADER), (sample_id, len(row))
print("2. every row is %d columns wide and carries one device: %s"
      % (len(HEADER), devices.pop()[:8]))

# The whole run.
fake = FakeSheet(sheet_rows)
before = len(fake.values)
original = [list(r) for r in fake.values]
at = [b.write_row_at_end(fake, row, sample_id) for sample_id, row, _, _ in todo]
assert at == list(range(before + 1, before + 1 + len(todo))), at[:5]
print("3. all %d appended in order at rows %d-%d" % (len(at), at[0], at[-1]))

assert fake.values[:before] == original, "an existing row was changed"
print("4. the rows already in the sheet were not touched")

for (sample_id, row, _, _), n in zip(todo, at):
    assert fake.values[n - 1] == [str(v) for v in row]
print("5. every row landed with the values it was given")

# The device's own code, for the same batches, must produce the same values.
for sample_id, row, scan, analysis in todo:
    legacy, extra = b.legacy_row(scan, analysis)
    assert not extra, extra
    assert [str(v) for v in row[:len(legacy)]] == \
           [str("" if v is None else v) for v in legacy], sample_id
print("6. identical to what the device itself would have written")

# Another writer takes the row between the count and the write.
fake = FakeSheet(sheet_rows, steal_at=before + 1)
n = b.write_row_at_end(fake, todo[0][1], todo[0][0])
assert n == before + 2, n
assert fake.values[before] == ["someone else"], "the other writer was overwritten"
print("7. a row taken by another writer is kept; ours goes to the next one")

# A write that reports success but leaves nothing behind.
fake = FakeSheet(sheet_rows, drop=(1, 2, 3))
assert b.write_row_at_end(fake, todo[0][1], todo[0][0]) == 0
print("8. a write that does not land is reported as a failure, not a success")

# Re-running after a complete run finds nothing left to do.
fake = FakeSheet(sheet_rows)
for sample_id, row, _, _ in todo:
    b.write_row_at_end(fake, row, sample_id)
again = {r[0] for r in fake.values[1:]}
assert not [t for t in todo if t[0] not in again]
print("9. re-running finds every batch already present — nothing is duplicated")
print("ALL BACKFILL CHECKS PASSED")
