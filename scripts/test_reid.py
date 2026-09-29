"""One identity per object, across a gap in detection.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_reid.py

The model is not certain frame to frame — plenty of what this machine detects
sits near 0.2 confidence — so an object sitting in plain view is found, missed
for a few frames, and found again. Missing it for longer than
TRACK_STALE_AFTER_SECONDS dropped its track, and the next detection of the very
same object became a brand-new id. Every "have I already shown this?" decision
downstream is answered by that id, so the object was shown, cropped and counted
again.

Measured on the device, 29 Sep: a single belt stop produced track ids 10
through 20 — eleven "objects" — for a handful of real ones, and the operator
was shown the same piece of foreign matter on several screens.

ObjectTracker._revive closes that. These checks cover both halves: an object
whose detection lapses keeps its id, and an id is never handed to something
that is genuinely a different object.
"""
import sys
sys.path.insert(0, '.')
from app.services.sort import ObjectTracker

FRAME = (1200, 1920)
FPS_STEP = 0.077                      # ~13 fps, the measured detection rate
BELT_PX_PER_FRAME = 96                # ~1244 px/s, the measured belt speed

box = lambda x, y: [float(x), float(y), float(x + 40), float(y + 40), 0.5, 2]


class Run:
    """A tracker plus a clock, so staleness can be exercised without sleeping."""

    def __init__(self, **kw):
        kw.setdefault("x_tolerance", 10)
        kw.setdefault("x_tolerance_ratio", 0.25)
        kw.setdefault("stale_after_seconds", 0.3)
        self.t = ObjectTracker(**kw)
        self.now = 0.0

    def step(self, detections, dt=FPS_STEP):
        self.now += dt
        self.t.update(detections, 0, FRAME, now=self.now)
        return [i for i in self.t.last_assignment if i is not None]

    def gap(self, frames, dt=FPS_STEP):
        for _ in range(frames):
            self.step([], dt)


# 1. THE BUG. Belt stopped, object sitting still, model loses it for well over
#    the staleness limit, then finds it again. It is the same object and must
#    come back with the same id.
r = Run()
first = r.step([box(300, 400)])
r.gap(8)                              # 0.6s — twice the staleness limit
again = r.step([box(300, 402)])       # 2px of box jitter, belt is stopped
assert first == [1] and again == [1], (first, again)
assert r.t.last_revived, "the id was re-issued rather than reclaimed"
print("1. stopped belt, 8-frame gap -> same id %s, not a new one" % again)

# 2. It must survive the whole stop. The belt takes about a second to halt and
#    three stationary frames are taken after that, so a gap spanning the lot is
#    the case that matters.
r = Run()
r.step([box(300, 400)])
r.gap(16)                             # ~1.2s, the full settle plus sampling
assert r.step([box(300, 405)]) == [1], "the id did not survive a full stop"
print("2. gap spanning a whole 1.2s stop -> still the same id")

# 3. Past the revive window it is genuinely forgotten, so a new id is correct.
r = Run(revive_within_seconds=0.5)
r.step([box(300, 400)])
r.gap(12)                             # ~0.9s, well past the 0.5s window
assert r.step([box(300, 402)]) == [2], "a long-forgotten track was still revived"
print("3. past the revive window -> a new id, as it should be")

# ---- and now the half that must NOT happen -------------------------------

# 4. A different object arriving in the same lane after the first has left.
#    Objects only ever move down the belt, so an object appearing at the TOP
#    cannot be one last seen further down.
r = Run()
for i in range(8):
    r.step([box(300, 100 + BELT_PX_PER_FRAME * i)])     # crosses and exits
r.gap(4)
assert r.step([box(300, 120)]) == [2], "a new object was given a departed one's id"
print("4. new object entering the lane a moment later -> its own id")

# 5. Two objects travelling one behind the other stay two, every frame.
r = Run()
seen = [r.step([box(300, 200 + BELT_PX_PER_FRAME * i),
                box(300, 600 + BELT_PX_PER_FRAME * i)]) for i in range(5)]
assert all(s == [1, 2] for s in seen), seen
print("5. two objects one behind the other in one lane -> stay distinct")

# 6. Belt RUNNING and the object lost for a long time. It has travelled far
#    past where it was, so anything now in that lane is something else — the
#    travel bound is what refuses this, and it is what makes the long window
#    safe on a moving belt.
r = Run()
for i in range(5):
    r.step([box(300, 100 + BELT_PX_PER_FRAME * i)])     # establishes the speed
r.gap(12)                                               # ~0.9s at 1244 px/s
assert r.step([box(300, 300)]) == [2], (
    "a moving belt's departed object was revived from a stale position")
print("6. moving belt + long gap -> NOT revived, correctly a new object")

# 7. THE EDGE CASE. An object that comes to rest half out of the bottom of the
#    frame is still detected, frame after frame, from the half still visible.
#    The exit rule drops its track each time, so it used to collect a fresh id
#    each time and be shown to the operator twice. Reported live, 29 Sep — and
#    the belt is stopped for the whole of review, which is exactly when an
#    object sits there being re-detected.
r = Run()
clipped = lambda y, h: [300.0, float(y), 340.0, float(y + h), 0.5, 2]
for y in (1000, 1040, 1080, 1120):                      # decelerating towards it
    r.step([box(300, y)])
parked = [r.step([clipped(1160, 40 - 6 * j)]) for j in range(7)]
assert all(p == [1] for p in parked), (
    "an object parked half out of the frame collected new ids: %s" % parked)
print("7. parked half out of the bottom -> one id, not one per frame")

# 8. But a real exit still ends the id — one frame of a running belt carries an
#    object further than a parked one can drift, which is what tells them apart.
r = Run()
crossing = [r.step([box(300, 100 + BELT_PX_PER_FRAME * i)]) for i in range(13)]
assert crossing[0] == [1] and crossing[-1] != [1], (
    "an object that genuinely left the view kept its id: %s" % crossing)
# It ends with no id rather than a fresh one: by the last step it is clipped by
# the bottom edge, and a half object on its way out is never minted a new id.
assert crossing[-1] == [], crossing
print("8. moving belt, object crosses and exits -> its id ends")

# 9. ...and the next object to enter the top is its own, not the departed one.
r = Run()
for i in range(12):
    r.step([box(300, 100 + BELT_PX_PER_FRAME * i)])
r.gap(3)
assert r.step([box(300, 80)]) == [2], "a new arrival inherited a departed id"
print("9. after an exit, a new object entering the top -> its own id")

# 10. A different lane is a different object, gap or no gap.
r = Run()
r.step([box(300, 400)])
r.gap(6)
assert r.step([box(900, 400)]) == [2], "an id crossed lanes"
print("10. same moment, different lane -> a different object")

# 11. The whole point, end to end: the flicker that produced eleven ids. One
#     object, in and out of detection, over a stop.
r = Run()
ids = set()
for found in [1, 0, 1, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 1]:
    got = r.step([box(300, 400)] if found else [])
    ids.update(got)
assert ids == {1}, "one object still produced several identities: %s" % sorted(ids)
print("11. one object flickering through a stop -> exactly one id: %s" % sorted(ids))

print("ALL RE-IDENTIFICATION CHECKS PASSED")
