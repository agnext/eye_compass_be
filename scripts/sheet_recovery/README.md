# Sheet recovery

Batches from the **legacy** devices that never reached the `Raw_Data` tab of the
eye_compass spreadsheet, and the fix that stops it happening again. Nothing here
runs as part of the backend; it is all run by hand.

## What went wrong

The legacy app (`/home/nvidia/eye_compass/sheet_update.py`) adds each finished
batch with gspread's `append_row`, which asks Google to find "the table" and add
after it. On this spreadsheet — years of rows from four devices, with gaps — the
detection settled on **row 4017** and, with gspread's default `OVERWRITE`,
replaced it once per batch instead of appending. Those batches never reached the
sheet, and row 4017's original contents were destroyed.

The new backend (`app/services/sync_service.py`) was never affected: it works
out the target row itself. `device_patch/` gives the legacy devices the same
behaviour.

## Putting the missing batches back

Two steps: a dry run first, then the real run. Both commands work as they stand,
from any folder. Change only the database file name to match the copy you made
(see "Getting the database off a device" below).

The python is the backend's own virtualenv — confusingly named `eye_compass`, but
it is the one `eye-compass-backend.service` uses, not the legacy app's (`m38`).

**1. Dry run — writes nothing.** Lists which batches are missing from the sheet
and the row they would go to. Always do this first.

```
cd /home/nvidia/eye_compass_new/eye_compass_be/scripts/sheet_recovery
/home/nvidia/.virtualenvs/eye_compass/bin/python -I backfill_from_backup.py --db data/eye_compass_blr_004.db
```

**2. Actual run — appends the missing rows to the sheet.** The same command with
`--send` on the end. It takes about a minute per four batches.

```
cd /home/nvidia/eye_compass_new/eye_compass_be/scripts/sheet_recovery
/home/nvidia/.virtualenvs/eye_compass/bin/python -I backfill_from_backup.py --db data/eye_compass_blr_004.db --send
```

Options, added to either command:

| Option | Meaning |
|---|---|
| `--db <file>` | Required. Your copy of the device's database. |
| `--send` | Actually write. Without it nothing is written. |
| `--since YYYY-MM-DD` | Only consider batches from that date on. Default is all of 2026. Example: `--since 2026-09-30` leaves out the August batches. |
| `--limit N` | Stop after N rows. Useful for a first small real run: `--send --limit 2`. |

The database is **your own copy** of the device's (next section). Everything runs
from that copy, so the device is never touched and need not be reachable.

Missing batches are **appended at the end, oldest first**. The tab is not in
time order anyway — four devices append to it independently — and inserting
mid-sheet would shift every row below and break the tabs that reference them.

A batch counts as missing when no row of that device starts at the same second.
Batch names are not used for this: operators reuse them, and one device database
has 98 batches called `sonamasooririce`.

Each row goes to a row checked empty first and is read back afterwards, so a run
can be repeated safely — rows already there are skipped. If a write cannot be
confirmed the run stops rather than carrying on, so the order holds and the next
run resumes where it left off. Writes are paced at 15s to stay inside the Sheets
quota, so expect roughly a minute per four batches.

### Getting the database off a device

Copy `/home/nvidia/eye_compass/eye_compass.db` from the device into the `data/`
folder here, as a file you can recognise (e.g. `data/eye_compass_blr_004.db`).
Any way you like — a USB stick, or from this Jetson over the network:

```
scp nvidia@100.64.145.101:/home/nvidia/eye_compass/eye_compass.db /home/nvidia/eye_compass_new/eye_compass_be/scripts/sheet_recovery/data/eye_compass_blr_004.db
```

(`100.64.145.101` is BLR-004. Check the file is not empty afterwards — a 0-byte
file means the copy did not complete.)

Copying only reads the device and does not disturb the running app. Do it while
no batch is being saved, so the copy is not caught mid-write; between batches is
fine, as is any time the app is stopped.

`data/` is not in git — these are copies of production data.

### Checking before a run

```
cd /home/nvidia/eye_compass_new/eye_compass_be/scripts/sheet_recovery
/home/nvidia/.virtualenvs/eye_compass/bin/python -I test_backfill.py --db data/eye_compass_blr_004.db
```

Replays the whole backfill against a stand-in sheet, so it touches nothing. It
checks that every row lands in order at the end, that no existing row is
touched, that each row is **identical to what the device itself would have
written**, that reused batch names hide nothing, that a row taken by another
writer is left alone, and that a write which does not land is reported as a
failure rather than a success.

## Stopping it happening again

`device_patch/` holds the fix for the legacy devices: `write_row_at_end()`
replaces `append_row` in `sheet_update.py`. `deploy_to_blr004.sh` installs it,
backing the live file up to `sheet_update.py.bak` first and refusing to run if
the device's file is not the version the patch was built against.

Editing the file restarts nothing — the legacy app imports `sheet_update` once
at startup — so the fix takes effect at the next app restart or reboot.

## Still to do

- BLR-004 (DC 004 / CGI3): patch not yet installed; 53 batches to backfill.
- CGI2 (Google) and CGI4 (DC 005) have the same bug: same patch, same backfill.
- Row 4017's original contents are still lost. Its neighbours are 12-13 Sep
  batches and it is not in BLR-004's database, so it belonged to another device,
  whose own database can regenerate it. The cell's edit history also holds every
  version, but there are dozens.
