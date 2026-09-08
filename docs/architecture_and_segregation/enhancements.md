# Enhancements — Beyond a Straight Legacy Port

The guiding rule for this whole project was "segregation only, behavior
identical to legacy" (see `1 - strategy.md`). A handful of places deliberately
depart from that rule anyway, because matching legacy exactly would have meant
reproducing a genuine bug, a silent failure mode, or a gap the web
architecture doesn't share. This document keeps those separate from ordinary
correctness fixes (which just made the port match legacy — see
`8 - remediation_log.md` / `9 - post_remediation_session_log.md`) and lists
them alongside further enhancements worth considering but not yet done.

## Enhancements already made

### Backend
- **Fixed a genuine legacy FM-counting bug: the count no longer inflates for
  detections that were never actually shown to the operator.** Legacy's
  `handle_detection` (`main.py:2597-2622`) adds every new tracker id to
  `existing_track_ids` (and `total_fo_detected` is exactly its length)
  **unconditionally** — even one the `has_similar_x_axis` check decided NOT to
  queue for operator review because a similar-looking detection was already
  pending. Only the operator-facing queue was ever actually gated; the count
  climbed regardless. Confirmed as a real, pre-existing legacy defect by
  reading `main.py`/`sort.py` directly (not a porting error) — legacy's own
  object tracker (`x_tolerance=10`px) can mint a "new" track id for a
  completely stationary object from ordinary frame-to-frame detection jitter,
  since neither legacy's nor this port's inference loop is gated on the
  conveyor actually moving. On the ground this silently inflated the FM count
  any time the belt was stopped (e.g. by the FM interlock itself) while an
  object stayed under the camera — first noticed on this dev unit as log
  lines like `FM detected but skipped (similar x-axis already pending)`
  appearing repeatedly with the belt stopped, while the on-screen FM count
  kept climbing anyway. Fixed in `scan_session.py`'s `process_frame`: an id
  suppressed by `has_similar_x_axis` is now left out of `existing_track_ids`
  entirely, so it doesn't inflate the count, and it's still eligible to be
  properly detected and counted once whatever it collided with in `pending`
  clears. The log line was also reworded, from "FM detected but **skipped**"
  (misleadingly implies nothing happened) to "FM detected but **not counted or
  queued**" (states plainly that neither happened, matching the new
  behavior).
- **Also gated FM counting directly on belt motion, closing the same bug at
  its root rather than only its symptom.** The fix above only stops a
  suppressed id from inflating the count when it happens to collide with
  something already in `pending` — it does nothing for a false detection that
  fires after `pending` has already cleared, or the very first false trigger
  after a stop, since there's nothing for `has_similar_x_axis` to match
  against in either case. Added a second, more direct check in
  `process_frame`: if `conveyor_service.machine_start_locked` is already
  `True` (an FM stop already in effect, waiting on Resume/Forward/Submit),
  **no** new tracker id is counted or queued at all, full stop — a new
  physical object cannot legitimately appear under the camera while the belt
  isn't moving, so nothing "new" should be believed while it isn't. This is a
  real behavior change from legacy, not present there in any form (legacy's
  `handle_detection` has no belt-motion check whatsoever), made because the
  bug it closes was reproducible and actively confusing during this project's
  own testing (see the log excerpt in `9 - post_remediation_session_log.md`
  if that gets recorded, or this conversation's own log lines showing the FM
  count climbing while the belt sat stopped and locked).
- **Real HTTP error codes instead of silent failure.** Legacy's conveyor
  commands returned `200 {"success": false}` (or nothing at all — a warning
  only in a log file the operator never sees) for both a blocked interlock and
  a genuine hardware failure. The backend now returns `409` (interlock
  blocked, an expected operating state) and `502` (real failure) with a
  message, so the frontend can show the operator what actually happened
  instead of a generic error or nothing.
- **Data Collection's finish step cleans up unconditionally.** Legacy's
  `submit_dc`/`back_from_dc` just navigate away — if the operator forgot to
  press Stop first, the frame-recording thread and the belt both keep running
  in the background indefinitely. `POST /api/camera/data_collection/finish`
  stops both before the frontend navigates away, closing that leak.
- **The Qualix bearer token is real.** Legacy issued the literal string
  `"dummy_offline_token"` for an offline login, which nothing downstream could
  actually authenticate with. The backend issues a real session token
  (`SessionStore`, 45-day TTL) for both the online and offline login paths.

### Frontend
- **The live-scan sidebar (Start/Stop, Blower FO/Magnetic FO, Submit Batch)
  stays visible even once an FM detection locks the belt**, instead of
  disappearing the way legacy's equivalent page does. This was added after a
  real operational problem on the dev unit: the FM interlock was tripping on
  an empty belt (a separate detection-quality issue, tracked on its own), and
  legacy's page-switch design has no way to finish/submit the batch from the
  locked screen at all — the operator would be stuck until the false
  detection was dismissed. Start and Stop are disabled while locked (Start
  because the interlock refuses it anyway; Stop because it would send a
  redundant `all_stop` that still increments the "Manual Stop Count" metric,
  corrupting `looker_data` for a stop that didn't really happen), but Submit
  Batch works the same as always, since `/api/scan/submit` only requires an
  active scan session, not an unlocked belt.
- **Numeric fields reject non-numeric input as you type**, rather than only
  validating after submission. Legacy (and an early version of this port)
  let you type anything into Blower FO/Magnetic FO/Sorting Quantity and only
  complained once you pressed Submit; these fields now strip non-digit
  characters immediately.
- **Confirmation before leaving an active scan.** Legacy has no protection at
  all against accidentally navigating away mid-scan — the desktop app simply
  doesn't have a "back" gesture in the same sense a browser does. The web
  version added confirmation prompts for every way a browser lets you leave a
  page: an in-app Back button, Cancel Batch, the browser's own Back button,
  and tab close/reload.
- **Loading vs. genuinely-empty dropdowns are visually distinct.** Legacy
  dropdowns look identical whether their options haven't arrived yet or there
  genuinely are none. Server-populated dropdowns (Vendor Name, Brand, Sorter
  Name) now show "Loading…" until their data arrives, so an empty list never
  looks broken.
- **Network/CORS failures are distinguished from a real wrong password** on
  the login screen. Legacy (and an earlier version of this port) showed the
  same "Invalid credentials"-style message regardless of the actual cause,
  which was actively misleading while diagnosing an unrelated networking bug
  during this project — see `9 - post_remediation_session_log.md`.
- **A New Batch draft survives navigating away and back**, instead of
  silently discarding everything typed. This one exists specifically *because*
  of the web architecture (a React page component is destroyed and rebuilt on
  navigation; legacy's equivalent Qt page object never was), not because
  legacy did anything better here — but the end result is a genuine
  improvement in operator experience.

## Suggested future enhancements

These are not yet built. Some originate from open questions raised during
this project; they're listed here as options for the team to weigh in on
alongside the more clear-cut open work in `todos.md`.

- **Collapse Submit Batch back into one step.** Legacy's real behavior — and
  what this port now deliberately reproduces — is a two-step Submit-then-
  confirm flow where a batch is never saved or synced at all if the operator
  doesn't reach the second click (see `todos.md`). A single-step version
  (compute, save, and sync all in the one Submit click) was actually how this
  port worked for most of this project, before being deliberately changed
  back to match legacy exactly on request. Worth a real decision: match
  legacy's fragility, or keep the safer single-step behavior as an
  intentional improvement.
- **A periodic sweep for orphaned results**, as a safety net *underneath*
  whichever Submit design is chosen: find any `result.json` on disk with no
  matching database row (e.g., because the operator never confirmed) and
  save/sync it anyway on a timer. This would remove the two-step flow's risk
  without changing its UX.
- **Expose real status for the camera blower / camera light / belt blower /
  vibrator / hopper door / sensor hardware visible on the machine.** Neither
  legacy nor this port has any visibility into these at all — the software
  only ever sends five abstract commands and has no idea which physical
  components respond to them (see `9 - post_remediation_session_log.md` §7).
  If the machine's controller can report individual component status, surfacing
  it in the UI would be a genuine capability legacy never had, not just a
  port of one.
- **Finalize the PWA manifest's branding** (icon, theme color) to match
  legacy's actual "Compass Eye" identity instead of the current placeholder
  values — see `3 - frontend_setup_walkthrough.md` and
  `10 - pwa_and_deployment_rollout.md`.
- **Fix XAI View for real**, rather than just wiring the frontend button to
  the existing (currently broken) backend endpoint. See `todos.md` for the
  two candidate approaches already identified.
