"""Object identity and track staleness (app/services/sort.py).

Run from the backend root with the service virtualenv:
    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_tracking.py

Staleness is asserted at three frame rates because it is the property that
breaks silently when the pipeline gets faster: expressed in frames, a track
that survives 0.3s at 10Hz survives only 70ms at 43Hz, and objects still under
the camera come back as new ids and are counted again.
"""
import sys; sys.path.insert(0, '.')
from app.services.sort import ObjectTracker

box = lambda x, y: [x, y, x + 40, y + 40, 0.9, 1]
SIZE = (1200, 1920)

# --- identity: a follower gets its own id, and two detections in one frame
#     cannot both claim the same track ---------------------------------------
t = ObjectTracker(x_tolerance=10)
t.update([box(300, 400)], 1, SIZE, now=0.0)
t.update([box(300, 420), box(300, 340)], 2, SIZE, now=0.1)
assert len(t.get_tracked_objects()) == 2, "follower did not get its own id"

t2 = ObjectTracker(x_tolerance=10)
t2.update([box(300, 400)], 1, SIZE, now=0.0)
t2.update([box(300, 402), box(300, 404)], 2, SIZE, now=0.1)
assert len(t2.get_tracked_objects()) == 2, "two detections claimed one track"
print("identity checks passed")

# --- staleness is the SAME in wall-clock terms at 10Hz and at 43Hz -----------
def frames_survived(fps):
    t = ObjectTracker(x_tolerance=10, stale_after_seconds=0.3)
    dt = 1.0 / fps
    now = 0.0
    t.update([box(300, 400)], 0, SIZE, now=now)
    n = 0
    while t.get_tracked_objects():
        n += 1
        now += dt
        t.update([], n, SIZE, now=now)          # object not detected any more
    return n * dt

for fps in (10, 21, 43):
    survived = frames_survived(fps)
    print("  at %2d Hz a lost track survives %.2fs" % (fps, survived))
    assert 0.30 <= survived <= 0.30 + 1.0 / fps, survived
print("rate-independence passed")

# --- a long review pause must NOT evict tracks ------------------------------
t = ObjectTracker(x_tolerance=10, stale_after_seconds=0.3)
t.update([box(300, 400)], 1, SIZE, now=0.0)
t.update([box(300, 400)], 2, SIZE, now=0.023)
assert t.get_tracked_objects(), "track lost before the pause"
# Operator reviews a detection for four minutes; capture is paused, so update()
# is simply not called. The next call comes 240s later.
t.update([box(300, 400)], 3, SIZE, now=240.0)
assert 1 in t.get_tracked_objects(), "a review pause evicted the track — the "\
    "object still under the camera would be re-counted as a new one"
assert t.get_tracked_objects()[1]['seen_at'] > 0, t.get_tracked_objects()
print("pause-immunity passed")

# --- the exit-zone rule still fires, and names itself in the diagnostic -----
t = ObjectTracker(x_tolerance=10)
# A detection already clipped by the bottom edge is half an object on its way
# out and is never given an id of its own — it would only be evicted again on
# the same update, once per frame, each time under a new id.
t.update([box(300, 1160)], 1, SIZE, now=0.0)
assert t.get_tracked_objects() == {}, t.get_tracked_objects()
assert t.last_evicted == {}, (
    "a half object at the bottom edge was minted an id just to evict it: %s"
    % t.last_evicted)
assert t.last_assignment == [None], t.last_assignment
print("bottom-edge mint refused:", t.last_assignment)

# An object tracked from inside the frame still hits the exit rule on its way
# out, and still names itself in the diagnostic.
t = ObjectTracker(x_tolerance=10)
t.update([box(300, 900)], 1, SIZE, now=0.0)
assert list(t.get_tracked_objects()) == [1], t.get_tracked_objects()
t.update([box(300, 1155)], 2, SIZE, now=0.05)
assert t.get_tracked_objects() == {}
assert "exit-zone" in list(t.last_evicted.values())[0], t.last_evicted
print("exit-zone check passed:", t.last_evicted)

# --- belt speed is recovered from tracked motion ----------------------------
# Object stepping 186px down the frame every 23ms is 8087 px/s.
t = ObjectTracker(x_tolerance=10, stale_after_seconds=0.3)
now, y = 0.0, 100
t.update([box(300, y)], 0, SIZE, now=now)
for i in range(1, 6):
    now += 0.023
    y += 186
    t.update([box(300, y)], i, SIZE, now=now)
speed = t.belt_speed_px_s
print("  measured belt speed: %.0f px/s" % speed)
assert 8000 <= speed <= 8200, speed
# A stationary object must not be read as motion.
t2 = ObjectTracker(x_tolerance=10, stale_after_seconds=10.0)
for i in range(5):
    t2.update([box(300, 400)], i, SIZE, now=i * 0.023)
assert t2.belt_speed_px_s is None, t2.belt_speed_px_s
print("belt-speed measurement passed")

print("ALL TRACKER CHECKS PASSED")
