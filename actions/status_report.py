"""How JARVIS is, in a few sentences: "status report".

One answer for everything that can quietly go wrong: which connected
services are up and which are not, whether the boards are reporting, how
whole today's sensor record is, how quickly he has been answering, and
what the model calls of the last day used, with how much of it came from
the cache. Nothing is fetched and no model is asked: each part reads what
JARVIS already keeps.

    status report / give me a status report / run diagnostics / run a self check

Also here: right_now(), a few lines of what JARVIS can see at this moment
(the time, each room's readings, how today has gone), given to the model
as background when he answers a general question, so "should I open a
window" or "what do you think of the room today" gets an answer grounded
in the room rather than a guess.
"""

import threading
import time
from datetime import datetime

from actions import sensor_history, sensors


# ---- how quickly he answers ---------------------------------------------------------------

# The last this many turns: enough for a fair average, small enough that an
# old slow afternoon does not hide today's.
TURNS_KEPT = 50

_lock = threading.Lock()
_turns = []          # seconds from hearing you to his first sound


def note_turn(seconds):
    """main.py: one turn took [seconds] from the words being heard to his first sound."""
    if not isinstance(seconds, (int, float)) or seconds < 0 or seconds > 600:
        return

    with _lock:
        _turns.append(float(seconds))
        del _turns[:-TURNS_KEPT]


def reply_seconds():
    """(average, slowest, how many) over the turns kept, or None before any."""
    with _lock:
        turns = list(_turns)

    if not turns:
        return None

    return sum(turns) / len(turns), max(turns), len(turns)


def forget():
    """Drop the turns kept. For tests."""
    with _lock:
        _turns.clear()


# ---- the parts of the report ------------------------------------------------------------------

def _listed(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _services():
    from actions import mcp_services

    try:
        names = list(mcp_services.configured())
    except Exception:
        return None

    if not names:
        return None

    up = [name for name in names if mcp_services.connected(name)]
    down = [name for name in names if name not in up]
    spoken = lambda name: name.upper() if len(name) <= 3 else name.replace("_", " ")  # noqa: E731

    if not down:
        return f"All {len(up)} connected services are up."

    if not up:
        return f"None of the connected services is up: {_listed(map(spoken, down))}."

    return f"{len(up)} of {len(names)} connected services are up; {_listed(map(spoken, down))} {'is' if len(down) == 1 else 'are'} not."


def _boards():
    known = sensors.known()

    if not known:
        return "No sensor board has reported since I started."

    live = sorted(name for name, board in known.items() if board["seen_ago"] < 120)
    quiet = sorted(name for name in known if name not in live)
    said = []

    if live:
        newest = min(known[name]["seen_ago"] for name in live)
        last = "just now" if newest < 5 else f"{int(newest)} seconds ago"
        said.append(f"{len(live)} sensor board{'s' if len(live) != 1 else ''} reporting, last heard {last}")

    if quiet:
        said.append(f"{_listed(quiet)} silent for {int(min(known[name]['seen_ago'] for name in quiet) // 60)} minutes")

    return "; ".join(said)[:1].upper() + "; ".join(said)[1:] + "."


def _record(now=None):
    """Today's sensor record: how many readings, and how many holes in it."""
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now)
    start = datetime(moment.year, moment.month, moment.day).timestamp()

    try:
        rows = sensor_history._rows("SELECT ts, board FROM readings WHERE ts >= ? ORDER BY ts", (start,))
    except Exception:
        return None

    if not rows:
        return None

    holes = 0

    for board in {board for _, board in rows}:
        holes += len(sensor_history.runs([(ts, None) for ts, name in rows if name == board])) - 1

    said = f"Today's sensor record has {len(rows)} entries"
    return said + (", with no gaps." if not holes else f" and {holes} gap{'s' if holes != 1 else ''} in it.")


def _replies():
    timing = reply_seconds()

    if timing is None:
        return None

    average, slowest, count = timing
    return (f"Over the last {count} repl{'ies' if count != 1 else 'y'} I've taken {average:.1f} seconds on average "
            f"from hearing you to my first word, {slowest:.1f} at the slowest.")


def _rounded(tokens):
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f} million"

    if tokens >= 10_000:
        return f"{round(tokens / 1000)} thousand"

    return f"{tokens:,}"


def _usage(summary=None):
    if summary is None:
        try:
            import usage

            summary = usage.summary(days=1)
        except Exception:
            return None

    calls = summary.get("calls") or 0

    if not calls:
        return "No model calls in the last 24 hours."

    fresh = summary.get("in", 0) + summary.get("cache_write", 0)
    read = summary.get("cache_read", 0)
    said = (f"{calls} model call{'s' if calls != 1 else ''} in the last 24 hours, "
            f"{_rounded(fresh + read + summary.get('out', 0))} tokens")

    if fresh + read:
        said += f", {round(100 * read / (fresh + read))} percent of the input read from the cache"

    return said + "."


def report():
    """The status report, as said."""
    parts = [part for part in (_services(), _boards(), _record(), _replies(), _usage()) if part]
    parts[0] = "Status report, sir. " + parts[0]
    return " ".join(parts)


# ---- what JARVIS can see right now, for a general question --------------------------------------

def right_now(now=None):
    """A few lines of the present, for the model's background: the time and each room's readings, or ""."""
    moment = datetime.fromtimestamp(time.time() if now is None else now)
    lines = [f"It is {moment.strftime('%A %d %B %Y, %H:%M')}."]

    for name, board in sorted(sensors.known().items()):
        if board["seen_ago"] >= 120:
            continue

        readings = board["readings"]
        place = sensor_history._place(name)
        parts = []

        if "temperature" in readings:
            parts.append(f"{readings['temperature']:.1f} C")

        if "humidity" in readings:
            parts.append(f"{readings['humidity']:.0f}% humidity")

        if board["moved_ago"] is not None:
            parts.append(f"movement {int(board['moved_ago'] // 60)} minutes ago")

        if parts:
            line = f"In {place} now: {', '.join(parts)}."

            try:
                stretch = sensor_history._so_far(moment.timestamp())
                rows = sensor_history._readings("temperature", [name], stretch)
            except Exception:
                rows = []

            if rows:
                values = [value for _, value in rows]
                when = stretch["spoken"][:1].upper() + stretch["spoken"][1:]

                if max(values) - min(values) < 0.2:
                    line += f" {when} it has stayed around {sum(values) / len(values):.1f} C."
                else:
                    line += f" {when} it has ranged {min(values):.1f} to {max(values):.1f} C."

            lines.append(line)

    return "\n".join(lines)
