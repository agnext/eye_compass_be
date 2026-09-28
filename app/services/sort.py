import time
from collections import deque

import numpy as np


class ObjectTracker:
    """Frame-to-frame identity for objects moving down the belt.

    Staleness is measured in SECONDS OF ACTIVE DETECTION rather than in frames.
    Legacy used two frame counters — `max_age=9` and `max_misses=2` — but both
    measured the same quantity (a track's `frame` is refreshed only on a match,
    and `miss_count` is reset only on a match), so the tighter of the two always
    fired first and the looser one was unreachable. What it worked out to, at
    the ~10Hz the pipeline actually ran, was "drop a track unseen for 3 frames",
    i.e. about 0.3 seconds.

    Expressing that in frames ties the tracker's behaviour to the frame rate: at
    43Hz the identical constants would drop a track after 70ms, minting new ids
    for objects that are still sitting under the camera and counting each one
    again as a fresh detection. Seconds keep it the same at any rate.
    """

    def __init__(self, x_tolerance=40, stale_after_seconds=0.3, max_step_seconds=0.1,
                 x_tolerance_ratio=0.25):
        self.tracked_objects = {}  # {object_id: {'x','x2','y','bbox','frame','seen_at'}}
        self.next_id = 1
        self.x_tolerance = x_tolerance
        # Scales the tolerance with the object's own width — see
        # _x_tolerance_for. x_tolerance stays as the floor.
        self.x_tolerance_ratio = x_tolerance_ratio
        self.stale_after_seconds = stale_after_seconds
        self.matched_ids = set()
        # {object_id: reason} for the ids dropped by the most recent update().
        # An evicted track whose object is still physically under the camera
        # is re-detected as a BRAND NEW id on a later frame, which the scan
        # session then counts as another foreign object — so when a count moves
        # without the material moving, this is the record of why. Diagnostic
        # only; nothing branches on it.
        self.last_evicted = {}
        # {object_id: bbox} for those same ids, as they were last seen. An
        # object dropped by the exit rule was still in frame on the update that
        # dropped it, so this is a usable last known position for something
        # that has now left the camera's view — the only record of it there is.
        self.last_evicted_boxes = {}
        # Which track id each detection of the most recent update() was given,
        # parallel to the detections list (None for a detection that was not
        # tracked at all). This is the only place identity actually lives — the
        # caller needs it to tell "the object I already showed the operator"
        # from "a different object", which no comparison of box coordinates can
        # do reliably.
        self.last_assignment = []

        # The tracker's own clock, advanced only by update() and only by the
        # real elapsed time since the previous call, clamped to max_step_seconds.
        #
        # The clamp is what makes a pause survivable. Capture stops entirely
        # while the operator reviews a detection, which can take minutes, and no
        # frames are processed in that window. Against a wall clock every track
        # would age past the staleness limit and be evicted, so the objects
        # still sitting under the stopped camera would all come back as new ids
        # the moment scanning resumed — each counted as another foreign object.
        # Clamping means a gap of any length ages a track by at most one frame's
        # worth, which is the same thing a frame counter did for free.
        self._clock = 0.0
        self._last_tick = None
        self.max_step_seconds = max_step_seconds

        # How far a matched track moved down the frame per second, sampled on
        # every match. The belt's linear speed is set on the VFD and the
        # application has no way to read it, but pixels per second is the form
        # the software actually depends on: it decides how long an object stays
        # in view, and therefore how many times it is looked at. Measuring it
        # here needs no cooperation from the conveyor and stays correct if
        # somebody changes the drive frequency.
        self._speed_samples = deque(maxlen=400)

    def _x_tolerance_for(self, *widths):
        """How far an edge may move between frames and still be the same object.

        Proportional to the object's own width, with x_tolerance as a floor.
        A fixed pixel budget suits exactly one object size: measured live, a
        306x234 box came back the next time as 298x226 with its right edge 16px
        away, which a flat 10px tolerance rejects — so the tracker issued a
        second id and the operator was shown the same object twice.
        """
        return max(self.x_tolerance, self.x_tolerance_ratio * max(widths))

    def _tick(self, now):
        if now is None:
            now = time.monotonic()
        if self._last_tick is None:
            step = 0.0
        else:
            step = min(max(0.0, now - self._last_tick), self.max_step_seconds)
        self._last_tick = now
        self._clock += step
        return self._clock

    def update(self, detections, frame_idx, frame_size, now=None):
        """
        Update the tracker with new detections.
        :param detections: List of tuples [(x, y, x2, y2), ...]
        :param frame_idx: Current frame index
        :param frame_size: Size of the frame as (height, width)
        :param now: Monotonic timestamp; defaults to time.monotonic(). Injected
            by tests so staleness can be exercised without real sleeping.
        """
        clock = self._tick(now)

        # Per FRAME, not per tracker. Carried across frames, every id that ever
        # matched once would stay permanently "matched", and the check below
        # that stops two detections in one frame claiming the same track would
        # stop working after the first frame.
        self.matched_ids = set()
        self.last_assignment = [None] * len(detections)

        for det_index, detection in enumerate(detections):
            x, y, x2, y2, _, _ = detection
            w = x2 - x
            h = y2 - y
            cx, cy = x + w // 2, y + h // 2  # Center of the bounding box
            cx = x
            cy = y

            # Nearest match, not first-within-tolerance. The candidate set is
            # unordered (a plain dict), so breaking on the first id inside the
            # tolerance box handed the detection to whichever track happened to
            # be enumerated first — with two tracks in range that is as likely
            # to be the wrong one as the right one, which swaps two closely
            # spaced objects' identities and makes one of them look new while
            # the other goes stale.
            best_id = None
            best_distance = None
            for obj_id, obj_data in self.tracked_objects.items():
                if obj_id in self.matched_ids:
                    # One detection per track per frame: without this, two
                    # detections in the same frame can both claim the same
                    # track and the second object never gets an id of its own.
                    continue
                tolerance = self._x_tolerance_for(
                    x2 - x, obj_data['x2'] - obj_data['x'])
                if (
                    abs(cx - obj_data['x']) <= tolerance
                    and abs(x2 - obj_data['x2']) <= tolerance
                    and cy >= obj_data['y'] - 5
                ):
                    distance = abs(cx - obj_data['x']) + abs(x2 - obj_data['x2'])
                    if best_distance is None or distance < best_distance:
                        best_id, best_distance = obj_id, distance

            if best_id is not None:
                previous = self.tracked_objects[best_id]
                dt = clock - previous['seen_at']
                dy = cy - previous['y']
                # Forward motion only. A track matched twice inside one frame's
                # clock step gives dt == 0, and a small backward dy is centroid
                # jitter on a stationary object, neither of which says anything
                # about belt speed.
                if dt > 0 and dy > 0:
                    self._speed_samples.append(dy / dt)
                self.tracked_objects[best_id] = {
                    'x': cx, 'x2': x2, 'y': cy, 'bbox': detection,
                    'frame': frame_idx, 'seen_at': clock,
                }
                self.matched_ids.add(best_id)
                self.last_assignment[det_index] = best_id
            else:
                # Every detection gets an id, including one hard against the
                # left edge. Legacy refused an id to anything with cx <= 10,
                # which left those detections with no identity at all — they
                # could never be recognised as "already shown", so each one
                # came back for review every time anything else triggered a
                # detection, and they were never counted either.
                self.tracked_objects[self.next_id] = {
                    'x': cx, 'x2': x2, 'y': cy, 'bbox': detection,
                    'frame': frame_idx, 'seen_at': clock,
                }
                self.matched_ids.add(self.next_id)
                self.last_assignment[det_index] = self.next_id
                self.next_id += 1

        self._remove_stale_objects(clock, frame_size)

    def _remove_stale_objects(self, clock, frame_size):
        """Drop tracks that have gone unseen, or that have left the frame."""
        height, width = frame_size
        survivors = {}
        evicted = {}
        dropped = {}
        for obj_id, obj_data in self.tracked_objects.items():
            dropped[obj_id] = obj_data.get("bbox")
            unseen_for = clock - obj_data['seen_at']
            if unseen_for > self.stale_after_seconds:
                evicted[obj_id] = "unseen(%.2fs)" % unseen_for
            elif obj_data['y'] > height - 50:
                # Exit rule: an object this far down has left, or is leaving,
                # the field of view. On a STATIONARY belt an object parked in
                # the bottom 50px is evicted and re-created every single frame,
                # each time with a fresh id.
                evicted[obj_id] = "exit-zone(y=%s > %s)" % (obj_data['y'], height - 50)
            else:
                survivors[obj_id] = obj_data
        self.tracked_objects = survivors
        self.last_evicted = evicted
        self.last_evicted_boxes = {
            obj_id: self.tracked_objects.get(obj_id, {}).get("bbox")
            or dropped[obj_id]
            for obj_id in evicted
        }

    def get_tracked_objects(self):
        return self.tracked_objects

    @property
    def belt_speed_px_s(self):
        """Median observed downward speed, or None before anything has moved.

        Median rather than mean: a track that jumps to a different object
        contributes a wild sample, and one of those would drag an average a
        long way.
        """
        if not self._speed_samples:
            return None
        return float(np.median(np.fromiter(self._speed_samples, dtype=float)))
