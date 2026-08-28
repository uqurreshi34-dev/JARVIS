"""Behavioural patterns JARVIS notices, kept separate from what he's told.

memory.py holds facts you stated outright -- "my name is X" -- and every
line in it is something a person can point to and say "yes, I said that."
A pattern is a different kind of thing entirely: a conclusion JARVIS
draws from watching what you actually do, never something you told him
directly. Keeping the two in separate files means you can always tell
"JARVIS knows this because I said so" from "JARVIS is guessing this from
habit" -- blurring that line would quietly undermine the whole reason
memory.py is trustworthy.

A pattern only ever gets two things done with it automatically: it gets
suggested once, and if you say yes, it starts running on its own from
then on. It is never silently promoted to running without being asked
first, and asking again after being ignored once would be exactly the
kind of nagging this project has deliberately avoided everywhere else.
"""

import os
import re
import threading
import time
from datetime import datetime

from actions import files, journal, safety


FILENAME = "patterns.txt"

# How many days of journal history to look at when checking for patterns.
LOOKBACK_DAYS = 30

# A pattern must appear at least this many times, on at least this many
# distinct calendar days, before it is even eligible to be suggested.
# Chosen deliberately conservative: better to stay quiet longer than to
# announce a habit from a handful of occurrences in one busy afternoon.
MIN_OCCURRENCES = 5
MIN_DISTINCT_DAYS = 4

# Same intent within this many minutes of each other counts as "the same
# time of day" -- "9:03" and "9:22" are clearly one habit; requiring an
# exact minute match would never find anything real.
TIME_WINDOW_MINUTES = 30

# Intents that make sense to notice a time-of-day habit for. Deliberately
# a fixed, small list rather than "any intent" -- some things (clearing
# the clipboard, answering a one-off question) have no business being
# auto-run just because they happened a few times at a similar hour.
_ELIGIBLE_INTENTS = frozenset({
    "market_report", "get_weather", "get_system_status", "show_news",
    "read_log", "log_summary", "read_notes", "read_calendar",
})

# Short, stable spoken labels, generated once at creation and never
# regenerated -- so "the markets report pattern" stays the same phrase
# to say later even if the detection details shift. Deliberately
# descriptive rather than numbered ("pattern 1"), so it's clear what's
# being removed without needing to ask first.
_LABELS = {
    "market_report": "the markets report pattern",
    "get_weather": "the weather pattern",
    "get_system_status": "the system status pattern",
    "show_news": "the news pattern",
    "read_log": "the activity log pattern",
    "log_summary": "the daily summary pattern",
    "read_notes": "the notes pattern",
    "read_calendar": "the calendar pattern",
}

_lock = threading.Lock()

# key: value lines, same shape as memory.py.
_LINE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.+)$", re.IGNORECASE)


def _path():
    base = files.root()

    return os.path.join(base, FILENAME) if base else None


def _read():
    path = _path()

    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return [
                line.strip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError as error:
        print(f"[JARVIS] could not read patterns: {error}")
        return []


def _write(lines):
    path = _path()

    if not path:
        return False

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "# Patterns JARVIS has noticed from what you actually do.\n"
                "# Separate from memory.txt on purpose: these are\n"
                "# conclusions, not things you were told to be true.\n"
                "# Edit or delete anything here; it is read at startup.\n"
            )

            for line in lines:
                handle.write(f"{line}\n")

        return True
    except OSError as error:
        print(f"[JARVIS] could not write patterns: {error}")
        return False


def _entries():
    """Every stored pattern as a dict, parsed from its "key: value" lines.

    Each pattern is stored as a run of lines sharing one "label" line to
    start it off, so multiple patterns in the file don't need a nested
    format -- reads the same way memory.txt does, one fact per line.
    """
    patterns = []
    current = None

    for line in _read():
        match = _LINE.match(line)

        if not match:
            continue

        key, value = match.group(1).strip().casefold(), match.group(2).strip()

        if key == "label":
            if current:
                patterns.append(current)

            current = {"label": value}
        elif current is not None:
            current[key] = value

    if current:
        patterns.append(current)

    return patterns


def _serialise(pattern):
    lines = [f"label: {pattern['label']}"]

    for key in ("intent", "hour", "minute", "status", "created"):
        if key in pattern:
            lines.append(f"{key}: {pattern[key]}")

    return lines


def _save_all(patterns):
    lines = []

    for pattern in patterns:
        lines.extend(_serialise(pattern))

    return _write(lines)


def _label_for(intent):
    return _LABELS.get(intent, f"the {intent.replace('_', ' ')} pattern")


def _cluster_by_time(occurrences):
    """Group (hour, minute) occurrences of one intent into clusters no
    more than TIME_WINDOW_MINUTES apart, returning the largest cluster's
    (occurrences, distinct_days, representative_hour, representative_minute).

    A simple pass is enough here: sort by minute-of-day, then split
    wherever the gap to the next occurrence exceeds the window. Good
    enough for a once-a-day habit, which is the only shape this is
    meant to catch -- it isn't trying to be a general clustering
    algorithm.
    """
    by_minute_of_day = sorted(occurrences, key=lambda o: o[1] * 60 + o[2])

    clusters = []
    current = [by_minute_of_day[0]]

    for entry in by_minute_of_day[1:]:
        previous = current[-1]
        gap = (entry[1] * 60 + entry[2]) - (previous[1] * 60 + previous[2])

        if gap <= TIME_WINDOW_MINUTES:
            current.append(entry)
        else:
            clusters.append(current)
            current = [entry]

    clusters.append(current)

    best = max(clusters, key=len)
    distinct_days = len({date for date, _, _ in best})

    mid = best[len(best) // 2]

    return len(best), distinct_days, mid[1], mid[2]


def detect():
    """Look at journal history for a new pattern worth suggesting.

    Returns a pattern dict ready to be suggested, or None. Only ever
    returns ONE pattern per call -- the caller decides when it's a good
    moment to actually mention it, and suggesting several habits at once
    would be exactly the kind of pattern a person can't act on anyway.
    Never returns a pattern that's already stored, suggested or not.
    """
    history = journal.command_history(days=LOOKBACK_DAYS)

    if not history:
        return None

    known_intents = {p.get("intent") for p in _entries()}

    by_intent = {}

    for date, hour, minute, intent in history:
        if intent not in _ELIGIBLE_INTENTS or intent in known_intents:
            continue

        by_intent.setdefault(intent, []).append((date, hour, minute))

    for intent, occurrences in by_intent.items():
        if len(occurrences) < MIN_OCCURRENCES:
            continue

        count, distinct_days, hour, minute = _cluster_by_time(occurrences)

        if count < MIN_OCCURRENCES or distinct_days < MIN_DISTINCT_DAYS:
            continue

        return {
            "label": _label_for(intent),
            "intent": intent,
            "hour": str(hour),
            "minute": str(minute),
            "status": "suggested",
            "created": datetime.now().strftime("%Y-%m-%d"),
        }

    return None


def store_suggested(pattern):
    """Save a newly detected pattern as suggested-but-not-confirmed."""
    with _lock:
        patterns = _entries()
        patterns.append(pattern)

        return _save_all(patterns)


def confirm(label):
    """Flip a suggested pattern to auto-running. Returns True if found."""
    wanted = safety.clean(label).casefold()

    with _lock:
        patterns = _entries()
        found = False

        for pattern in patterns:
            if pattern.get("label", "").casefold() == wanted:
                pattern["status"] = "auto"
                found = True

        if found:
            _save_all(patterns)

        return found


def decline(label):
    """Remove a suggested pattern outright rather than leave it pending
    forever -- declining should mean "stop asking", not "ask again"."""
    return forget(label)


def forget(label):
    """Remove one pattern by its exact label. Returns True if found."""
    wanted = safety.clean(label).casefold()

    with _lock:
        patterns = _entries()
        kept = [
            p for p in patterns if p.get("label", "").casefold() != wanted
        ]
        removed = len(kept) != len(patterns)

        if removed:
            _save_all(kept)

        return removed


def forget_all():
    """Clear every stored pattern. Returns how many were removed."""
    with _lock:
        patterns = _entries()

        if patterns:
            _write([])

        return len(patterns)


def auto_running():
    """Every pattern currently set to run without asking."""
    return [p for p in _entries() if p.get("status") == "auto"]


def due_now(tolerance_minutes=5):
    """Auto-running patterns whose time is right now, within tolerance.

    Checked by the caller on some regular interval -- this module has
    no scheduler of its own, the same way market_monitor and the
    reminder system each run their own loop rather than this file
    reaching into theirs.
    """
    now = datetime.now()
    due = []

    for pattern in auto_running():
        try:
            hour, minute = int(pattern["hour"]), int(pattern["minute"])
        except (KeyError, ValueError):
            continue

        target_minutes = hour * 60 + minute
        now_minutes = now.hour * 60 + now.minute

        if abs(target_minutes - now_minutes) <= tolerance_minutes:
            due.append(pattern)

    return due


def describe():
    """A spoken summary of every stored pattern, suggested or running."""
    patterns = _entries()

    if not patterns:
        return "I haven't noticed any patterns yet, sir."

    parts = []

    for pattern in patterns:
        label = pattern.get("label", "a pattern")
        status = pattern.get("status")

        if status == "auto":
            parts.append(f"{label}, running automatically")
        else:
            parts.append(f"{label}, awaiting your confirmation")

    return f"I've noticed {len(patterns)}: " + "; ".join(parts) + ", sir."


def spoken_suggestion(pattern):
    """How a newly detected pattern is offered, once."""
    hour, minute = int(pattern["hour"]), int(pattern["minute"])
    label = pattern["label"]

    time_phrase = f"{hour % 12 or 12}" + (
        f":{minute:02d}" if minute else ""
    ) + (" a.m." if hour < 12 else " p.m.")

    return (
        f"I've noticed you usually ask about this around {time_phrase}, "
        f"sir -- shall I make {label} run automatically from now on?"
    )


# How often to check whether an auto-running pattern is due. Matches
# due_now's own tolerance window sensibly -- checking much less often
# risks missing the window entirely.
_DUE_CHECK_SECONDS = 60

# How often to look for a brand new pattern. Detection reads the whole
# journal history, which is real work -- there is no reason to repeat
# it on a tight loop, since a pattern that has just cleared the bar
# will still be there an hour from now.
_DETECT_CHECK_SECONDS = 3600


class PatternMonitor:
    """Watches for auto-running patterns coming due, and for new
    patterns worth suggesting -- the same set_listener/start/stop shape
    as MarketMonitor, battery_monitor, and watcher."""

    def __init__(self):
        self._due_listener = None
        self._suggestion_listener = None
        self._stop = threading.Event()
        self._thread = None
        self._last_detect_check = 0.0
        # A pattern is only ever suggested once per process run, even
        # if it stays undetected-as-declined (declining removes it
        # outright, but an ignored suggestion that's simply never
        # answered should not be repeated on every check either).
        self._already_suggested = set()
        # label -> the date it last actually ran. Without this, a
        # pattern due for the next _DUE_CHECK_SECONDS-to-tolerance
        # window would fire on every single check inside that window --
        # the markets report read out five times over five minutes
        # rather than once, since the due window is necessarily wider
        # than one check interval.
        self._last_run_date = {}

    def set_due_listener(self, listener):
        """Register a callable taking a list of due patterns."""
        self._due_listener = listener

    def set_suggestion_listener(self, listener):
        """Register a callable taking one newly detected pattern."""
        self._suggestion_listener = listener

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                today = datetime.now().strftime("%Y-%m-%d")

                due = [
                    p for p in due_now()
                    if self._last_run_date.get(p["label"]) != today
                ]

                if due and self._due_listener:
                    for pattern in due:
                        self._last_run_date[pattern["label"]] = today

                    self._due_listener(due)

                now = time.monotonic()

                if now - self._last_detect_check >= _DETECT_CHECK_SECONDS:
                    self._last_detect_check = now
                    found = detect()

                    if found and found["label"] not in self._already_suggested:
                        self._already_suggested.add(found["label"])
                        store_suggested(found)

                        if self._suggestion_listener:
                            self._suggestion_listener(found)

            except Exception as error:
                print(f"[JARVIS] pattern check failed: {error}")

            self._stop.wait(_DUE_CHECK_SECONDS)


pattern_monitor = PatternMonitor()
