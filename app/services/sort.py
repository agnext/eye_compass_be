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
                 x_tolerance_ratio=0.25, revive_within_seconds=2.0,
                 travel_margin=1.6, min_travel_px=80.0, edge_margin_px=15):
        self.tracked_objects = {}  # {object_id: {'x','x2','y','bbox','frame','seen_at'}}
        self.next_id = 1
        self.x_tolerance = x_tolerance
        # Scales the tolerance with the object's own width — see
        # _x_tolerance_for. x_tolerance stays as the floor.
        self.x_tolerance_ratio = x_tolerance_ratio
        self.stale_after_seconds = stale_after_seconds
        # A track dropped for going unseen is not forgotten immediately — see
        # _revive. {object_id: {..track state.., 'lost_at': clock}}
        self._lost = {}
        self.revive_within_seconds = revive_within_seconds
        self.travel_margin = travel_margin
        self.min_travel_px = min_travel_px
        self.edge_margin_px = edge_margin_px
        # {new_detection_id: old_id} for ids brought back by the most recent
        # update(). Diagnostic only; nothing branches on it.
        self.last_revived = {}
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
        self.last_evicted_first_frames = {}
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

        # A second, much shorter speed record, and the one _max_travel uses.
        # _speed_samples above only takes forward motion, which is right for
        # reporting the belt's running speed but useless for "how far could
        # this object have gone just now": on a stopped belt nothing moves, so
        # nothing is sampled, and the median stays at the running speed as if
        # the belt were still going. This one samples every match including
        # stationary ones, over a short window, so it falls to roughly zero
        # within a second of the belt stopping — which is exactly when an
        # object must be recognised as not having moved.
        self._recent_speed = deque(maxlen=30)

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
        frame_height = frame_size[0]

        # Per FRAME, not per tracker. Carried across frames, every id that ever
        # matched once would stay permanently "matched", and the check below
        # that stops two detections in one frame claiming the same track would
        # stop working after the first frame.
        self.matched_ids = set()
        self.last_revived = {}
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
                    # Deliberately no upper bound on how far down the lane a
                    # live track may reach. One was tried and removed: any
                    # such bound has to be derived from how fast things are
                    # moving, and while the belt decelerates the objects on it
                    # are moving at very different speeds at the same instant —
                    # one already at rest, another still travelling most of a
                    # frame height. Every estimate over that mixture sits well
                    # below what the fastest object is doing, so the bound
                    # refused matches the belt had plainly made, and the very
                    # next detection of an object already being tracked became
                    # a brand-new id. That is the duplicate the operator sees,
                    # so the bound cost far more than it saved. Two objects one
                    # behind the other in the same lane are already separated
                    # by nearest-match below, which hands each detection to the
                    # closest track rather than the first one in range.
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
                if dt > 0:
                    # Every match, stationary ones included — see
                    # _recent_speed. Backward jitter reads as not moving.
                    self._recent_speed.append(max(0.0, dy) / dt)
                self.tracked_objects[best_id] = {
                    'x': cx, 'x2': x2, 'y': cy, 'bbox': detection,
                    'frame': frame_idx, 'seen_at': clock,
                    'first_frame': previous.get('first_frame', frame_idx),
                }
                self.matched_ids.add(best_id)
                self.last_assignment[det_index] = best_id
                continue

            # No live track matched. Before minting a new id, check whether
            # this is an object that was being tracked until the model lost
            # sight of it a moment ago — see _revive.
            revived = self._revive(cx, x2, cy, clock, detection, frame_idx)
            if revived is not None:
                self.last_assignment[det_index] = revived
                continue

            # A detection clipped by the BOTTOM edge is half an object on its
            # way out, and gets no NEW id — matching and revival above have
            # already had their chance to hand it the one it owns. Without
            # this, an object counted once while whole in the middle of the
            # frame was counted again a moment later as a clipped box at the
            # bottom, and the operator was shown it twice: once in frame, once
            # half in frame while leaving. Reported live, 29 Sep.
            #
            # The top edge is deliberately NOT treated the same way, even
            # though an entering object is just as clipped. An object entering
            # is minted while still clipped and then keeps that id as it moves
            # in — its left and right edges do not change and it only moves
            # down — so entry never produced a second id to begin with.
            # Refusing one there would instead leave a large object resting
            # against the top edge with no identity at all, and identity is
            # the only thing that stops it being shown again after it has been
            # reviewed. That case is real and logged: see test_identity.py.
            if y2 >= frame_height - self.edge_margin_px:
                continue

            # Every detection otherwise gets an id, including one hard against
            # the left edge. Legacy refused an id to anything with cx <= 10,
            # which left those detections with no identity at all — they
            # could never be recognised as "already shown", so each one
            # came back for review every time anything else triggered a
            # detection, and they were never counted either.
            self.tracked_objects[self.next_id] = {
                'x': cx, 'x2': x2, 'y': cy, 'bbox': detection,
                'frame': frame_idx, 'seen_at': clock,
                'first_frame': frame_idx,
            }
            self.matched_ids.add(self.next_id)
            self.last_assignment[det_index] = self.next_id
            self.next_id += 1

        self._remove_stale_objects(clock, frame_size)

    def _revive(self, cx, x2, cy, clock, detection, frame_idx):
        """Give a detection back the id it had before the model lost sight of it.

        This is the fix for one object being reported as several. The model is
        not certain frame to frame — plenty of what this machine detects sits
        near 0.2 confidence — so an object present the whole time is found,
        missed for a few frames, and found again. Missing it for longer than
        stale_after_seconds drops the track, and the next detection of the very
        same object used to become a brand-new id. Everything downstream that
        asks "have I already shown this to the operator?" is answered by that
        id, so the object was shown, cropped and counted again. Measured on
        29 Sep: one stop produced ids 10 through 20 for a handful of objects.

        Deliberately a separate step rather than a longer stale_after_seconds.
        Widening the live window changes what every detection matches against
        on every frame; this only runs for a detection that was about to become
        a new id, so a detection that has a live track to match is unaffected.

        A lost track is only a candidate while the object could still be where
        it is being claimed:

          * same lane, within the usual x tolerance;
          * at or ahead of where it was lost — objects only move down the belt;
          * no further ahead than _max_travel allows, which on a stopped belt
            is barely any distance at all and while running is large.

        A track dropped by the exit rule is offered on much tighter terms: it
        may only be claimed by a detection that has barely moved at all
        (min_travel_px), because the only way to still be seeing an object that
        reached the bottom of the frame is for it not to have gone anywhere —
        a stopped belt. One frame of a running belt carries an object further
        than that bound, so a genuinely departing object cannot reclaim its id
        and then keep it. That bound is a fixed distance rather than a measured
        one on purpose: once a track starts being evicted every frame it stops
        matching, so no new speed samples are taken and current_speed_px_s
        stays frozen at whatever the belt was last doing.

        Nearest candidate wins, same as live matching.
        """
        if not self._lost:
            return None

        best_id = None
        best_distance = None
        for obj_id, lost in self._lost.items():
            if obj_id in self.matched_ids:
                continue
            tolerance = self._x_tolerance_for(x2 - cx, lost['x2'] - lost['x'])
            if abs(cx - lost['x']) > tolerance or abs(x2 - lost['x2']) > tolerance:
                continue
            if cy < lost['y'] - 5:
                continue
            bound = (self.min_travel_px if lost.get('exited')
                     else self._max_travel(clock - lost['seen_at']))
            if cy > lost['y'] + bound:
                continue
            distance = abs(cx - lost['x']) + abs(x2 - lost['x2'])
            if best_distance is None or distance < best_distance:
                best_id, best_distance = obj_id, distance

        if best_id is None:
            return None

        won = self._lost.pop(best_id)
        self.tracked_objects[best_id] = {
            'x': cx, 'x2': x2, 'y': cy, 'bbox': detection,
            'frame': frame_idx, 'seen_at': clock,
            'first_frame': won.get('first_frame', frame_idx),
        }
        self.matched_ids.add(best_id)
        self.last_revived[best_id] = round(clock, 2)
        return best_id

    def _remove_stale_objects(self, clock, frame_size):
        """Drop tracks that have gone unseen, or that have left the frame."""
        height, width = frame_size
        survivors = {}
        evicted = {}
        dropped = {}
        born = {}
        for obj_id, obj_data in self.tracked_objects.items():
            dropped[obj_id] = obj_data.get("bbox")
            born[obj_id] = obj_data.get("first_frame")
            unseen_for = clock - obj_data['seen_at']
            if unseen_for > self.stale_after_seconds:
                evicted[obj_id] = "unseen(%.2fs)" % unseen_for
                # Not gone, just not currently visible: held for _revive, so
                # the same object found again keeps the id it already had.
                self._lost[obj_id] = dict(obj_data, lost_at=clock)
            elif obj_data['y'] > height - 50:
                # Exit rule: an object this far down is leaving the field of
                # view. Its id goes, but it is still offered to _revive — an
                # object half out of the bottom of a STOPPED belt is detected
                # again, frame after frame, from the half still visible, and
                # without that it collected a fresh id every time. Since "have
                # I shown this?" is answered by the id, the operator was shown
                # the same half-visible object twice. Reported live, 29 Sep,
                # and the belt is stopped for the whole of review, so this is
                # precisely when it happens.
                evicted[obj_id] = "exit-zone(y=%s > %s)" % (obj_data['y'], height - 50)
                self._lost[obj_id] = dict(obj_data, lost_at=clock, exited=True)
            else:
                survivors[obj_id] = obj_data
        for obj_id in list(self._lost):
            if clock - self._lost[obj_id]['lost_at'] > self.revive_within_seconds:
                del self._lost[obj_id]

        self.tracked_objects = survivors
        self.last_evicted = evicted
        self.last_evicted_boxes = {
            obj_id: self.tracked_objects.get(obj_id, {}).get("bbox")
            or dropped[obj_id]
            for obj_id in evicted
        }
        # Which frame each evicted track was first seen on. A track whose
        # first frame is also its last was minted and evicted inside one
        # update and never travelled anywhere — see _note_escapes.
        self.last_evicted_first_frames = {
            obj_id: born[obj_id] for obj_id in evicted
        }

    def get_tracked_objects(self):
        return self.tracked_objects

    @property
    def current_speed_px_s(self):
        """How fast things are moving right now, or None before any match.

        Distinct from belt_speed_px_s, which answers "how fast does this belt
        run" and deliberately ignores stationary samples. This one answers
        "how far could something have moved since I last saw it", which on a
        stopped belt is nearly nothing.
        """
        if not self._recent_speed:
            return None
        return float(np.median(np.fromiter(self._recent_speed, dtype=float)))

    def _max_travel(self, gap_seconds):
        """How far down the frame an object could have got in `gap_seconds`.

        Used as the upper bound on a match, so a track cannot claim a detection
        that is further down the belt than the object could possibly have
        travelled. Generous on purpose — the margin covers the speed estimate
        being low and the floor covers a belt that has only just started moving
        — because the cost of being too tight is a new id for an object that
        already had one, which is the failure this whole class exists to avoid.
        """
        # The FASTEST thing recently seen, not the typical one. A stopping
        # belt holds objects moving at very different speeds at the same
        # instant — one already at rest, another still travelling — and a
        # median over that mixture is far below what the moving one is
        # actually doing. Bounding by the median made the tracker refuse a
        # match the belt had plainly made and mint a second id for an object
        # it was already tracking, which is the duplicate the operator sees.
        # belt_speed_px_s ignores stationary samples by construction, so it
        # is the right ceiling; current_speed_px_s still raises it if
        # something is moving faster than the belt ever ran.
        speeds = [v for v in (self.current_speed_px_s, self.belt_speed_px_s)
                  if v is not None]
        if not speeds:
            return float('inf')
        speed = max(speeds)
        # Never less than one frame's worth of belt. Two detections of the
        # same object on consecutive frames are separated by one frame
        # interval, but the clock gap between them reads as near zero once
        # a track has already been matched inside this same update — which
        # collapsed the bound to min_travel_px and refused a match the belt
        # had plainly made. At the measured 1244-1550 px/s an object covers
        # ~96px between frames, comfortably past that floor, so the bound
        # was rejecting ordinary forward motion at full speed.
        gap = max(max(0.0, gap_seconds), self.max_step_seconds)
        return max(self.min_travel_px, speed * gap * self.travel_margin)

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
