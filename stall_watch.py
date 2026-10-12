"""Says when the HUD froze, for how long, and what the HUD's thread was doing then.

The HUD froze for a second or two at start-up (12 October 2026) and the
console said nothing about it, so there was nothing to fix it from. This
measures instead of guessing: a Qt timer on the HUD's own thread marks
each time it runs, and a watcher on a thread of its own notices when the
marks stop. Past STALL_SECONDS it takes the HUD thread's stack at that
moment -- what it was stuck in -- and once the HUD runs again prints how
long it was frozen with that stack:

    [JARVIS] HUD froze for 1.6s, in:
      main.py:731 run
      speech.py:1340 start_voice
      ...

A freeze caused by another thread holding Python's lock for a long time
-- a model loading in C, say -- stops this watcher too, so the HUD's own
beat could not be told apart from it. The watcher times its own sleep as
well: one that overran by STALL_SECONDS means everything paused, and it
says so, with what each busy thread was doing as it woke.

    JARVIS_STALL_WATCH=off   in .env turns it off
"""

import os
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QTimer


STALL_SECONDS = 0.5
BEAT_MS = 100
FRAMES_SHOWN = 6

_last_beat = [time.monotonic()]
_sleep = time.sleep
_started = []


def enabled():
    return (os.getenv("JARVIS_STALL_WATCH") or "on").strip().casefold() not in ("off", "0", "false", "no")


def _where(frame, limit=FRAMES_SHOWN):
    """The innermost [limit] places in [frame]'s stack, innermost last, as file:line function."""
    places = traceback.extract_stack(frame)[-limit:]
    return [f"{os.path.basename(place.filename)}:{place.lineno} {place.name}" for place in places]


def _busy_threads(skip):
    frames = sys._current_frames()
    return [
        (thread.name, _where(frames[thread.ident], 2))
        for thread in threading.enumerate()
        if thread.ident in frames and thread.ident not in skip
    ]


def _watch(hud_thread_id, report, report_pause):
    stalled_since = None
    seen = None

    while True:
        asleep = time.monotonic()
        _sleep(BEAT_MS / 1000.0)
        overslept = time.monotonic() - asleep - BEAT_MS / 1000.0

        # This thread was stopped as well: something held Python's lock.
        if overslept >= STALL_SECONDS:
            report_pause(overslept, _busy_threads({threading.get_ident()}))

        silent = time.monotonic() - _last_beat[0]

        if silent >= STALL_SECONDS and stalled_since is None:
            stalled_since = _last_beat[0]
            hud = sys._current_frames().get(hud_thread_id)
            others = _busy_threads({hud_thread_id, threading.get_ident()})
            seen = (_where(hud) if hud is not None else [], others)

        elif silent < STALL_SECONDS and stalled_since is not None:
            report(_last_beat[0] - stalled_since, *seen)
            stalled_since = None


def _print_report(seconds, hud_stack, others):
    lines = [f"[JARVIS] HUD froze for {seconds:.1f}s, in:"]
    lines += [f"  {place}" for place in hud_stack]
    busy = [f"  {name}: {' <- '.join(reversed(places))}" for name, places in others
            if places and not places[-1].endswith(("wait", "sleep", "select", "_wait_for_tstate_lock", "accept"))]

    if busy:
        lines.append(" other threads then:")
        lines += busy[:8]

    print("\n".join(lines), flush=True)


def _print_pause(seconds, threads):
    lines = [f"[JARVIS] everything paused for {seconds:.1f}s (a thread held Python's lock); threads then:"]
    lines += [f"  {name}: {' <- '.join(reversed(places))}" for name, places in threads if places][:10]
    print("\n".join(lines), flush=True)


def start(report=_print_report, report_pause=_print_pause):
    """Start watching the HUD's thread -- the one calling this, after the QApplication exists. Once.

    Measured from here: the time before it is the imports, not the HUD.
    """
    if _started or not enabled():
        return

    _last_beat[0] = time.monotonic()

    def beat():
        _last_beat[0] = time.monotonic()

    timer = QTimer()
    timer.timeout.connect(beat)
    timer.start(BEAT_MS)
    _started.append(timer)

    threading.Thread(target=_watch, args=(threading.get_ident(), report, report_pause), name="stall-watch",
                     daemon=True).start()
