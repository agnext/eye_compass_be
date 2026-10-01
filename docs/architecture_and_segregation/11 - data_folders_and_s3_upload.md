# 11. Data Folders & S3 Upload Reference

What gets written where on disk, when, and what actually makes it to S3.
Reference doc, not a change log — nothing here was modified as part of
writing it.

All paths are rooted at `settings.OUTPUT_DIR` (`app/core/config.py`), which
defaults to `<backend_root>/output` unless overridden by env, `.env`, or
legacy's `config.INI`.

## Short version

- **Data Collection folder** — raw, untouched frames from the Data
  Collection tool, one file per grabbed frame while recording is on. No
  inference, no crops.
- **Output folder** — a batch's FM/NON-FM crop images, plus its
  `result.json`. One folder per batch.
- **Output frame folder** — a batch's full frames, three kinds:
  `r_frame_N.jpg` (every 2nd frame with nothing in it — the clean belt, and
  what "Frame Count" counts), `fm/` (every frame the model found anything in,
  with YOLO labels) and `fm_full_frames/` (one frame per counted FM).
- **Rejected folder** — a cancelled batch's `output/` folder contents,
  *moved* here, or deleted instead with `REJECTED_SAVE_ENABLED=false`. Never
  contains full frames — `output_frame/` isn't touched by Cancel at all.
- **S3** — every 3 days, anything in `output/`, `output_frame/` and
  `Data_Collection/` past its retention window is uploaded, and deleted from
  the device once S3's copy is confirmed identical. `S3_RETENTION_DAYS`
  (default 3) for the two big trees; `output/` is held for at least
  `HISTORY_WINDOW_DAYS` (30) so History never lists a batch whose images are
  gone. `rejected/` is never uploaded.

## Detailed

### 1. Data Collection folder

```
<OUTPUT_DIR>/Data_Collection/<commodity>/<variety>/<epoch>_<sample_id>/
```

Created once per visit to the Data Collection page
(`app/api/camera.py:483-489`, port of legacy's `goto_dc_page`,
`main.py:1843-1862`).

Frames are written continuously for as long as `_dc_recording` is `True`,
inside the `/ws/data_collection/stream` loop's `grab_and_save()`
(`camera.py:356-367`). Two ways to turn recording on:
- **Start/Stop** (`POST /api/camera/data_collection/start`,
  `camera.py:496-515`) — records until explicitly stopped, also starts the
  belt (port of `start_dc`, `main.py:1864-1869`).
- **Capture** (`POST /api/camera/data_collection/capture`,
  `camera.py:528-545`) — records for a fixed 2 seconds then auto-stops, does
  not touch the conveyor (port of `capture_image_dc`, `main.py:2004-2010`).

Every frame grabbed during that window is saved via a direct `cv2.imwrite`
with **no color conversion and no call into `_inference` anywhere** —
confirmed raw and untouched, matching the route's own docstring
(`camera.py:76-82`).

Filename: `<epoch>_<sample_id>_<frame_epoch>.png` (`camera.py:364-366`).

### 2. Output folder

```
<OUTPUT_DIR>/output/<commodity_slug>/<variety_slug>/<sample_id>_<YYYYMMDDHHMMSS>/
```

(`ScanSession.start()`, `scan_session.py:160-163`; `_slug()` lowercases and
replaces spaces with underscores, `scan_session.py:50-51`.) Created once at
batch Start.

Files land here only when a detection is *resolved*, not on every frame —
two writers:

1. **Operator taps a box and picks an FM type** — `label_detection()`
   (`scan_session.py:402-436`, port of `main.py:232-259/141-147/1343-1360`)
   crops the frozen frame and saves `<FMType>_<epoch_ms>.png`
   (`scan_session.py:429-432`).
2. **Operator dismisses the detection without tapping a box** —
   `save_unselected()` (`scan_session.py:438-459`, port of
   `main.py:1325-1341`), called from `resume()`, auto-saves every
   still-unlabelled box as `NON-FM_<epoch_ms>_<box_index>.png`
   (`scan_session.py:454-456`).

`finish()` also writes `result.json` into this same folder
(`scan_session.py:582-585`) — the assembled per-class counts, dates, and
looker_data metrics. `create_results()` (`scan_session.py:517-533`) counts
these crop files by filename prefix to build the Item/Count breakdown shown
on the results screen — an exact port of legacy's own file-counting
`create_results` (`main.py:1372-1452`).

### 3. Output frame folder

```
<OUTPUT_DIR>/output_frame/<commodity_slug>/<variety_slug>/<sample_id>_<timestamp>/
    r_frame_<n>.jpg
    fm/
        frame_<n>.png / .txt / .conf
        low_confidence_frames/low_confidence_frame_<n>.png / .txt / .conf
    fm_full_frames/
        frame_<index>.png / .txt / .conf
```

Same subpath shape as `output/` (built alongside it in `ScanSession.start()`),
but a sibling tree with different content. All three are written from
`ScanSession.process_frame` and the review screen, on a background writer
thread — see `12 - object_capture_and_detection.md` §5.

- **`r_frame_<n>.jpg`** — every `RAW_FRAME_EVERY`-th (default 2) frame the
  model found nothing in, JPEG quality 95 (`save_raw_frame`, port of
  `save_raw_image`, `main.py:2413-2441`). `update_fm_count()` counts these
  files and reports the count as `"Frame Count"`, which is posted to Qualix.
- **`fm/`** — every frame the model found anything in: the full frame as PNG,
  a YOLO label file (`class x y w h`, normalised) and a confidence file in the
  same row order (`save_fm_training_artifacts`, port of `save_image`,
  `main.py:2474-2533`). Frames a commodity suppression rule discarded go to
  `low_confidence_frames/`. Off with `FM_FRAMES_ENABLED=false`.
- **`fm_full_frames/`** — exactly one frame per FM on the review list, i.e.
  one per crop in `output/` and per unit of `total_fo_detected`; `<index>`
  matches the number the crop's filename ends in, and the label file holds
  only that FM's box (`save_fm_full_frames`). Off with
  `FM_FULL_FRAMES_ENABLED=false`.

### 4. Rejected folder

```
<OUTPUT_DIR>/rejected/<commodity>/<variety>/<sample_id>_<timestamp>/
```

Only touched by `ScanSession.cancel()` (`scan_session.py:488-511`, port of
`cancel_result`, `main.py:2064-2084`) — Cancel Batch / discard-pending-result.

Every regular file currently in that batch's `output/` folder is **moved**
(`shutil.move`, not copied — nothing is deleted, no originals left behind)
into the rejected folder. With `REJECTED_SAVE_ENABLED=false` those same files
are deleted instead and no rejected folder is created. So a rejected batch's folder holds the same kind
of content `output/` would have — FM/NON-FM crop images (and, if the batch
got that far, `result.json`) — **never raw full frames**: `output_frame/`
is completely untouched by `cancel()`, in both legacy and this port.

Confirmed real quirk, present in legacy too and deliberately kept as-is
(`scan_session.py:494-497`): the rejected path's commodity/variety segments
are used **raw**, not slugified — while `output/`'s equivalent segments
are lowercased/underscored. So a cancelled batch's rejected folder can have
different casing/spacing in its path than its own `output/` folder would
have had. This exactly mirrors legacy's own inconsistency between
`cancel_result` (`main.py:2070`, raw `currentText()`) and `start_process`
(`main.py:779`, slugified) — not a port-introduced bug.

### 5. S3 upload (`app/services/s3_worker.py`)

Started at app startup if `settings.S3_ENABLED`. Every
`S3_UPLOAD_EVERY_DAYS` (default 3) it uploads everything under three trees:

```
output/  output_frame/  Data_Collection/
```

and deletes each file from the device once S3 is confirmed to hold an
identical copy (`S3_DELETE_AFTER_UPLOAD`, default on). `rejected/` is never
uploaded or deleted.

**Schedule.** The worker wakes every `S3_CHECK_INTERVAL_SECONDS` (default an
hour) and reads the `s3_upload_state` table (one row, created automatically by
`create_all()` on the next startup — no migration needed). A run starts only when
the last *completed* run is `S3_UPLOAD_EVERY_DAYS` old, so restarts don't
reset the count. The first time the worker starts it only records the date,
and the first run happens `S3_UPLOAD_EVERY_DAYS` after that. A run that fails
(no internet, five failures in a row) or is put off (a scan or data collection
in progress) leaves the date alone, so it is tried again at the next hourly
check. If the clock is behind the recorded date, the run goes ahead rather
than waiting.

**Retention — what stays on the device.** `S3_RETENTION_DAYS` (default 3) is
the real control over what gets removed: a batch or collection folder is only
uploaded and deleted once **nothing in it has changed** for that long, so the
most recent few days are always on the device. Age comes from the newest
modification time anywhere in the folder, not the batch's start time, so a
batch whose crops were reclassified yesterday counts as a day old.

**`output/` is held longer, for History's sake.** It is the only tree the UI
reads — the record view loads a batch's crops straight out of it — so removing
them while History still lists that batch leaves a row that opens empty.
`output/` therefore uses **at least `HISTORY_WINDOW_DAYS`** (30), making
"if History lists it, its images are still here" true by construction, and
still true if `HISTORY_WINDOW_DAYS` is changed later. This costs almost nothing:
`output/` is crops and `result.json` only — 14 MB against 2.5 GB across the
other two trees on this device. `output_frame/` and `Data_Collection/` are read
by nothing in the UI and keep the short window.

`HISTORY_WINDOW_DAYS=0` means History shows *everything*, which no finite
retention can cover; `output/` then falls back to `S3_RETENTION_DAYS` and older
rows show the "uploaded to cloud storage" notice instead of crops.

| Tree | Read by the UI | Kept for |
|---|---|---|
| `output/` | History record view | `max(S3_RETENTION_DAYS, HISTORY_WINDOW_DAYS)` — 30 days |
| `output_frame/` | nothing | `S3_RETENTION_DAYS` — 3 days |
| `Data_Collection/` | nothing | `S3_RETENTION_DAYS` — 3 days |

Note the two periods are independent and both default to 3:

| | Meaning |
|---|---|
| `S3_UPLOAD_EVERY_DAYS` | how often a run happens |
| `S3_RETENTION_DAYS` | how much data always stays on the device |

Because a run only comes round every `S3_UPLOAD_EVERY_DAYS`, a batch can be up
to the two added together (6 days at the defaults) before it actually goes.
Lowering `S3_UPLOAD_EVERY_DAYS` tightens that and is safe — retention, not the
schedule, is what protects recent data.

**What is never touched.** Work is done per batch / collection folder (three
levels under each tree). Skipped:
- anything inside the retention window above;
- the current batch's `output/` and `output_frame/` folders, as long as the
  scan session still refers to them (including the results page after Submit,
  which can still reclassify crops);
- the current Data Collection folder;
- every held or interrupted batch (and the active one), from the
  `scan_progress` table — a held batch is continued in its own folders, so
  removing them would take its FMs and frames away before it is finished. If
  the database cannot be read the run is abandoned rather than guessing. See
  `13 - held_batches_and_power_cut_recovery.md`;
- any folder with anything changed in the last `S3_MIN_AGE_MINUTES` (default
  30) — a floor under retention, and the guard that still applies if
  `S3_RETENTION_DAYS` is set to 0.

If a scan or data collection starts during a run, the run stops before the
next file and carries on at a later check.

**Per file, in order:**
1. SHA-256 the file, checking it didn't change while being read.
2. If S3 already has the key with the same SHA-256 and size, skip the upload.
   Otherwise `PutObject` with `ChecksumSHA256` attached. S3 checks the bytes
   against it and refuses the upload if they differ. Always a single
   request, never multipart, so the stored checksum is the file's own.
3. `HeadObject` (with `ChecksumMode=ENABLED`) must return the same SHA-256
   and size. If it doesn't, the file is kept.
4. The local file must still have the size and modification time it had when
   hashed. Only then is it deleted.

Objects on S3 with no checksum (uploaded by legacy or the previous uploader)
are uploaded again with one: same size alone is not taken as proof. Folders
are only ever removed with `rmdir`, which fails unless the folder is already
empty, so a kept file keeps its folder. Folders above the batch folder are
left in place.

S3 key: `<bucket_folder>/<client>/<output|output_frame|Data_Collection>/<same
relative path as local>` (`_key_prefix()`), where `bucket_folder`/`client`
come from the `clientinfo` DB row cached at Qualix login, falling back to
`S3_BUCKET_FOLDER`/`S3_CLIENT` config if that row doesn't exist.

**After a batch is uploaded**, History's record view shows "This scan's images
have been uploaded to cloud storage and removed from this device" instead of
the crops (`GET /api/history/{id}/images` returns `on_device: false`). The
batch's counts and its Qualix sync are unaffected: both come from the
database, not these files.

**Running it by hand:** `scripts/s3_upload_now.py`:
- `--dry-run` lists what a run would upload. No network access, nothing
  deleted.
- `--keep` uploads and verifies but deletes nothing.
- With no flag it does a full run, which also counts as the scheduled one.

It runs as its own process, so it can't see a scan running in the backend.
Use it with the machine idle.

### 6. Verifying uploads

1. At startup: `S3 worker started: every 3 day(s), checked every 3600s,
   delete after upload: True`. On the first real run, `S3 credentials
   initialised for bucket ...`, or `S3 init failed: ...`.
2. Each run ends with one summary line:
   `S3 upload complete in Ns: X session(s), Y file(s) — U uploaded, A already
   there, D deleted (M MB freed), F failed, V not verified (kept); skipped
   I in use, R within retention (3 days, output/ 30)`.
   The same numbers are kept in the database, which matters because the
   journal on this device is not persistent (see `logging.md`):
   ```sql
   select last_completed_at, last_attempt_at, last_result from s3_upload_state;
   ```
3. `not verified` above zero means S3 accepted files but didn't read back
   the matching SHA-256. They were kept. On a new bucket or Cognito role, run
   `scripts/s3_upload_now.py --keep` first: if `not verified` is 0, deletion
   will work on this device.
4. Per-file failures log at ERROR (`S3 upload failed for <path>: ...`).
5. To check a key on S3 directly:
   ```bash
   aws s3api head-object --bucket agnext-cognito --key <key> --checksum-mode ENABLED
   ```
   `ChecksumSHA256` there is the base64 SHA-256 of the file.

### Legacy-vs-port differences (S3 scope)

- Legacy ran two uploaders. The in-app one (`s3_upload.py`) walked only
  `output/` once at startup and never deleted anything. A separate cron
  script (`upload_videos_pool_id.py`) deleted after upload but only compared
  sizes, and deleted without any check at all when it wasn't allowed to
  look. This port has one uploader covering all three trees, on a 3-day
  schedule, and deletes only after a SHA-256 read-back.
- Legacy's cron script decided whether inference was busy from the process
  list. Here the backend checks its own scan and data-collection state, and
  the exact folders in use.
- The cron script's key layout (`<prefix>/<customer>/<location>/<device_id>/
  <top folder>/<file date>/...`) is not used. Keys follow the in-app
  uploader's layout above.
- Cognito identity pool ID is configurable (`settings.S3_IDENTITY_POOL`)
  instead of legacy's two hardcoded literal copies (`s3_upload.py:59,129`).
