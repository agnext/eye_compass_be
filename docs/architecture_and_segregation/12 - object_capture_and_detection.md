# 12. Object Capture and Detection — 24–28 September 2026

Three symptoms were reported against the live-scan flow, and they turned out to
be failures of the same pipeline seen from different sides:

1. **Objects were being missed.** Placing two pieces of foreign matter close
   together on the belt reliably lost the second one.
2. **Objects were being counted twice.** A single object was put in front of
   the operator, cropped and counted more than once.
3. **One group of objects arrived across several review screens.** Five pieces
   sitting together on the belt reached the operator as 5, then 1, then 4.
4. **Overlapping boxes could not be tapped.** Where two objects sat close
   together, a tap on the frozen frame could only ever reach the box drawn on
   top, and the confirmation marks covered their neighbours.

Fixing the first made the second far more visible, and fixing both left the
third as what remained, so they are recorded together. Eight distinct defects
were found, spanning the camera loop, the inference post-processing, the
tracker and the review state machine. **5 objects placed, 5 reported** was the
result after the first two; §4 covers the third. The fourth is a review-screen
problem rather than a detection one, and is §9.

Sections 1–3 are the two counting symptoms and their evidence. §4, §5 and §9
are the review-screen behaviour, which is the only part of this that alters
what the operator experiences rather than only what is correct — §8.1 is how to
turn the settle behaviour off without a code change.

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

**Order is newest-first, and bounded.** `detection_queue` is appended to and
drained with `pop()`, so once the screen in front of the operator is submitted
the backlog comes off its most recent end — the object most recently identified
is the next one shown, and anything found earlier waits behind it. This is a
deviation from legacy, which drains FIFO: its `detection_queue` is a
`queue.Queue` (`main.py:2391`). Legacy's *other* queue, the camera-to-inference
`LifoQueue`, is a newest-first one, but that is a different thing entirely and
is not the precedent here. Neither codebase capped the detection queue. This one now does, at
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

## 4. Stop the belt, let it settle, then look

A detection used to be a snapshot of the single frame something was first
spotted in. Two things make that the wrong frame to review:

- The belt takes about a second to stop, so the objects are still moving in it.
- **The model does not find the same set in every frame.** Much of what this
  machine detects sits at 0.11–0.35 confidence, and an object found in one
  frame is missed in the next. A single frame shows whatever happened to be
  found in that one instant.

Caught on 28 September with five objects sitting together on the belt:

```
16:36:53.514   5 box(es) on screen
16:36:53.660   1 more found, queued behind it      (146 ms later)
16:36:54.002   4 more found, queued behind it      (342 ms later)
```

Nothing was lost — the backlog carried them — but the operator met one group of
objects across three screens.

`review_phase` now runs a cycle. On the first sighting the interlock goes on and
`FM_detected` is sent immediately, but **nothing is shown and nothing is
counted**; the scan enters `settling` for `DETECTION_SETTLE_SECONDS`. Once that
elapses it collects `DETECTION_SAMPLE_FRAMES` frames of the now-stationary belt,
`_merge_overlapping_detections` collapses the repeats across them, and the
result goes on one screen using the newest sample as the image.

Combining across frames is sound **only** because the belt is stopped: boxes
measured in different frames then describe the same positions, which is exactly
what is not true during the deceleration. Using the newest sample as the image
means every crop is cut from a frame its box genuinely belongs to.

Two fallbacks, because a missed object is not recoverable and a spurious box is:

- If the stationary frames find nothing, the sighting that stopped the belt is
  shown instead of being dropped.
- If everything found has already been reviewed, the interlock is released
  rather than leaving the operator on a frozen screen with no boxes.

**One thing this cycle can lose, and what catches it.** An object that is on
the belt when the stop is commanded but travels out of the bottom of the frame
before the belt is stationary cannot appear in any of the sample frames. The
old behaviour caught it, because it froze the frame the object was seen in.
`_note_escapes` covers that case: the tracker's exit rule drops a track once it
reaches the bottom of the frame, and it was still in view on the update that
dropped it, so `ObjectTracker.last_evicted_boxes` holds a box that belongs to
the frame being processed right then. That pairing — a box and an image that
match — is kept for any id that has never been shown, and `_queue_escapes`
puts it behind the main screen. It is marked counted at that point rather than
when shown, because nothing will detect it again.

This is deliberately narrow, on three counts. Only the exit rule counts, not a
track lost to staleness: an object that merely stopped being recognised for a
few frames is still physically there and will be in the stationary frames.

Second, **a track minted and exit-evicted inside the same update is ignored**.
An object at rest in the bottom 50 px of a stopped belt is detected again on
every frame from the half still in view, and the exit rule takes its id every
time, so it was being minted and evicted once per frame — each time under a
brand new id, which is why the per-id guard above could never collapse them and
each one queued its own review screen. `ObjectTracker.last_evicted_first_frames`
records the frame each evicted track was first seen on; when that equals the
frame it was evicted on, the track never travelled anywhere and did not escape.

Third, **`_queue_escapes` takes the combined stationary detections and drops
any escape that overlaps one**. Something still visible on the stopped belt did
not get away: it is about to be shown on the main screen, and queueing it as
well showed it twice. This is why `_promote_from_samples` builds `combined`
before calling `_queue_escapes` rather than after. The overlapping escape is
*not* marked counted on the way out — `_revive` hands the same id back to the
object still in view, so counting it here would filter it off the very screen
it belongs on. Together these three produced the 29 Sep report of one parked
object reaching the operator as three separate screens;
`scripts/test_parked_edge.py` is the regression.

**One object, one identity — even through a gap in detection.** The model is
not certain frame to frame: plenty of what this machine detects sits near 0.2
confidence, so an object in plain view is found, missed for a few frames, and
found again. A gap longer than `TRACK_STALE_AFTER_SECONDS` (0.3 s, four frames
at the measured rate) dropped the track, and the next detection of the same
object became a brand-new id. Every "have I already shown this?" decision rests
on that id, so the object was shown, cropped and counted again. Measured on
29 Sep: **one belt stop produced ids 10 through 20** — eleven "objects" — for a
handful of real ones.

`ObjectTracker._revive` closes it. A track dropped for going unseen is held for
`TRACK_REVIVE_WITHIN_SECONDS` (2.0) rather than forgotten, and a detection that
is about to become a new id is offered those held tracks first. It may only
claim one that it plausibly is: same lane within the usual x tolerance, at or
ahead of where the track was lost, and no further ahead than `_max_travel`
allows.

Two deliberate choices make this safe:

- **A separate step, not a longer staleness window.** Widening the live window
  changes what every detection matches against on every frame. This runs only
  for a detection that has no live track, so the ordinary path is untouched.
- **`_max_travel` uses `current_speed_px_s`, not `belt_speed_px_s`.** The
  latter takes only forward motion, which is right for reporting the belt's
  running speed and useless here: on a stopped belt nothing moves, so nothing
  is sampled and the median sits at the running speed as though the belt were
  still going. `current_speed_px_s` samples every match including stationary
  ones over a short window, so it falls to roughly zero within a second of the
  belt stopping. The effect is that the window is tight exactly where objects
  are not moving, and wide open where they are — so a long revive window costs
  nothing on a running belt (the object is long past by then) and is what makes
  a stopped one work.

A track dropped by the **exit rule** is revivable too, but on much tighter
terms: only by a detection that has barely moved at all (`TRACK_MIN_TRAVEL_PX`,
80 px). An object that comes to rest half out of the bottom of the frame is
still detected, frame after frame, from the half still visible; the exit rule
dropped its track each time, so it collected a fresh id each time and the
operator was shown it twice. That is precisely when it happens, too — the belt
is stopped for the whole of review. One frame of a running belt carries an
object about 96 px, further than that bound, so an object genuinely on its way
out cannot reclaim its id and keep it. The bound is a fixed distance rather
than a measured one because once a track starts being evicted every frame it
stops matching, so no new speed samples are taken and `current_speed_px_s`
freezes at whatever the belt was last doing — a gate on measured speed
deadlocks here, and was tried and removed.

**A detection clipped by the bottom edge is never minted a new id.** The belt
carries material down through the frame, so every object is cut in half by the
bottom edge on its way out. That clipped box is a perfectly good detection —
the model finds it from the half still visible — and it used to be given a
brand new id, which made it a brand new object to everything downstream: an
object reviewed while whole in the middle of the frame came back a moment later
as a half box and was reviewed again. Reported live on 29 Sep, two objects
reaching the operator as four.

`TRACK_EDGE_MARGIN_PX` (default 15) sets how close to the bottom the box has to
reach to count as clipped. Matching and revival run first and are unaffected —
an object leaving keeps the id it already owns, which is what the exit rule and
`_note_escapes` work from. Only the mint is refused. `ScanSession` applies the
same rule once more when assembling candidates: a bottom-clipped detection that
came back with no id is dropped there too, because the already-shown filter
speaks through ids and the geometry backstop would otherwise call it novel and
put it on screen.

**The top edge is deliberately excluded**, though an entering object is just as
clipped. An object entering is minted while still clipped and then keeps that
id as it comes in — its left and right edges do not move and it only travels
down — so entry never produced a second id in the first place. Refusing one
there would instead leave a large object resting against the top edge with no
identity at all, and identity is the only thing that stops it being reviewed
twice. That case is real and logged: `test_identity.py` is built on a box from
the live log with its top edge at y=0. `scripts/test_edge_duplicate.py` covers
the bottom-edge case, including that the bottom of the frame has not become a
dead zone for genuinely new objects.

Widening the camera's view to give objects room at the edges is not available:
the MV-CS023-10GC is already read out at its full 1920x1200 sensor, so there is
no more field of view to take. The margin above is the software equivalent —
it does not capture more, it stops a half-seen object being mistaken for a
second one.

**Live matching has no such upper bound, deliberately.** One was tried there —
a live track may only claim a detection within `_max_travel` of it — and
removed. Any such bound has to come from how fast things are moving, and while
the belt decelerates the objects on it are moving at very different speeds at
the same instant: one already at rest, another still crossing most of a frame
height. Every estimate over that mixture, median or maximum, sits well below
what the fastest object is doing, so the bound refused matches the belt had
plainly made and the next detection of an already-tracked object became a brand
new id — manufacturing exactly the duplicate the revival logic exists to
prevent. Two objects one behind the other in the same lane are already
separated by nearest-match, which hands each detection to the closest track
rather than the first one in range. `scripts/test_reid.py` covers both halves:
identity surviving a gap, and an id never reaching a different object.

**Still open: objects lost during the stop.** At this belt's measured
1244 px/s an object crosses the 1200 px view in 964 ms, about as long as the
1.0 s settle, so the stationary frames often contain nothing at all — in one
run, 13 of 18 review screens. Two things can still be lost there: an object the
model stops finding partway through the stop (only a bottom-of-frame exit is
held), and the rest of the stopping sighting when the stationary frames find
some but not all of it. An attempt to close both by holding every sighting
during the stop was reverted, because it was built on top of the id churn
described above and faithfully reproduced it — one object became several
"missed" ones and was shown repeatedly. It is worth revisiting now that the
identity problem is fixed. See §10.

`detection_queue` survives as a safety net and should now always read 0 — a
sighting is resolved into one screen before capture pauses, so nothing reaches
it in ordinary running. `scripts/test_queue.py` drives it directly for that
reason; `scripts/test_settle.py` covers the normal path.

A related fix fell out of this. `process_frame` used to return early on a frame
with no detections, which meant such a frame never reached the tracker — so an
object that vanished completely stopped the staleness clock and its id lived
forever. Empty frames now flow through.

## 5. Writing the raw frame was costing more than the inference

`save_raw_frame` writes a full-resolution quality-95 JPEG, and it ran inline
inside `process_frame` — the detection loop. Measured on this device:

| | Time |
|---|---|
| TensorRT predict | 23.4 ms |
| **save_raw_frame (2.7 MB on disk)** | **52.0 ms** |
| Display encode | 24.6 ms |

More than twice the inference, blocking the next frame, on every detection.
The effect is worst exactly where it hurts most: during a burst of detections,
successive events in the log were landing ~150 ms apart — about **7 fps**
against a 42.8 fps ceiling — which is a large part of why one group of objects
was being split across screens.

The encode and write now happen on a single background thread. The frame number
is still assigned on the detection loop so files stay numbered by capture order
rather than by whichever write finished first, and `flush_raw_frames()` waits
for the backlog before anything counts those files (`update_fm_count`) or moves
them (`cancel`). `stop_raw_frame_writer()` retires the thread on shutdown, and
is also registered with `atexit` — a daemon thread killed inside `cv2.imencode`
aborts the interpreter, which reads as a crash in the journal.

The stream loop also logs the measured detection rate every 10 s
(`Detection rate: N fps`), so this no longer has to be inferred from timestamps.

---

## 6. Track staleness is measured in time, not frames

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

## 7. Diagnostics added

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

## 8. Settings, and how to turn the new behaviour off

All default to current behaviour; all are documented in `.env` and commented
out there.

| Setting | Default | Governs |
|---|---|---|
| `TRACK_STALE_AFTER_SECONDS` | `0.3` | How long a track may go undetected before its id is dropped, in seconds of active detection |
| `TRACK_X_TOLERANCE_PX` | `10` | Floor for how far a box edge may move between frames and still be the same object |
| `TRACK_X_TOLERANCE_RATIO` | `0.25` | The same tolerance as a fraction of the object's own width |
| `DETECTION_MERGE_IOU` | `0.6` | Overlap above which two boxes in one frame are treated as one object |
| `DETECTION_QUEUE_MAX` | `20` | Most detections that may wait behind the one on screen |
| `DETECTION_SETTLE_SECONDS` | `1.0` | How long to let the belt stop before looking |
| `DETECTION_SAMPLE_FRAMES` | `3` | Stationary frames combined into one review screen; 1 disables |
| `REVIEW_JPEG_QUALITY` | `88` | Quality of the frozen review frame, which is sent at full sensor width rather than `STREAM_MAX_WIDTH` |

`CAMERA_FRAME_QUEUE_SIZE` remains declared and **read nowhere**. It
corresponds to legacy's *other* queue, the `LifoQueue(maxsize=32)` between the
camera thread and the inference thread (`GrabImage.py:85`); this port grabs and
infers in one loop and has no equivalent.

### 8.1 The two switches behind "stop, settle, then look"

Section 4's behaviour is the one worth being able to back out of a device
without a code change, because it is the only change here that alters what the
operator experiences rather than just what is correct. Both settings live in
`eye_compass_be/.env` and take effect on a service restart.

**`DETECTION_SETTLE_SECONDS`** (default `1.0`) — how long to wait, after the
belt has been told to stop, before taking the frames the review screen is built
from. It exists because the conveyor does not stop instantly: at the measured
1550 px/s it is still carrying objects for most of that second, and frames taken
during it show objects in positions they will not be in when the operator looks.

- Raise it if the belt is slower to stop than a second, which shows up as boxes
  that do not sit on the objects in the frozen image.
- Lower it to make the review screen appear sooner. Too low and the belt is
  still moving when the frames are taken, which is the problem it exists to
  solve.
- `0` takes the frames immediately. Combined with `DETECTION_SAMPLE_FRAMES=1`
  this is the old freeze-the-first-frame behaviour.

**`DETECTION_SAMPLE_FRAMES`** (default `3`) — how many frames of the stopped
belt to combine into one review screen. It exists because the model does not
return the same set of objects in every frame: much of what this machine detects
sits at 0.11–0.35 confidence, so an object found in one frame is missed in the
next. One frame shows whatever happened to be found in that instant, and the
rest arrive afterwards as separate detections — which is how five objects
sitting together reached the operator as 5, then 1, then 4.

- Raise it if objects are still arriving on separate screens. Each extra frame
  costs about 50 ms of settle time and one more inference pass.
- **`1` disables the combining**, so the review screen carries whatever a single
  frame contained. That is the behaviour this section replaced.

To return a device to the pre-existing behaviour entirely:

```
DETECTION_SETTLE_SECONDS=0
DETECTION_SAMPLE_FRAMES=1
```

Nothing else needs changing, and no other fix in this document is affected —
the identity filtering, the duplicate merging and the tracker changes all
continue to apply. What comes back is the old symptom: one group of objects
spread across several review screens, arriving through `detection_queue`.

---

## 9. Reviewing what was found

The boxes drawn on the frozen frame were the only way to classify an object,
and on a touch screen they do not work.

### 9.1 Why they do not work

**They are smaller than a fingertip.** Foreign matter runs 40–70 sensor pixels
across (§1). The frame is displayed stretched to fill the belt view —
`object-fit: fill` plus `preserveAspectRatio="none"` on the overlay, matching
legacy's QLabel exactly — and on a 16:9 panel the 16:10 sensor image is
squashed more vertically than horizontally. A 47 px object lands under 30
screen pixels. The usual floor for a reliable touch target is about 44.

**The padding made neighbours overlap that never touched.** Every box shown was
`enlarge_bbox(..., pad=20)`, which adds 40 px to both width and height. On a
47 px object the drawn box is nearly double the object. Two objects 30 px apart
produce boxes that overlap by 10 px. The padding exists to give the *saved
crop* some margin; it was never meant to be the hit area.

**A tap resolves by DOM order, not by intent.** Overlapping `<rect>` elements
hand the tap to whichever was drawn last. There was no way to reach the one
underneath, and nothing on screen said there was one underneath.

**The confirmation marks made it worse.** A labelled box carried a green
`✓ <type>` tag, with a compact dot fallback for small objects. The comment
justifying that fallback asserted two tags "essentially can't collide without
their two boxes already overlapping, which the detector itself rules out" —
which the padding above makes false. On a cluster the tags covered each other
and the objects.

### 9.2 What the review screen does instead

The frame is now the map; the controls are beside it.

- **Each object is listed in its own row** in a panel where the Start/Stop
  sidebar sits (that sidebar is hidden throughout review anyway,
  `SHOW_SIDEBAR_DURING_FM_REVIEW = false`). A row is a full-width button with a
  112 px minimum height, so overlap on the frame cannot affect reachability.
  The panel **collapses to a 56 px rail**, keeping the marked-so-far count and
  the button that brings it back. Collapsing only gives the frame more width:
  `object-fit: fill` and `preserveAspectRatio="none"` map the whole frame onto
  whatever space there is at any width, so nothing is ever cut off in either
  state — only how much the picture is squeezed changes.
- **Each row carries a magnified thumbnail of its own object**, cut from the
  frozen frame with a canvas (`FmCrop` in `Dashboard.jsx`). Cut from the
  *padded* box, so the object sits in context and the thumbnail is exactly what
  the backend saves as that object's crop. Letterboxed, never stretched — the
  belt view is distorted for legacy parity, but an operator judging shape must
  not be.
- **Tapping a row opens the existing FM-type picker**, the same bottom bar a
  box tap opens. Nothing about labelling, re-labelling or Submit changed.
- **Tapping the thumbnail itself enlarges it** in a preview overlay, with the
  type buttons repeated and Previous/Next through the whole set. Even in the
  panel the object is small, and deciding what something is has to be possible
  before choosing a type for it. Same shape as `ReclassifyObjects.jsx` and
  `ResultsViewer.jsx`'s own previews, including their backdrop-dismiss guard:
  the backdrop covers the screen the instant it renders, so without a short
  dead window the second tap of a double-tap closes the preview again and it
  reads as blinking. Marking from the preview leaves it open, so Next carries
  straight on. Each row is a `<div role="button">` containing the thumbnail's
  `<button>` — a button inside a button is not valid HTML.
- **The rows and the boxes share one numbering**, assigned by position in
  `pending` — furthest down the belt first, which is the order the objects
  entered the camera's view. The frame shows only that number; the type name is
  in the panel, where there is room for it.
- **Boxes stay tappable.** It is convenient for an isolated object, and nothing
  depends on it.

### 9.3 Two supporting changes

**The overlay draws the detection, outset a little.** Every entry in `pending`
carries both boxes: `box`, padded by 20 px a side, which every crop is cut
from, and `raw_box`, the detection as the model reported it. Both are set by
`_set_pending` in `scan_session.py`, which also replaced the duplicated
pending-building code in `_show` and `_promote_next_detection`, and the
unpadded box is carried through the detection queue, the escaped-object path
and the stop-sighting fallback so all three routes to the screen have it.

What is drawn is `raw_box` plus `BOX_OUTSET` (7 px, `Dashboard.jsx`), clamped
to the frame. The padded box drawn as-is made a ~50 px object look ~90 px, so
neighbours 30 px apart appeared to overlap; the bare detection removed the
false overlap but sat flush against the object and read as a much smaller box
than operators were used to. 7 px clears the object without approaching what it
takes to collide with a neighbour.

**Classic View is the default, and List View is one press away.** Classic View
is the screen as it was before the panel: the frozen frame alone at full width,
boxes drawn at the full padded size, and the green `✓ <type>` tag or its
compact dot on each labelled one. `ClassicBox` in `Dashboard.jsx` is that view,
carrying its own original reasoning in its comments — including the assumption
§9.1 disproves. The **List View** button in the review header brings up the
panel, which is what to reach for when objects sit too close together to tap
apart. Either way it is a presentation switch only: the same detections, the
same `handleLabel`, the same crops and counts.

**The frozen frame is sent at full resolution.** The live stream is capped at
`STREAM_MAX_WIDTH` (1280) at quality 70 because it pays that cost on every
frame. The review frame is encoded once per review screen with the belt already
stopped, and the thumbnails are cut from it, so it goes out uncapped at
`REVIEW_JPEG_QUALITY` (88) via `encode_review` in `app/api/camera.py`. At 1280
a 70 px object would arrive as 47 px and be magnified from there.

---

## 10. Tests

Runnable with the service virtualenv from the backend root:

```
/home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_<name>.py
```

| Script | Covers |
|---|---|
| `test_tracking.py` | Identity, staleness held constant at 10/21/43 Hz, a four-minute review pause not evicting tracks, exit-zone eviction, belt-speed recovery |
| `test_reid.py` | An object keeps its id across a gap in detection, including one spanning a whole stop; past the revive window it correctly gets a new one; and an id never reaches a different object — a new arrival in the same lane, two objects one behind the other, a moving belt with a long gap, an object that left the frame, another lane |
| `test_parked_edge.py` | An object parked half out of the bottom of a stopped belt reaches the operator on one screen, not one per frame, and does not come back after submit |
| `test_edge_duplicate.py` | An object reviewed while whole is not reviewed again as a half box on its way out of the bottom of the frame, and the bottom of the frame is still live for new objects |
| `test_dup.py` | Trailing object kept, same object filtered, the whole queued backlog matched against, the live 306 px regression from batch `T11790579022` |
| `test_queue.py` | The four states of the review screen: first detection shown, second queued behind it, Submit promoting while staying frozen and interlocked, Submit on an empty queue restoring the live view |
| `test_merge.py` | Both live class-confusion pairs merged, genuinely adjacent objects kept separate |
| `test_identity.py` | An already-reviewed object not shown again when a different object arrives; a left-edge object reported once and not again |
| `test_settle.py` | Nothing shown until the belt has stopped; three stationary frames finding different subsets combined into one screen carrying all five objects; fallback when the stationary frames find nothing; an object leaving the view mid-stop held and shown afterwards; raw frames written off the loop |

Several assert against coordinates taken verbatim from production logs, so a
regression reproduces the original failure rather than an approximation of it.

---

## 11. Not changed, and still open

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

## 12. The same thing in simple words

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

### Why one group of objects arrived across several screens

Even once nothing was being missed or double-counted, five objects sitting
together on the belt still reached the operator as 5 on one screen, then 1, then
4 — three screens for one handful of objects.

**The AI does not find the same things in every photo.** A lot of what this
machine spots it is only barely sure about. On one photo it sees an object, on
the next it does not. So a review screen built from a single photo shows
whatever happened to be found in that one instant, and the stragglers turn up a
moment later as separate screens.

**And the belt is still moving while all this happens.** It takes about a second
to stop. The old behaviour froze the very first photo — taken while everything
was still travelling.

The fix is to **stop the belt, wait for it to actually stop, then look**:

1. Something is spotted → the belt is told to stop, but nothing is shown yet
2. Wait about a second for it to come to rest
3. Take three photos of the now-stationary belt
4. Combine what is found in all three and show it on one screen

Combining only works because nothing is moving. During the stop, boxes measured
in different photos are in different places and cannot share one image.

**One thing this could lose, and what catches it.** An object that is on the
belt when the stop begins but slides off the bottom of the picture before the
belt halts cannot be in any of those three photos. The old behaviour caught it
by accident, because it froze the photo the object was seen in. So the system
keeps hold of anything that leaves the view mid-stop, together with the last
photo it appeared in, and shows it straight after the main screen.

**The machine now keeps track of an object even when it blinks.** The AI is not
certain from photo to photo — a lot of what this machine spots it is only
barely sure about — so an object sitting in plain view gets found, missed for a
few photos, then found again. The machine used to treat that second sighting as
a completely new object. Since "have I already shown you this?" is answered by
which object it thinks it is, the same piece of foreign matter got shown,
cropped and counted more than once. On one belt stop this turned a handful of
real objects into eleven.

Now, when the AI loses sight of something, the machine holds onto its identity
for a couple of seconds instead of forgetting it immediately. If something
turns up again in the same place, it gets its old identity back rather than a
new one.

It only does this when the object could genuinely still be there. It has to be
in the same position across the belt, and no further down it than the belt
could have carried it in that time. On a stopped belt that means it has barely
moved, so the test is strict. On a moving belt the object is long gone, so
anything new appearing in that lane is correctly treated as a different object.
An object that has run off the bottom of the picture only keeps its identity if
it has not moved since — which happens when the belt is stopped and it comes to
rest half in and half out of the picture. It is still spotted over and over
from the half that is visible, and used to count as a new object every time.
One frame of a moving belt carries an object much further than that, so
something genuinely on its way out does not keep its identity.

There is a second half to that. An object resting at the very bottom edge is
still in the picture, so it belongs on the ordinary review screen along with
everything else — and the machine used to *also* file it under "this one got
away before I could stop the belt" and show it again afterwards. It now checks
whether the object is still there before doing that. If it is, it goes on the
normal screen and nowhere else.

**An object is not counted twice for being half out of the picture.** The belt
carries things down and out of the bottom of the picture, so on its way out
every object is cut in half — and the machine could still see the half that was
left, which it used to treat as a brand new object. That is why two pieces of
paper arrived as four. It now recognises a box cut off by the bottom edge as
the tail end of something it has already been watching, not as something new.

Giving the camera a wider view instead is not possible — it already reads its
whole sensor, so there is no more picture to be had.

**Which order the operator sees them in.** When more than one screenful of
objects is waiting, the most recently identified one is shown first, then the
one before it, and so on back. The object just identified is the one the
operator is looking at on the belt.

**And one thing that can still be lost.** This belt is fast — an object crosses
the whole picture in under a second, about as long as the belt takes to stop —
so the stopped-belt photos often contain nothing at all, and an object the AI
stops spotting partway through the stop is not caught. Showing everything ever
seen during the stop was tried and made things worse at the time, because it
was built on top of the identity problem above. Worth trying again now that
part is fixed.

**A second thing that was making it worse.** Every detection was saving a
full-size photo to disk, which took 52 milliseconds — more than twice as long as
the AI itself — right in the middle of the loop, holding up the next photo. That
now happens in the background. It mattered most exactly when several objects
arrived together, which is when the frame rate matters most.

### How to turn this behaviour off

This is the only change here that alters what you actually experience rather
than just making the counts right, so it can be backed out from
`eye_compass_be/.env` without touching any code. Restart the service afterwards.

```
DETECTION_SETTLE_SECONDS=0
DETECTION_SAMPLE_FRAMES=1
```

- **`DETECTION_SETTLE_SECONDS`** (normally `1.0`) is how long to wait for the
  belt to stop before taking the photos. Raise it if the boxes do not sit
  properly on the objects in the frozen image — that means the belt was still
  moving. Lower it to make the screen appear sooner. `0` means take the photo
  straight away.
- **`DETECTION_SAMPLE_FRAMES`** (normally `3`) is how many photos of the
  stopped belt to combine. Raise it if objects are still turning up on separate
  screens; each extra one adds about 50 milliseconds. `1` means use a single
  photo, which is the old behaviour.

Setting both as shown above returns the machine to how it worked before.
Everything else in this document keeps working — the counting fixes, the
duplicate merging, the tracking. The only thing that comes back is one group of
objects being spread over several screens.

### Why tapping the boxes on screen was so hard

Even with the right objects on the right screen, actually marking them was the
next problem. **The boxes are smaller than a fingertip.** A piece of foreign
matter is about the size of a grain of rice in a 2-megapixel photo, and by the
time that photo is stretched across the display its box is under 30 screen
pixels — smaller than the area a finger actually covers when it touches glass.

**And the boxes were drawn bigger than the objects.** Every box had a 20-pixel
margin added all the way round, because the picture the machine saves of each
object needs a bit of space around it. But that margin was also being drawn on
screen, which made each object look nearly twice its real size — so two objects
sitting a small gap apart appeared to overlap when they never touched.

**When two boxes overlap, a tap can only ever reach the top one.** There was no
way to get at the one underneath, and nothing told you there was one underneath.
On top of that, a marked box got a green tick and the type name written beside
it, which on a cluster covered the neighbouring boxes and the objects
themselves.

**The fix: a list beside the picture.** Every object found is now listed down
the right-hand side, one row each, with a **close-up of that object** blown up
several times life size. Tapping a row opens exactly the same type buttons as
before. The rows are numbered, and the same number appears on the frame, so you
can always see which object on the belt a row refers to.

The picture is now there to show you *where* things are. The list is how you
mark them. Overlapping boxes stop mattering, because you are no longer aiming
at them — and for the first time you can actually see what you are classifying
instead of a smudge.

**Tap the picture in a row and it opens up big**, with the type buttons right
there and Previous/Next to walk through every object without going back to the
list. It is the same enlarged view the reclassify screen and the saved-record
screen already have.

**The screen still opens the way it always did** — just the picture, the
bigger boxes, and the green tick and type name on each one. The **List View**
button in the top bar brings up the list of close-ups beside it, and **Classic
View** goes back. Nothing else changes: same objects, same pictures saved, same
counts. Reach for List View when objects are sitting too close together to tap
apart.

Two smaller things came with it. **The boxes on the frame are drawn close to
the object's real size now**, with just a small gap around it instead of that
20-pixel margin — so the picture is much less cluttered and objects only look
like they are touching when they really are.

And **the frozen picture you review is now sent at the camera's full
resolution** instead of the reduced size used for the live view. It is sent
once, with the belt already stopped, so it costs nothing while scanning and
makes every close-up sharper.

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

Five pieces of foreign matter placed on the belt, five reported — and now on
one screen rather than three. Every fix above has an automated test behind it,
several written against the exact coordinates from the runs where the problem
was caught, so if any of it comes back it will reproduce the original failure
rather than something like it.

The measured detection rate is also in the log now, every ten seconds, so it
never has to be guessed at again:

```
Detection rate: 20.3 fps (decimation=2, 49 ms per inferred frame)
```
