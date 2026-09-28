# 12. Object Capture and Detection — 24–28 September 2026

Two symptoms were reported against the live-scan flow, and they turned out to
be opposite failures of the same pipeline:

1. **Objects were being missed.** Placing two pieces of foreign matter close
   together on the belt reliably lost the second one.
2. **Objects were being counted twice.** A single object was put in front of
   the operator, cropped and counted more than once.

Fixing the first made the second far more visible, so they are recorded
together. Six distinct defects were found, spanning the camera loop, the
inference post-processing, the tracker and the review state machine. The
final test after all of them: **5 objects placed, 5 reported.**

`enhancements.md` carries each change as a standalone entry describing current
behaviour. This document is the record of what was wrong, what the evidence
was, and how the pieces relate.

---

## 1. Measurements the rest of this rests on

Everything below was decided against numbers measured on the device rather
than estimated. They are recorded here because several of them are not
obvious and are easy to assume wrongly.

### 1.1 Per-frame cost

Benchmarked on the Jetson with the real TensorRT engine:

| Step | Time | Implied ceiling |
|---|---|---|
| Debayer 1920×1200 | 1.76 ms | 567 fps |
| **TensorRT predict (640×640)** | **23.35 ms** | **42.8 fps** |
| JPEG encode (q70, resized to 1280 wide) | 22.64 ms | 44 fps |

Inference is the hard ceiling. The JPEG encode is nearly as expensive as the
inference itself, which matters because it used to run on the event loop.

### 1.2 Camera and link

The camera is a **Hikrobot MV-CS023-10GC**, GigE, on a 1 Gb link, reporting
`gige=True` and a negotiated `GevSCPSPacketSize` of 1500.

- Frame: 1920 × 1200 × 1 byte (BayerRG8) = 2.30 MB
- Usable link throughput ≈ 117 MB/s → **~50 fps ceiling**
- At MTU 1500 each frame arrives as ~1600 packets

Both the legacy app and this port load the **same** camera configuration file,
`/home/nvidia/eye_compass/FeatureFile_new.ini` (legacy at `GrabImage.py:679`,
this port via `settings.CAMERA_FEATURE_FILE`). Resolution is therefore
identical between the two codebases, and `Width 1920 / Height 1200` with
`OffsetX 0 / OffsetY 0` and no binning or decimation is the **full sensor** of
a 2.3 MP camera. There are no unused sensor columns to widen into; field of
view is a function of the lens and the camera's height only.

`camera_service._log_sensor_geometry()` now prints the region against the
sensor maximum at every startup, and warns if they ever differ, so this does
not have to be re-established by hand:

```
Sensor geometry: reading 1920x1200 at offset (0, 0) from a 1920x1200 sensor
```

### 1.3 Detection rate, then and now

Legacy has **no frame-rate throttle anywhere**. Its camera thread grabs as
fast as the link allows and pushes every 2nd frame into a
`LifoQueue(maxsize=32)` (`GrabImage.py:85`); a separate `ProcessingThread`
drains it and infers at its own pace. Supply (~25 fps) sits below inference
capacity (~43 fps), so it keeps up.

| | Frames actually put through detection |
|---|---|
| Legacy | **~25 /s** |
| This port, before this work | **~10 /s** |
| This port, after | **~21 /s** |

The port had silently fallen to less than half of legacy. The cause is in
§2.3. Note the port is now at rough parity with legacy, not beyond it.

### 1.4 Belt speed

The application has no control over belt speed at all — the entire conveyor
vocabulary is five on/off commands (`conveyor_service.EXPECTED_ACKS`:
`machine_start`, `all_stop`, `FM_detected`, `camera_on`, `camera_off`). Speed
is set on a Goodrive10 VFD, observed at 32.83 Hz output frequency, and cannot
be read back by the software.

It was first *derived* from legacy's own stop calibration,
`(-0.000125 × y) + 0.56` (`main.py:2672`), whose slope implies 8000 px/s. That
derivation was **wrong**. The tracker now measures it directly and reports it
when a scan finishes:

```
Scan finished: sample=T11790579022 total_fo=43 ... belt=1550 px/s (774 ms in view)
```

**1550 px/s**, so an object crosses the 1200 px frame in **774 ms** — roughly
16 detection frames at the current rate, not the 1–2 the derived figure
implied. This is worth knowing for two reasons: raising the frame rate further
is much less urgent than it appeared, and legacy's hardcoded stop calibration
does not match the belt this machine actually runs at.

Because the constant is baked in and unreadable, the drive frequency must be
treated as fixed and recorded. Nothing in the software will notice if it
changes.

---

## 2. Why objects were being missed

### 2.1 Duplicate suppression compared only the x-axis

Legacy decides whether a fresh detection is merely one already queued by
comparing **centre-x alone**, within 10 px (`has_similar_x_axis`,
`main.py:2516`). The belt travels in **+y** — the tracker's own match gate
(`cy >= obj['y'] - 5`) and its `y <= height - 50` exit rule both depend on
that — so two objects one behind the other on the belt have near-identical
x-centres *by construction*. An x-only test cannot tell "the same object, one
frame later" from "a second object a few centimetres behind the first", and
discards the second.

The window where it bites is the one-second conveyor deceleration delay in
`pause_capture()`: frames keep being inferred for that whole second after a
detection freezes the screen, which is exactly when a trailing object slides
into view. Worse, `process_frame` recorded every tracked id in
`existing_track_ids` whether or not it was queued, so the suppressed object's
id was consumed permanently — never offered for review again after Resume,
never cropped, never counted.

**Fixed** in `ScanSession._novel_boxes` with a **directional** y comparison
rather than a second distance threshold. An object can only move forward down
the frame, so a box at the same x sitting at or ahead of one already awaiting
review (`ny >= ey - y_tolerance`) is that same object seen later and is
filtered; a box at the same x but *behind* one cannot be the same object at
any frame interval, so it is genuinely new and is kept.

A symmetric `|ny − ey|` threshold does not work here: across the deceleration
window the same object legitimately travels a long way in y, so any threshold
loose enough to still suppress it would also swallow a real trailing object.

### 2.2 Three defects in the tracker

`app/services/sort.py`, all in `ObjectTracker.update`:

- **`matched_ids` was never cleared between frames.** It is created in
  `__init__` and only ever added to, so from the second frame onward every id
  that had ever matched once was permanently "seen", `miss_count` never
  incremented again, and the `max_misses` eviction never fired. Stale tracks
  lingered for the full `max_age` window and could absorb a genuinely new
  object arriving in the same lane. Now reset per frame.
- **First match won, not the nearest.** The candidate set is an unordered
  dict, so breaking on the first id inside the tolerance box handed the
  detection to whichever track happened to be enumerated first. With two
  tracks in range that is as likely to be the wrong one as the right one,
  which swaps two closely-spaced objects' identities and makes one look new
  while the other goes stale. Now picks the closest candidate.
- **Two detections in one frame could claim the same track.** Nothing stopped
  it, so the second object never got an id of its own. Now one detection per
  track per frame.

### 2.3 The detection rate had collapsed to ~10 Hz

Three problems in the websocket loop, `app/api/camera.py`:

- **Decimation was applied *after* inference.** `grab_and_infer` ran
  `predict()` on every grabbed frame and the `raw_frame_index % decimation`
  check then discarded the result. Half of every TensorRT pass was spent on
  frames nothing ever looked at. The check now runs *before* inference, so a
  skipped frame is genuinely free.
- **The display JPEG encode ran on the event loop.** 22.64 ms inline, stalling
  the next grab. The paused branch already offloaded its encode correctly;
  the live path did not. Now offloaded.
- **Detection was paced to `STREAM_FPS`.** A `sleep` filled out the remainder
  of a 1/`STREAM_FPS` interval on every iteration, pinning the tracker's input
  rate to the browser's refresh rate — 20 fps grabbing, 10 fps detecting.
  Detection now runs as fast as the hardware allows; only the send to the
  browser is throttled, and a frame that has just stopped the belt is sent
  immediately regardless, since that frame is the one the operator reviews.

The stream's `fps` field now reports frames actually put through detection,
which is the rate that determines whether an object can cross the view unseen.

### 2.4 There was no detection queue

Legacy does not display a detection when it finds one — it **enqueues** it
(`detection_queue`, `main.py:2581`) and a separate `process_queue` thread
(`main.py:2651-2706`) pops one at a time, gated on a `que_next` flag that
Submit sets (`main.py:1289`). The live view returns only when the queue empties.

This port had no queue. `_on_foreign_matter` overwrote `pending` and
`pending_frame` wholesale, so during the one-second coast every detection
destroyed the previous one: only the last frame's objects were ever reviewed,
and everything found earlier in that window was discarded unseen.

Worth recording: `capture_paused`, which this port's comments cite as a port of
`cam_thread.capture_paused` at `GrabImage.py:82/95`, **does not exist anywhere
in the legacy source on this machine**. It is this port's own invention.
Legacy never stops grabbing during review; it relies on `que_next`.

**Fixed** with `ScanSession.detection_queue`. Finding foreign matter only
appends; stopping the belt, engaging the interlock and freezing the frame all
belong to `_promote_next_detection`, which runs when a detection actually
reaches the screen. `resume()` saves the current detection's crops and then
promotes the next queued one, staying frozen and interlocked; only when
nothing is left does it unlock, resume capture and hand back the live view.
`queue_depth` rides on every stream message so the Dashboard holds the overlay
up and shows how many are still waiting.

**Queued detections are promoted with no settling delay.** Legacy sleeps
0.41–0.56 s before each pop, computed from the object's y-coordinate. That is
conveyor *travel-time compensation* — holding the belt running long enough for
the detected object to reach the pickup position before the stop command goes
out, which is why an object nearer the top of the frame gets the longer wait.
It is load-bearing for the first detection of a burst and meaningless for the
rest, because Submit unlocks the interlock but never sends `machine_start`, so
the belt is already stationary when a queued detection is promoted. (Legacy's
own floor, `if self.delay < 0.3: self.delay = 0.4`, is unreachable — the
formula cannot return less than 0.41 inside a 1200 px frame.)

Two consequences fell out of building the queue:

- **Duplicate suppression had to become per-box.** A frame-level verdict
  discards every frame containing the object under review — which is all of
  them, for the whole deceleration window — including any genuinely new object
  that arrived in one of them. That would have made the queue dead code.
- **Pending box indices now run continuously for the whole scan** instead of
  restarting at 0 per detection. The index is part of the crop filename, and
  re-labelling deletes the crop it replaces by globbing `*_<index>.png`; with
  per-detection numbering that glob also matches the identically-numbered box
  of every earlier detection in the run. The bug predated the queue; the queue
  would have made it routine.

**Order is oldest-first, and bounded.** `detection_queue` is appended to and
drained with `pop(0)`, so the operator meets objects in the order they were
found. Legacy is the same — its `detection_queue` is a `queue.Queue`
(`main.py:2391`), which is FIFO; its *other* queue, the camera-to-inference
`LifoQueue`, is the newest-first one, and that is a different thing entirely.
Neither codebase capped the detection queue. This one now does, at
`DETECTION_QUEUE_MAX` (default 20), because each entry holds its own full
frame (6.9 MB) — every crop is cut from the frame its object was found in.
Objects that arrive while the backlog is full are held back **uncounted**, so
they are detected again once it drains rather than lost.

Within a single frozen frame, boxes are ordered by how far down the belt they
sit, furthest first. Every box in one frame was found at the same instant, so
there is no "found first" among them, but the one furthest along entered the
camera's view earliest — the order the operator watched them arrive in.
Untouched, the order is whatever NMS emitted, which is by descending
confidence, and that order reaches the operator through the numbering on the
crops and the reclassify gallery's listing of unlabelled ones.

A third, in the stream loop: a frozen review frame was sent once per pause
episode. A queued detection is promoted while capture stays paused throughout,
so the browser would have kept showing the previous image with the new boxes
drawn over it. `ScanSession.frozen_frame_seq` now tells the loop the frame
underneath has changed.

### 2.5 Objects against the left edge were never tracked

`ObjectTracker` refused to create an id for any detection with `cx <= 10`.
Those detections had no identity at all: they could never be recognised as
already-shown, so they returned for review every time anything else fired, and
they were never counted either. The gate is removed — every detection gets an
id.

---

## 3. Why objects were being counted twice

### 3.1 The model's NMS is class-wise

`non_max_suppression` offsets every box by its class id before handing it to
`torchvision.ops.nms` (`run_inference.py:727`, with the default
`agnostic=False`), so it only ever suppresses overlaps **within** a class. An
object the model cannot settle a class for comes back as two boxes at the same
coordinates under different classes.

Confirmed live on batch `T11790338159` — exactly two coordinate pairs in the
entire run appeared twice, and both were this:

```
(8,  [1412, 759, 1459, 813], conf 0.228, class 3)
(9,  [1412, 759, 1459, 813], conf 0.273, class 2)

(28, [1623, 821, 1670, 873], conf 0.305, class 1)
(29, [1623, 821, 1670, 873], conf 0.343, class 2)
```

Identical to the pixel, different class, both low confidence — the model
hedging. Downstream nothing could tell them apart: the tracker issued each an
id, both landed in `pending`, and on screen they are one rectangle drawn
exactly over another. The operator taps once, labels one, and
`save_unselected` writes the other as `NON-FM`. Since `create_results` counts
crop files, one object was reported twice.

**Fixed** by `ScanSession._merge_overlapping_detections`, which drops the
lower-confidence box of any pair overlapping by more than
`DETECTION_MERGE_IOU` — what a class-agnostic NMS would have kept. It runs
before tracking, so track ids, counted ids and the operator's boxes all come
off one list. It fired 7 times in a single subsequent run.

### 3.2 A fixed 10-pixel tolerance suits exactly one object size

Both the tracker's match test and the review-duplicate test used flat pixel
figures. The model holds a steady box around a small object, but around a
large one the box breathes. Measured on batch `T11790579022`:

```
first look:   [1606, 511, 1912, 745]   (306 × 234)
second look:  [1598, 903, 1896, 1129]  (298 × 226)
```

Same object, moved 392 px down the belt. Its right edge moved 16 px and its
centre 12 px — both past a flat 10 px, so the tracker issued a second id and
the operator was shown the same object twice.

**Fixed** with `TRACK_X_TOLERANCE_RATIO` (a fraction of the wider box) and
`TRACK_X_TOLERANCE_PX` as the floor, so small objects keep the tight threshold
that stops two of them being collapsed into one. The same figure drives
`ObjectTracker._x_tolerance_for` and `ScanSession._novel_boxes`; a mismatch
between them would let an object be re-queued for review while still holding
one track id, or the reverse.

### 3.3 The root cause: review decisions were made on geometry, not identity

This is the one that mattered most, and neither codebase ever solved it.

Legacy's `has_similar_x_axis` compares against the boxes still sitting in
`detection_queue`. That comparison has nothing to work with the moment the
queue drains — and `handle_detection` then queues `coo`, **every box in the
frame**. So an object the operator reviewed and submitted a second earlier
goes straight back on screen as soon as anything else triggers a detection.

Caught live on 28 September:

```
Track churn: new=[2] evicted={} tracked=[2]
Pending box coordinates: [(1, [1131, 0, 1272, 119], ..., class 0)]
Foreign matter on screen: 1 box(es)

        ... operator labels it, submits, presses Start ...

Track churn: new=[3] evicted={} tracked=[2, 3]
Pending box coordinates: [(2, [874, 1057, 931, 1120], ..., class 3),
                          (3, [1132, 0, 1272, 120],  ..., class 0)]
Foreign matter on screen: 2 box(es)
```

`tracked=[2, 3]` — track 2 was **never evicted**. The tracker knew perfectly
well it was the same object. The information needed to leave it alone was
present and unused, because nothing was awaiting review to compare its box
against.

**Fixed** by making identity the decision. `ObjectTracker.last_assignment`
reports which track each detection in the current frame belongs to, and a box
whose track is already in `counted_track_ids` is not queued again.
`counted_track_ids` is now filled with the ids actually put in front of the
operator, so it means exactly one thing: objects this scan has already shown.
`total_fo_detected` reads the same set, which is why the live count and what
the operator saw cannot drift apart.

Geometry survives only as a backstop, for the one case identity cannot cover:
a track that was dropped and re-minted arrives wearing an id nobody has seen.
The directional y comparison from §2.1 stays part of that backstop — without
it, an object following closely behind another shares its centre-x and is
discarded, which is the defect this whole thread started with.

---

## 4. Track staleness is measured in time, not frames

Legacy's `_remove_stale_objects` carried two frame counters, `max_age=9` and
`max_misses=2`. Both measure the same quantity — a track's `frame` is
refreshed only on a match and `miss_count` is reset only on a match — so the
tighter one always fired first and `max_age` was unreachable. What actually
governed was "drop a track unseen for 3 frames", which at the ~10 Hz the
pipeline ran came to about 0.3 seconds.

Counted in frames, that behaviour is hostage to the frame rate. With detection
no longer paced to `STREAM_FPS` and a 42.8 fps ceiling, the identical
constants would drop a track after 70 ms — re-minting ids for objects still
sitting under the camera and counting each one again.
`TRACK_STALE_AFTER_SECONDS` (default 0.3) keeps it the same at any rate.

The tracker measures that against **a clock of its own**, advanced only by
`update()` and only by the elapsed time since the previous call, clamped to
`max_step_seconds`. The clamp is what makes a pause survivable: capture stops
entirely while a detection is under review, which can run to minutes, and
against a wall clock every track would age out — so the objects under the
stopped belt would all return as new ids the moment scanning resumed. A gap of
any length now ages a track by at most one frame's worth, which is what a
frame counter gave for free and the property that had to be preserved
explicitly once the unit changed.

---

## 5. Diagnostics added

These stay in place; they are how any recurrence gets diagnosed without
guesswork.

- **`Track churn`** (`scan_session.process_frame`) — which ids are new this
  frame and which were evicted, each with the rule that evicted it
  (`unseen(0.30s)`, `exit-zone(y=1160 > 1150)`). A count can only grow when an
  id appears that has never been seen, so on a stopped belt this names the
  cause directly.
- **Belt speed at scan finish** — `belt=1550 px/s (774 ms in view)`.
- **`Merged a duplicate box`** — how often the model is hedging between
  classes on one object.
- **Sensor geometry at camera init** — the region being read versus the
  sensor maximum, with a warning if they differ.

---

## 6. Settings introduced

All default to current behaviour; all are documented in `.env` and commented
out there.

| Setting | Default | Governs |
|---|---|---|
| `TRACK_STALE_AFTER_SECONDS` | `0.3` | How long a track may go undetected before its id is dropped, in seconds of active detection |
| `TRACK_X_TOLERANCE_PX` | `10` | Floor for how far a box edge may move between frames and still be the same object |
| `TRACK_X_TOLERANCE_RATIO` | `0.25` | The same tolerance as a fraction of the object's own width |
| `DETECTION_MERGE_IOU` | `0.6` | Overlap above which two boxes in one frame are treated as one object |
| `DETECTION_QUEUE_MAX` | `20` | Most detections that may wait behind the one on screen |

`CAMERA_FRAME_QUEUE_SIZE` remains declared and **read nowhere**. It
corresponds to legacy's *other* queue, the `LifoQueue(maxsize=32)` between the
camera thread and the inference thread (`GrabImage.py:85`); this port grabs and
infers in one loop and has no equivalent.

---

## 7. Tests

Runnable with the service virtualenv from the backend root:

```
/home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_<name>.py
```

| Script | Covers |
|---|---|
| `test_tracking.py` | Identity, staleness held constant at 10/21/43 Hz, a four-minute review pause not evicting tracks, exit-zone eviction, belt-speed recovery |
| `test_dup.py` | Trailing object kept, same object filtered, the whole queued backlog matched against, the live 306 px regression from batch `T11790579022` |
| `test_queue.py` | The four states of the review screen: first detection shown, second queued behind it, Submit promoting while staying frozen and interlocked, Submit on an empty queue restoring the live view |
| `test_merge.py` | Both live class-confusion pairs merged, genuinely adjacent objects kept separate |
| `test_identity.py` | An already-reviewed object not shown again when a different object arrives; a left-edge object reported once and not again |

Several assert against coordinates taken verbatim from production logs, so a
regression reproduces the original failure rather than an approximation of it.

---

## 8. Not changed, and still open

- **`apply_suppression_rules` judges the whole frame by `detections[0]`**
  (`inference_service.py:143`). If the first detection trips a
  commodity-specific confidence rule, every detection in that frame is
  discarded with it. This is deliberate legacy fidelity — the docstring says
  so — but it is a genuine "skips FM" path. Changing it to a per-detection
  filter would shift counts relative to the legacy machine, so it needs an
  explicit decision.
- **The first detection of a burst has no settling delay.** The port never had
  legacy's 0.41–0.56 s travel compensation. If objects come to rest short of
  where operators reach for them, this is the cause. It is a physical
  calibration question, not a code one.
- **`CAMERA_FRAME_DECIMATION` is still 2.** Now that decimation happens before
  inference it is free to set to 1, roughly doubling the detection rate. With
  ~16 looks per object already, it is not currently needed.
- **Jumbo frames are not enabled.** The NIC reports `maxmtu 9194` but runs at
  1500, so each frame arrives as ~1600 packets instead of ~280. This is the
  usual cause of intermittent incomplete frames on GigE cameras. `sudo ip link
  set enP8p1s0 mtu 9000`; the code re-negotiates packet size on the next
  camera init.
- **Field of view cannot be widened in software.** §1.2 — the camera is
  already reading its entire sensor. Wider coverage needs a shorter focal
  length or more height, both of which shrink every object in pixels. A small
  object is ~47 px now and about 15 px by the time it reaches the model, which
  letterboxes 1920×1200 into 640×640.

---

## 9. The same thing in simple words

**The problem, in one line:** the machine was missing some pieces of foreign
matter, and showing others to the operator more than once.

### Why things were being missed

**The camera was only looking at half as many photos as it should have been.**
The old system took 20 photos a second but only examined 10 of them — and it
was doing the expensive AI work on all 20 and throwing half the results away.
Fixed: it now examines about 21 a second. (The original machine did about 25,
so this is back to normal rather than better than normal.)

**Two objects one behind the other looked like one object.** To avoid showing
the same thing twice, the system checked "is this in the same left-right
position as something I'm already showing?" But two objects travelling one
behind the other on a belt are *always* in the same left-right position — that
is what "one behind the other" means. So the second one got thrown away. It
now also checks whether the object is in front of or behind the one already
found.

**Anything found while the belt was still slowing down got lost.** The belt
takes about a second to stop. Anything spotted in that second replaced what was
found a moment earlier, instead of waiting its turn. There is now a **queue**:
you deal with them one at a time, the screen tells you "3 more to review", and
only when the queue empties do you get the live camera back.

**Objects touching the left edge of the picture were invisible to the
counter.** They are not any more.

### Why things were being shown twice

**The AI sometimes couldn't decide what an object was, so it drew two boxes on
it** — one labelled "stone", one labelled "plastic", in exactly the same spot.
On screen they sat perfectly on top of each other, so the operator saw one box,
tapped it once, and the untouched second box got saved as a separate object.
Now two boxes in the same place are merged into one.

**Big objects confused the "have I seen this before?" check.** The check
allowed the object to shift by 10 pixels between photos. That is right for a
small stone, but the AI's box around a large object wobbles by tens of pixels
every photo. So a large object looked like a different object each time. The
allowance now scales with how big the object is.

**The real problem: the system forgot objects the moment you pressed Submit.**
Every object gets a number when first seen. One object got number 2, was shown
to you, and you submitted it. A different object arrived shortly after — and
the system showed you number 2 *again* alongside it, even though its own log
proves it still knew that object was number 2. The "have I shown this?" check
only looked at what was on screen at that instant, and after Submit the screen
is empty, so everything looked new again. It is like a shop assistant who
forgets you the second they finish serving you.

It now **remembers the numbers**. Once an object has been put in front of you,
it is never shown again for the rest of that scan.

### Two things worth knowing

**The belt speed is set on a box on the wall and the software cannot read it.**
There is a stopping calculation built into the code for one specific speed. If
someone changes that dial, objects will stop in the wrong place and nothing
will warn anybody. Keep it fixed, write the number down, and say so if it ever
changes.

**The camera cannot see any wider than it already does.** It is a 2.3
megapixel camera already using every pixel it has — the same settings the
original machine used. Seeing more of the belt means a different lens or
mounting the camera higher, and both make everything in the picture smaller,
which makes small objects harder to detect. That is a real trade-off, not
something a setting can fix.

### Where it ended up

Five pieces of foreign matter placed on the belt, five reported. Every fix
above has an automated test behind it, several written against the exact
coordinates from the runs where the problem was caught, so if any of it comes
back it will reproduce the original failure rather than something like it.
