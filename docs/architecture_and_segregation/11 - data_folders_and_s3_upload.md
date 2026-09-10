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
- **Output frame folder** — a batch's raw full frames, but only one per FM
  detection event (not every frame of the run). Powers the "Frame Count"
  metric.
- **Rejected folder** — a cancelled batch's `output/` folder contents,
  *moved* (not copied) here. So: FM/NON-FM crop images, same as `output/`
  would have had. Never contains raw full frames — `output_frame/` isn't
  touched by Cancel at all.
- **S3** — a 60-second background sweep uploads everything under `output/`
  and `output_frame/` only (crops + `result.json` + raw per-FM-event
  frames). `Data_Collection/` and `rejected/` are never uploaded.

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
```

Same subpath shape as `output/` (built alongside it in the same `start()`
call, `scan_session.py:164-168`), but a sibling tree with different content.

Only writer: `save_raw_frame()` (`scan_session.py:247-262`), called exactly
once per FM-detection event from `_on_foreign_matter()`
(`scan_session.py:389`) — **not** every raw frame of the whole scan.
Filename: `r_frame_<n>.jpg`, JPEG quality 95, BGR→RGB converted
(`scan_session.py:254-260`, port of `save_raw_image`, `main.py:2413-2441`).

Purpose: `update_fm_count()` (`scan_session.py:535-556`) counts these
`.jpg` files and reports the count as `"Frame Count"` in `looker_data` — an
exact port of `main.py:1535-1563`. Per the code's own comment
(`scan_session.py:248-251`): without this folder, Frame Count is always
zero.

### 4. Rejected folder

```
<OUTPUT_DIR>/rejected/<commodity>/<variety>/<sample_id>_<timestamp>/
```

Only touched by `ScanSession.cancel()` (`scan_session.py:488-511`, port of
`cancel_result`, `main.py:2064-2084`) — Cancel Batch / discard-pending-result.

Every regular file currently in that batch's `output/` folder is **moved**
(`shutil.move`, not copied — nothing is deleted, no originals left behind)
into the rejected folder. So a rejected batch's folder holds the same kind
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

Started at app startup if `settings.S3_ENABLED` (default on,
`app/main.py:90-95`). Runs on a **fixed 60-second timer**
(`S3_UPLOAD_INTERVAL_SECONDS`), not event-driven — nothing uploads the
instant a file is saved; it's picked up on the next sweep.

Each cycle walks exactly two trees and nothing else
(`s3_worker.py:114-122`):

```python
for sub in ("output", "output_frame"):
    ...
```

So the crop `.png` files, `result.json`, and the raw `r_frame_N.jpg` files
all get uploaded. **`Data_Collection/` and `rejected/` are never visited by
this loop and never reach S3.**

S3 key: `<bucket_folder>/<client>/<output|output_frame>/<same relative path
as local>` (`_key_prefix()`, `s3_worker.py:73-93`), where `bucket_folder`/
`client` come from the `clientinfo` DB row cached at Qualix login, falling
back to `S3_BUCKET_FOLDER`/`S3_CLIENT` config if that row doesn't exist.

No database "uploaded" flag exists anywhere. "Already uploaded" is decided
live, every cycle, by comparing sizes: if the S3 object is missing or
smaller than the local file, it (re-)uploads
(`_sync_directory`, `s3_worker.py:137-153`) — same comparison legacy used
(`s3_upload.py:99-107`).

### 6. Verifying uploads

1. Log check: `"S3 background uploader started"` and `"S3 credentials
   initialised for bucket agnext-cognito"` at startup. If the second is
   missing, look for `"S3 disabled: no Cognito identity pool configured"`
   or `"S3 init failed: %s"`.
2. `ls` the local `output/`/`output_frame/` folder for the run in question
   to know what *should* be there.
3. Per-file failures log at ERROR (`"S3 upload failed for %s: %s"`);
   successes only log at DEBUG.
4. Get the real prefix from Postgres, then compare directly:
   ```sql
   select client_name, image_folder_name from clientinfo;
   ```
   ```bash
   aws s3 ls s3://agnext-cognito/<image_folder_name>/<client_name>/output/ --recursive
   aws s3 ls s3://agnext-cognito/<image_folder_name>/<client_name>/output_frame/ --recursive
   ```
   Compare names/counts, and object sizes (`aws s3api head-object`) against
   local `stat` sizes — that's literally the same check the worker itself
   uses.
5. Absence of anything under `Data_Collection/`/`rejected/` in S3 is
   expected, not a bug.

### Confirmed legacy-vs-port differences (S3 scope)

- This port also uploads `output_frame/`; legacy's `s3Uploading`
  (`s3_upload.py:16`) only ever walked `output/`.
- Cognito identity pool ID is configurable (`settings.S3_IDENTITY_POOL`,
  `s3_worker.py:51`) instead of legacy's two hardcoded literal copies
  (`s3_upload.py:59,129`).
- Multipart upload threshold is 8 MB (`s3_worker.py:130-135`) vs legacy's
  unusually low 1600-byte threshold that forced multipart on nearly every
  file (`s3_upload.py:143-147`).
- `s3_worker.py`'s own header comment currently claims legacy's uploader
  "was never started — the class was defined and nothing instantiated
  it." That doesn't hold up against `/home/nvidia/eye_compass_legacy/main.py:3143-3149`,
  where `s3Uploading()` is in fact constructed and started from
  `start_background_threads()`. Flagged here as a stale/inaccurate code
  comment worth correcting, not a functional issue — not yet fixed.
