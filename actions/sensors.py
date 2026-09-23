"""Small things on the network that report what they can feel.

A five pound board on the wifi is a nerve ending. It cannot think, it
cannot speak, and it has no idea what it is for -- it reads a pin and
posts what it saw. Everything that turns that into something worth
saying lives here, so the board stays disposable and the judgement
stays in one file.

Nothing here touches the network. phone.py receives the report and
hands it over; this decides whether it is worth interrupting anyone,
and in what words. That is the same shape as aircraft.py and the
radar: the thing that fetches never speaks, and the thing that speaks
never fetches.

The hard part is not hearing a sensor. It is staying quiet. A board
reporting every thirty seconds would have JARVIS announce the same
fact two thousand times a day, so the rule throughout is that only a
CHANGE is worth a sentence: a sensor that was gone and has come back,
motion in a room that was empty, a temperature that has actually
moved. Everything else is recorded and swallowed.
"""

import threading
import time


# How long a sensor must have been silent before its return is worth
# announcing again. A board that reports every half minute is simply
# still there; one that vanished for five minutes and came back has
# been unplugged, rebooted, or carried into the room.
ABSENT_SECONDS = 300

# How long after the last movement a room still counts as occupied.
#
# A PIR detects movement of heat, not presence -- sit still for a
# minute and it stops firing, which is the sensor working correctly
# rather than failing. So presence is inferred: recent movement means
# someone is here, and a long gap means they left. Generous, because
# the cost of thinking an empty room is occupied is nothing, and the
# cost of greeting someone who never left is worse.
PRESENT_SECONDS = 600

# How long before the same room may be greeted again, so stepping out
# to the kitchen and back is not an occasion.
GREET_AGAIN_SECONDS = 1800


_lock = threading.Lock()

# name -> {"seen": monotonic, "moved": monotonic|None, "greeted": monotonic|None,
#          "readings": {...}}
_sensors = {}


def _now():
    return time.monotonic()


def _spoken(name):
    """A sensor's name as it should be said aloud."""
    name = (name or "").strip()

    if not name:
        return "A sensor"

    # Capitalise the first letter only. 'front door' becomes 'Front
    # door', not 'Front Door', which reads as a proper noun and is not
    # how anyone says it.
    return name[0].upper() + name[1:]


def known():
    """Every sensor heard from this run, with how long ago."""
    now = _now()

    with _lock:
        return {
            name: {
                "seen_ago": now - state["seen"],
                "readings": dict(state["readings"]),
            }
            for name, state in _sensors.items()
        }


def present(name, within=PRESENT_SECONDS):
    """Whether a room has had movement recently enough to count."""
    with _lock:
        state = _sensors.get(name)
        moved = state and state.get("moved")

    return bool(moved and _now() - moved < within)


def reset():
    """Forget everything. For tests, and for a fresh start."""
    with _lock:
        _sensors.clear()


def report(payload):
    """Take one report from a board. Returns what to say, or None.

    The payload is whatever the board sent, and nothing in it is
    trusted to exist:

        {"name": "office", "event": "online"}
        {"name": "office", "event": "motion"}
        {"name": "office", "temperature": 19.4, "humidity": 48}

    Returning None is the normal case and is not a failure -- it means
    the report was recorded and there was nothing new worth saying.
    """
    if not isinstance(payload, dict):
        return None

    name = (payload.get("name") or "").strip().casefold()

    if not name:
        name = "sensor"

    event = (payload.get("event") or "").strip().casefold()
    now = _now()

    # Set inside the lock, read after it. Initialised here so the name
    # exists on every path rather than only the one that assigns it.
    fresh = False

    with _lock:
        state = _sensors.get(name)

        if state is None:
            state = {"seen": None, "moved": None, "greeted": None,
                     "readings": {}}
            _sensors[name] = state

        # How long it had been quiet BEFORE this report, which is what
        # decides whether its return is news. Read before the clock is
        # updated, because afterwards the gap is always zero.
        previous = state["seen"]
        gap = None if previous is None else now - previous

        state["seen"] = now

        for field in ("temperature", "humidity"):
            value = payload.get(field)

            if isinstance(value, (int, float)):
                state["readings"][field] = float(value)

        if event == "motion":
            was_present = bool(
                state["moved"] and now - state["moved"] < PRESENT_SECONDS
            )
            greeted = state["greeted"]

            state["moved"] = now

            fresh = (
                not was_present
                and (greeted is None
                     or now - greeted > GREET_AGAIN_SECONDS)
            )

            if fresh:
                state["greeted"] = now

        first_time = previous is None
        returning = gap is not None and gap > ABSENT_SECONDS

    # Said outside the lock. Building a sentence is cheap, but nothing
    # that might be handed to another thread happens while holding it.
    if event == "online":
        if first_time or returning:
            return f"{_spoken(name)} sensor online, sir."

        return None

    if event == "motion":
        return "Welcome back, sir." if fresh else None

    return None
