# Archive SSD

The backend copies the device's raw capture folders to an external SSD once a
day, organised by date. It frees device space by removing samples older than
30 days, but only after every file of the sample has been re-checked against
its SSD copy. Nothing on the SSD is ever deleted automatically.

Each device has its own archive SSD.

Code: `app/services/ssd_archive/`. It runs inside the backend as a background
worker (`worker.SsdArchiveTask`), next to the S3 uploader.

## What is copied, and where to

Device (under `OUTPUT_DIR`):

```
Data_Collection/<commodity>/<variety>/<sample>/...
output_frame/<commodity>/<variety>/<sample>/...
```

SSD:

```
<SSD>/<YYYY-MM-DD>/Data_Collection/<commodity>/<variety>/<sample>/...
<SSD>/<YYYY-MM-DD>/output_frame/<commodity>/<variety>/<sample>/...
```

The date is the sample's own date, read from its folder name:

| Folder | Name | Example | Date |
|---|---|---|---|
| Data_Collection | epoch seconds first | `1789387342_testentry` | 2026-09-14 |
| output_frame | `YYYYMMDDHHMMSS` last | `G11791445078_20261008131152` | 2026-10-08 |

If a name has no date, the oldest file's time is used. Sub-folders inside a
sample, such as `fm_full_frames/`, keep their structure. A sample folder
written without its commodity or variety folder (for example
`output_frame/_20260910025931/`) is archived with the missing level named
`_unsorted`.

## Each run

The worker checks every hour (`SSD_ARCHIVE_CHECK_INTERVAL_SECONDS`) and does
the work once a day. A day missed because the SSD was unplugged or the device
was off is caught up as soon as both are available.

1. Skipped if today's run already succeeded, if a batch scan or data
   collection is running, or if the S3 upload is running.
2. Finds the archive SSD. If it is not connected, nothing happens until the
   next check.
3. Checks the SSD has room for everything not yet on it, plus a 1 GB reserve.
   If not, nothing is copied or removed.
4. **Copy.** Every file is written as `<name>.part`, flushed to disk, checked
   for size and SHA-256, then renamed. Files already on the SSD are skipped.
   Files changed in the last 5 minutes are left for the next run.
5. **Record.** A Data Collection run whose files are all on the SSD is marked
   on its history row (`data_collection_sessions.ssd_archived_at`, `ssd_name`,
   `ssd_path`).
6. **Device clean-up.** A sample dated more than 30 days ago is removed from
   the device only if:
   - none of its files changed in the last 30 days,
   - it is not in use (the active batch, a held batch, the current Data
     Collection run),
   - every file's SSD copy is re-hashed now and matches exactly, and
   - for a Data Collection run, its history row records the SSD copy.

   A missing or damaged SSD copy is copied again and checked again. If it
   still does not match, the whole sample stays. After removal the row gets
   `ssd_removed_at`.
7. Saves the result in the `ssd_archive_state` table. The day counts as done
   only if nothing failed.

A batch scan or data collection starting during a run stops it between files;
the rest is done at the next check. The worker runs in its own thread at lower
CPU priority.

## Safety rules

| Situation | What happens |
|---|---|
| SSD not connected, or more than one marked SSD | Nothing copied or removed |
| SSD full | Nothing copied or removed |
| SSD unplugged during a run | Run stops; device clean-up skipped |
| SSD already has a *different* file with the same name | Neither is overwritten. The device file is saved next to it as `<name>.conflict-<hash8>.<ext>` |
| SSD copy damaged since it was written | Detected before removal; copied again; device copy kept until it verifies |
| Database cannot record the SSD copy | The run stays on the device; tried again next check |
| System clock behind the newest data | Copy runs; device clean-up is skipped |
| Power cut mid-copy | Only a `.part` file can be left; it is replaced on the next run |
| Data on the SSD | **Never deleted automatically** |

## Data Collection History

The history screens show a run from wherever its images are:

| Where the images are | Label | Images shown |
|---|---|---|
| On the device | On Device (plus an **SSD** tag once copied) | From the device |
| Moved to the SSD, SSD connected | On Archive SSD | From the SSD |
| Moved to the SSD, not found on it | Missing from SSD | None. The note says whether the SSD is unplugged or the folder is not on it |
| Gone, never moved to the SSD | Missing from Device | None |
| Uploaded to S3 and removed from the device | Uploaded & Cleaned | None |

## Setting up an SSD

The archiver only uses a drive that carries a marker file
(`.eyecompass_archive`) at its root, so it never writes to a pen drive or a
second disk by mistake. The drive must be mounted (it is mounted
automatically under `/media/nvidia/` while the kiosk desktop session is
logged in) and must not be on the same disk as the data.

1. Plug in the SSD and check its file system: `lsblk -f`. ext4 is preferred.
   exFAT and NTFS also work if the Jetson has drivers for them.
2. Mark it, once, from `eye_compass_be` with the backend's Python:

   ```
   /home/nvidia/.virtualenvs/eye_compass/bin/python -m app.services.ssd_archive init /media/nvidia/<SSD-NAME>
   ```

3. Check it:

   ```
   /home/nvidia/.virtualenvs/eye_compass/bin/python -m app.services.ssd_archive status
   /home/nvidia/.virtualenvs/eye_compass/bin/python -m app.services.ssd_archive dry-run
   ```

   `dry-run` reports what a run would copy and remove, and changes nothing.

### S3 deletion

The S3 uploader also removes these folders from the device after uploading
them (`S3_DELETE_AFTER_UPLOAD=true`, after `S3_RETENTION_DAYS`, 3 by
default). A folder it removes before the archiver has copied it never reaches
the SSD, and shows as Uploaded & Cleaned. On a device with an archive SSD,
set `S3_DELETE_AFTER_UPLOAD=false` in `.env` so the archiver alone frees the
device. Uploads to S3 continue.

## Configuration

Set in `.env` or the service's environment.

| Variable | Default | Meaning |
|---|---|---|
| `SSD_ARCHIVE_ENABLED` | `true` | Run the worker. Does nothing without a marked SSD |
| `SSD_ARCHIVE_CHECK_INTERVAL_SECONDS` | `3600` | How often it checks for the SSD and whether today's run is done |
| `SSD_ARCHIVE_DEVICE_RETENTION_DAYS` | `30` | Device samples older than this are removed once verified |
| `SSD_ARCHIVE_MIN_FILE_AGE_SECONDS` | `300` | Skip files changed more recently than this |
| `SSD_ARCHIVE_RESERVE_GB` | `1` | Free space always left on the SSD |
| `SSD_ARCHIVE_MARKER` | `.eyecompass_archive` | Marker file name at the SSD root |
| `SSD_ARCHIVE_MOUNT_ROOTS` | `/media:/run/media:/mnt` | Where to look for the SSD |

## Logs and state

- The backend log, under the `app.services.ssd_archive` loggers: one summary
  line per run, plus every warning and error.
- The last run: `python -m app.services.ssd_archive status`, or the
  `ssd_archive_state` table.

## Tests

```
USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_ssd_archive.py
USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_dc_history.py
```

Both use temp folders, a fake SSD and a throwaway SQLite database.

## Open points

- The SSD is never cleaned, so it fills up over time. When free space drops
  below the reserve, runs stop copying and removing; old data on the SSD then
  has to be cleared by hand or moved to a new drive.
- Retention counts from the sample's own date (its folder name), not from the
  date it was copied.
- Not yet tested on the device: a real SSD, power loss during a copy, exFAT
  or NTFS mounts.
