# Archive SSD and device clean-up

Two background jobs in the backend copy the device's session folders off
the device:

- the **SSD archiver** (`app/services/ssd_archive/`) copies them to this
  device's own archive SSD once a day, and
- the **S3 uploader** (`app/services/s3_worker.py`) uploads them to S3.

Both record what they have confirmed, per folder, in the `folder_copies`
table (`app/services/folder_copies.py`). Each one reads the other's record
before removing anything from the device. History reads it to show a batch or
collection run that has left the device.

The SSD keeps each sample for `SSD_ARCHIVE_SSD_RETENTION_DAYS` (30) days from
its date, then deletes it, whether or not it reached S3 unless
`SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD` is on. A sample deleted from the SSD
that was never uploaded to S3 is gone for good.

## Who removes data from the device

Set in `.env`:

| Mode | Settings | A folder leaves the device when |
|---|---|---|
| SSD only | `SSD_ARCHIVE_ENABLED=true`, `S3_DELETE_AFTER_UPLOAD=false` | every file is verified on the SSD and it is not in use (after `SSD_ARCHIVE_DEVICE_RETENTION_DAYS`, 0 = at once) |
| SSD and cloud | the same, plus `SSD_ARCHIVE_REQUIRE_CLOUD=true` | as above, and it is also confirmed in S3 |
| Cloud only | `SSD_ARCHIVE_ENABLED=false`, `S3_DELETE_AFTER_UPLOAD=true` | it is older than `S3_RETENTION_DAYS` and every file is confirmed in S3 |
| Keep everything | both off | never (except the low-disk safety net) |

With both `SSD_ARCHIVE_ENABLED` and `S3_DELETE_AFTER_UPLOAD` on, the S3
uploader removes only folders the SSD already holds, so the SSD archive has no
gaps. S3 uploads continue in every mode; only removal changes.

S3 uploads can take a long time when a device is offline. In SSD only mode, a
folder leaves the device as soon as it is verified on the SSD; whatever S3 has
uploaded by then stays in S3. To stop using the SSD at any point, switch to
cloud only.

`SSD_ARCHIVE_ENABLED` is off by default, so a device without an archive SSD
keeps working as before.

## What is copied, and where to

Device (under `OUTPUT_DIR`):

```
Data_Collection/<commodity>/<variety>/<sample>/...
output_frame/<commodity>/<variety>/<sample>/...
output/<commodity>/<variety>/<sample>/...      (batch crops, SSD_ARCHIVE_INCLUDE_CROPS)
```

SSD:

```
<SSD>/<YYYY-MM-DD>/<Data_Collection|output_frame|output>/<commodity>/<variety>/<sample>/...
```

The date is the sample's own date, read from its folder name:

| Folder | Name | Example | Date |
|---|---|---|---|
| Data_Collection | epoch seconds first | `1789387342_testentry` | 2026-09-14 |
| output_frame, output | `YYYYMMDDHHMMSS` last | `G11791445078_20261008131152` | 2026-10-08 |

If a name has no date, the oldest file's time is used. Sub-folders inside a
sample, such as `fm_full_frames/`, keep their structure. A sample folder
written without its commodity or variety folder (for example
`output_frame/_20260910025931/`) is archived with the missing level named
`_unsorted`.

## Each archive run

The worker checks every hour (`SSD_ARCHIVE_CHECK_INTERVAL_SECONDS`) and does
the work once a day. A day missed because the SSD was unplugged or the device
was off is caught up as soon as both are available.

1. Put off if a batch scan or data collection is running, or the S3 upload is
   running.
2. The low-disk check (below).
3. Skipped if today's run already succeeded.
4. Finds the archive SSD. If it is not connected, nothing else happens.
5. **SSD clean-up.** Samples whose date folder is more than
   `SSD_ARCHIVE_SSD_RETENTION_DAYS` days old are deleted from the SSD (with
   `SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD`, only those confirmed in S3), and
   recorded as deleted. Only the archiver's own layout is touched:
   `<YYYY-MM-DD>/<source>/<commodity>/<variety>/<sample>/`. Other folders on
   the drive are left alone.
6. Checks the SSD has room for everything not yet on it, plus a 1 GB reserve.
   If not, nothing is copied or removed.
7. **Copy.** Every file is written as `<name>.part`, flushed to disk, checked
   for size and SHA-256, then renamed. Files already on the SSD are skipped.
   Files changed in the last 5 minutes are left for the next run, and folders
   in use are not copied until they are closed. A device sample already older
   than the SSD keeps samples is not copied (it would be deleted from the SSD
   at once); it stays on the device and a warning is logged.
8. **Record.** A sample whose files are all on the SSD is recorded in
   `folder_copies` (drive, path on it, time).
9. **Device clean-up.** Once `SSD_ARCHIVE_DEVICE_RETENTION_DAYS` have passed
   since a sample was taken (0, the default: straight away), it is removed
   from the device only if:
   - none of its files changed within that period or the last 5 minutes,
   - it is not in use (the active batch, a held batch, the current Data
     Collection run),
   - with `SSD_ARCHIVE_REQUIRE_CLOUD`, it is confirmed in S3 since it last
     changed,
   - every file's SSD copy is re-hashed now and matches exactly, and
   - its SSD copy is recorded in the database.

   A missing or damaged SSD copy is copied again and checked again. If it
   still does not match, the whole sample stays.
10. Saves the result in the `ssd_archive_state` table. The day counts as done
    only if nothing failed.

A batch scan or data collection starting during a run stops it between files;
the rest is done at the next check. The worker runs in its own thread at lower
CPU priority.

## Low-disk safety net

The normal clean-up cannot always run: the SSD may be unplugged for weeks, the
internet may be down, or removal may be switched off. While the device has
less than `DEVICE_LOW_DISK_FREE_PERCENT` free (10% by default), the oldest
samples that have a confirmed copy are removed early, until it is 5 points
above that. A sample counts as copied if it is:

- on the connected SSD, re-verified file by file now, or
- with the SSD unplugged, recorded on the SSD since it last changed, or
- confirmed in S3 since it last changed.

A sample with no copy anywhere is never removed, nor one in use. If space is
still low after that, an error is logged: connect the SSD or the internet.
The check runs every hour, also with the SSD archive off. `0` turns it off.

## Rules both jobs follow

| Situation | What happens |
|---|---|
| SSD not connected, or more than one marked SSD | Nothing copied; removal waits (S3 too, while the SSD archive is on) |
| SSD full | Nothing copied or removed by the archiver |
| SSD unplugged during a run | Run stops; device clean-up skipped |
| SSD already has a *different* file with the same name | Neither is overwritten. The device file is saved next to it as `<name>.conflict-<hash8>.<ext>` |
| SSD copy damaged since it was written | Detected before removal; copied again; device copy kept until it verifies |
| Database cannot record the SSD copy | The folder stays on the device; tried again next check |
| Folder changed after it was copied or uploaded | That copy no longer counts until it is confirmed again |
| S3 upload and archive run due together | Never at the same time, also with `scripts/s3_upload_now.py` (lock file `.device_files.lock`) |
| Folder already fully uploaded and unchanged | The S3 uploader does not check it file by file again |
| System clock behind the newest data | Copy runs; device clean-up is skipped |
| Power cut mid-copy | Only a `.part` file can be left; it is replaced on the next run |
| Data on the SSD | Deleted after `SSD_ARCHIVE_SSD_RETENTION_DAYS` (0 = never) |

## History

Data Collection History and batch History show a folder from wherever its
images are, so changing `SSD_ARCHIVE_DEVICE_RETENTION_DAYS` (for example to
fewer days than `HISTORY_WINDOW_DAYS`) does not leave History rows without
images.

| Where the images are | Label | Images shown |
|---|---|---|
| On the device | On Device | From the device |
| Left the device, SSD connected | On Archive SSD | From the SSD |
| Left the device, not readable on the SSD, in S3 | In Cloud | None |
| Copied to the SSD, not found on it now, not in S3 | Missing from SSD | None. The note says whether the SSD is unplugged or the folder is not on it |
| Deleted from the SSD after its retention, not in S3 | Deleted from SSD | None |
| Gone, no copy recorded | Missing from Device | None |

Data Collection History also tags each run with its other copies (SSD,
Cloud, Partly uploaded) and, on the run's page, when and why it was removed
from the device. Batch History's record view shows the crops from the SSD
with a note saying so, or says where they went.

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

3. Set `SSD_ARCHIVE_ENABLED=true` in `.env` (and `S3_DELETE_AFTER_UPLOAD=false`
   for SSD only mode), then restart the backend when the device is not in use.
4. Check it:

   ```
   /home/nvidia/.virtualenvs/eye_compass/bin/python -m app.services.ssd_archive status
   /home/nvidia/.virtualenvs/eye_compass/bin/python -m app.services.ssd_archive dry-run
   ```

   `dry-run` reports what a run would copy and remove, and changes nothing.

## Configuration

Set in `.env` or the service's environment.

| Variable | Default | Meaning |
|---|---|---|
| `SSD_ARCHIVE_ENABLED` | `false` | Copy to the archive SSD and remove what is verified there |
| `SSD_ARCHIVE_DEVICE_RETENTION_DAYS` | `0` | Days a sample stays on the device before removal; 0 = as soon as verified on the SSD |
| `SSD_ARCHIVE_SSD_RETENTION_DAYS` | `30` | Days a sample stays on the SSD; 0 = never deleted from it |
| `SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD` | `false` | Delete from the SSD only what is confirmed in S3 |
| `SSD_ARCHIVE_INCLUDE_CROPS` | `true` | Also copy `output/` (batch crops) |
| `SSD_ARCHIVE_REQUIRE_CLOUD` | `false` | Remove only once also confirmed in S3 |
| `DEVICE_LOW_DISK_FREE_PERCENT` | `10` | Low-disk safety net threshold; `0` = off |
| `SSD_ARCHIVE_CHECK_INTERVAL_SECONDS` | `3600` | How often the worker checks |
| `SSD_ARCHIVE_MIN_FILE_AGE_SECONDS` | `300` | Skip files changed more recently than this |
| `SSD_ARCHIVE_RESERVE_GB` | `1` | Free space always left on the SSD |
| `SSD_ARCHIVE_MARKER` | `.eyecompass_archive` | Marker file name at the SSD root |
| `SSD_ARCHIVE_MOUNT_ROOTS` | `/media:/run/media:/mnt` | Where to look for the SSD |
| `S3_DELETE_AFTER_UPLOAD` | `true` | The S3 uploader removes what it confirmed (see the modes above) |

`output/` is held on the device for at least `HISTORY_WINDOW_DAYS` by the S3
uploader while it removes data, because S3 copies cannot be shown in History.
The SSD archiver does not hold it back: History shows it from the SSD.

## Logs and state

- The backend log, under `app.services.ssd_archive` and
  `app.services.s3_worker`: one summary line per run, plus every warning and
  error.
- The last archive run: `python -m app.services.ssd_archive status`, or the
  `ssd_archive_state` table.
- Per folder: the `folder_copies` table.

## Tests

```
USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_ssd_archive.py
USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_dc_history.py
USE_MOCK_CAMERA=true /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_s3_worker.py
```

All use temp folders, a fake SSD, a fake S3 and a throwaway SQLite database.

## Open points

- With the device cleared as soon as data is on the SSD, S3 (which uploads
  only from the device, after `S3_RETENTION_DAYS`) may never get it. With
  `SSD_ARCHIVE_SSD_DELETE_REQUIRE_CLOUD=false`, such data exists only on the
  SSD and is gone after 30 days. To keep a cloud copy, also set
  `SSD_ARCHIVE_REQUIRE_CLOUD=true` (the device waits for S3), or lower
  `S3_RETENTION_DAYS`.
- Both retentions count from the sample's own date (its folder name), not
  from the date it was copied. A wrong system clock that jumps ahead would
  make the SSD clean-up delete early.
- If the SSD fills up before 30 days, runs stop copying and removing until
  the clean-up frees room.
- Not yet tested on the device: a real SSD, power loss during a copy, exFAT
  or NTFS mounts.
