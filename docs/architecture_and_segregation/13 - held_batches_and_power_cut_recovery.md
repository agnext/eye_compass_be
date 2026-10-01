# 13. Held Batches and Power-Cut Recovery

What happens to a batch scan that is not finished in one go: on purpose (the
operator holds it) or not (power cut, crash, restart, the scan page reloaded).
Reference doc for `app/services/scan_progress.py` and the pieces around it.

## Short version

- On the results page after **Submit**, besides **Save** and **Cancel**, there
  is **Hold Batch**. It saves nothing to History, sends nothing to Qualix and
  moves no files. The batch is just set aside.
- **Held Batches** on the Home screen lists every batch waiting to be
  continued, with a count on the card.
- **Continue** opens the live scan screen for that batch, with its earlier FMs
  carried over. **Start** scans more material into the same batch; **Submit
  Batch** goes straight to its results page, where it can be saved.
- If the device loses power (or the backend restarts) during a batch, that
  batch appears in the same list marked **Interrupted** and is continued the
  same way.
- **Discard** on the list throws a batch away exactly as Cancel does: its
  crops go to `rejected/`.

## Why it can work at all

A batch's results are already on disk while it runs:

| Result | Comes from |
|---|---|
| FM counts by type | the crop files in `output/<…>/<batch>/`, counted by filename prefix (`create_results`) |
| Frame Count | the `r_frame_N.jpg` files in `output_frame/<…>/<batch>/` (`update_fm_count`) |
| Training frames | `fm/`, `fm_full_frames/` in the same `output_frame` folder |

So continuing a batch never needs its images copied or rebuilt. It continues
in its own folders. What has to be kept is only what otherwise lives in memory
(`ScanSession.progress_state()`):

- which batch it is: sample id, commodity, variety, batch id, FM types;
- when it started: start date and time;
- its two folders;
- the stop counters (`conveyor_stop_count`);
- how far its file numbering has got.

## The `scan_progress` table

One row per scan run (`ScanProgress` in `app/models/schema.py`), keyed by the
run's folder name. Created on the first Start of a batch and updated at every
operator action:

| Action | Endpoint |
|---|---|
| Start | `/scan/start` |
| Stop | `/scan/stop` |
| Each reviewed detection | `/scan/label`, `/scan/resume` |
| Forward | `/scan/forward` |
| Submit | `/scan/submit` (also sets `awaiting_save`) |
| Hold | `/scan/hold` |

**Never from the per-frame detection loop**, so scanning speed is not
affected. Every write is best-effort: a database error is logged and the scan
carries on, because losing the ability to *recover* a batch must not stop the
operator *scanning* one.

| status | Meaning |
|---|---|
| `active` | the batch currently loaded in the scan session |
| `held` | held from the results page |
| `interrupted` | was active when the backend stopped, or was left unfinished |
| `saved` | Save pressed: it is in History |
| `discarded` | Cancel (live or results page) or Discard from the list |

`held` and `interrupted` are the open states: listed, continuable, and
protected from the S3 cleanup. There is only ever one `active` row. Recording a
checkpoint for a new batch marks any other `active` row `interrupted`.

### When a batch becomes interrupted

- **At startup** (`app/main.py` lifespan): any row still `active` was running
  when the backend stopped. That covers a power cut, a crash and
  `systemctl restart` alike.
- **The scan page is loaded for a different batch, or reloaded**
  (`/scan/reset`): the batch that was loaded is kept, not dropped. Before this
  feature, reloading the scan page mid-batch lost it.
- **Continue is pressed** while a results page for another batch was left
  open: that batch is kept as interrupted, not lost.

The hold of an interrupted batch starts at its **last checkpoint**, the last
moment it is known to have been running, not at startup. So the outage is
recorded as time held, never as stop time.

## Continuing a batch

`POST /scan/held/{id}/resume` → `ScanSession.restore(state)`. Refused (409)
while another batch is being scanned, and if the batch's `output/` folder is no
longer on the device.

What `restore` takes care of:

1. **File numbering continues past what is on disk**, not only past the stored
   counters. This is the part that matters most:
   - A crop's filename ends in its pending index, and re-labelling a box
     deletes `*_<index>.png`. Reusing an index would delete an *earlier*
     object's crop.
   - `fm/frame_<n>` and `r_frame_<n>.jpg` would be overwritten the same way.

   The disk is checked as well as the stored row because a power cut can land
   after a file was written but before its row was updated
   (`_numbering_on_disk`).
2. **The live FM count carries on.** The tracker starts fresh on a continued
   batch: its ids restart, and the objects it knew are long gone. So the
   earlier reviews are carried as `prior_fo_count`, the number of crops already
   on disk, and added to the live count. The final count at Submit always comes
   from the files, so it is right either way.
3. **Stop metrics stay true.**
   - A stop that was running when the batch was interrupted is closed at the
     last checkpoint.
   - A batch that was submitted before being held has already counted Submit's
     own implicit stop. Its final Submit would count a second one, and the
     Manual Stop Count only discounts one. The earlier one is taken back out.
4. **Detection waits for Start**, the same as after a review is submitted, so
   objects still sitting under the camera are not reported the moment the live
   view returns.

The frontend then opens `/dashboard` with `resumed: true`, which stops it
running its usual "fresh batch" reset on arrival. A blue note says which batch
is being continued until Start is pressed. Start goes through `start()`'s
existing "already active" path, which continues the session rather than
creating a new folder.

## Time on hold

Stored, not counted, not shown, on request:

| Column | |
|---|---|
| `hold_count` | how many times the batch was held or interrupted |
| `total_held_seconds` | total time spent held or interrupted |
| `hold_history` | `[{held_at, resumed_at, reason}]`, reason `held` or `interrupted` |

Hold time is never added to Manual Stop Time, FM Stop Time or Total Stop Time.
The batch keeps its original start date and time. Its end time is the final
Submit.

## S3 cleanup

`S3UploaderTask._protected_dirs()` adds the folders of every `active`, `held`
and `interrupted` row. Without this, a batch held for more than
`S3_RETENTION_DAYS` would have its frames uploaded and deleted, and after
`HISTORY_WINDOW_DAYS` its crops too, before it was continued. If the database
cannot be read, the S3 run is abandoned rather than treating "unknown" as
"nothing to protect".

## What a power cut can still lose

- **The detection on screen at that moment**, if its boxes had not been
  reviewed yet. Those crops are written when the operator labels or submits the
  review, so there is nothing on disk for them yet.
- **The last second or so of frames** still queued in the background writers.
- **Objects that were under the camera** may be detected again after the batch
  is continued and Start is pressed. The operator reviews them again.
- Files written in the last moments before the cut can be incomplete on disk.
  That is an operating-system limit, not something this app controls.

Everything already reviewed (its crops), every frame already written, the
batch's identity, its start time and its stop counters survive.

## Tests

`scripts/test_held_batches.py` runs 33 checks against the real API functions
and scan session, on a throwaway SQLite database:

- hold, then another batch run and cancelled in between;
- a simulated power cut and the startup pass;
- S3 protection;
- continuing after two days on hold, and scanning more into the batch;
- numbering continuing with no file overwritten;
- the final counts, the Manual Stop Count, and hold time not counted as stop
  time;
- Save and Discard;
- a reloaded scan page.
