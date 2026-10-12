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

A freeze caused by another thread holding Python's lock shows the HUD
thread waiting in ordinary code, with every other busy thread's place
listed after it, so the culprit is named either way.

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
_started = []


def enabled():
    return (os.getenv("JARVIS_STALL_WATCH") or "on").strip().casefold() not in ("off", "0", "false", "no")


def _where(frame, limit=FRAMES_SHOWN):
    """The innermost [limit] places in [frame]'s stack, innermost last, as file:line function."""
    places = traceback.extract_stack(frame)[-limit:]
    return [f"{os.path.basename(place.filename)}:{place.lineno} {place.name}" for place in places]


def _watch(hud_thread_id, report):
    stalled_since = None
    seen = None

    while True:
        time.sleep(BEAT_MS / 1000.0)
        silent = time.monotonic() - _last_beat[0]

        if silent >= STALL_SECONDS and stalled_since is None:
            stalled_since = _last_beat[0]
            frames = sys._current_frames()
            hud = frames.get(hud_thread_id)
            others = [
                (thread.name, _where(frames[thread.ident], 2))
                for thread in threading.enumerate()
                if thread.ident in frames and thread.ident not in (hud_thread_id, threading.get_ident())
            ]
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


def start(report=_print_report):
    """Start watching the HUD's thread -- the one calling this, after the QApplication exists. Once."""
    if _started or not enabled():
        return

    def beat():
        _last_beat[0] = time.monotonic()

    timer = QTimer()
    timer.timeout.connect(beat)
    timer.start(BEAT_MS)
    _started.append(timer)

    threading.Thread(target=_watch, args=(threading.get_ident(), report), name="stall-watch", daemon=True).start()
