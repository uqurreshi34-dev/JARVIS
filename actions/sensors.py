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

import re
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

# How long before you may be greeted again, so stepping out to the
# kitchen and back is not an occasion.
GREET_AGAIN_SECONDS = 1800

# "Welcome back" is about you coming back, not about which room you are
# in: walking from the hall to the kitchen with a sensor in each is one
# person already home, not two arrivals. So the greeting is decided for
# the whole house -- only when no sensor anywhere has seen movement
# lately, and at most once per GREET_AGAIN_SECONDS -- never per room.
_last_greeting = None


_lock = threading.Lock()

# name -> {"seen": monotonic, "moved": monotonic|None, "greeted": monotonic|None,
#          "readings": {...}}
_sensors = {}

# Told of every report, with known() as it stands after it -- the HUD's
# sensor panel. Called on the web server's thread; whatever listens hands
# it to its own thread, as the panel does with a Qt signal.
_listener = None


def set_listener(callback):
    """Have [callback] called with known() after every report."""
    global _listener
    _listener = callback


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
                "moved_ago": None if state["moved"] is None else now - state["moved"],
                "read_ago": None if state.get("read") is None else now - state["read"],
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
    global _last_greeting

    with _lock:
        _sensors.clear()
        _last_greeting = None


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
    global _last_greeting

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
                     "read": None, "readings": {}}
            _sensors[name] = state

        # How long it had been quiet BEFORE this report, which is what
        # decides whether its return is news. Read before the clock is
        # updated, because afterwards the gap is always zero.
        previous = state["seen"]
        gap = None if previous is None else now - previous

        state["seen"] = now

        for field in ("temperature", "humidity"):
            value = payload.get(field)

            if isinstance(value, (int, float)) and not isinstance(value, bool):
                state["readings"][field] = float(value)
                state["read"] = now

        if event == "motion":
            # Anyone already about, anywhere the sensors can see?
            someone_home = any(
                other["moved"] and now - other["moved"] < PRESENT_SECONDS
                for other in _sensors.values()
            )

            state["moved"] = now

            fresh = (
                not someone_home
                and (_last_greeting is None
                     or now - _last_greeting > GREET_AGAIN_SECONDS)
            )

            if fresh:
                _last_greeting = now
                state["greeted"] = now

        first_time = previous is None
        returning = gap is not None and gap > ABSENT_SECONDS

    # Said outside the lock. Building a sentence is cheap, but nothing
    # that might be handed to another thread happens while holding it.
    if _listener is not None:
        try:
            _listener(known())
        except Exception as error:
            # A display failing must not stop the announcement.
            print(f"[JARVIS] sensor display failed: {error}", flush=True)

    if event == "online":
        if first_time or returning:
            return f"{_spoken(name)} sensor online, sir."

        return None

    if event == "motion":
        return "Welcome back, sir." if fresh else None

    return None


# ---- asked aloud: "what's the temperature in the room" ---------------------

# What a question can be about, and the words that ask for it. Rooms are
# not listed anywhere: they are whatever the boards have called
# themselves, so a new board is askable the moment it reports.
_ASKING = {
    "temperature": ("temperature", "temp", "warm", "warmer", "hot", "cold", "colder",
                    "chilly", "degrees", "heat"),
    "humidity": ("humidity", "humid", "damp", "muggy", "moisture"),
    "presence": ("anyone", "anybody", "someone", "somebody", "occupied", "empty",
                 "movement", "motion", "moving"),
    "everything": ("sensor", "sensors", "readings", "reading"),
}

# How a question to the sensors starts, so a command that merely mentions a
# sensor ("remind me to check the sensor wiring") is left alone.
_QUESTION_STARTS = ("what", "whats", "how", "hows", "is", "are", "any", "anyone", "anybody",
                    "tell", "read", "give", "check", "who")

# Words that mean "the rooms the sensors are in" without naming one.
_INDOORS = ("in here", "inside", "indoors", "in the house", "at home", "the house")

_WORD = re.compile(r"[a-z0-9]+")


def _words(text):
    return _WORD.findall((text or "").casefold().replace("'", ""))


def _has_phrase(words, phrase):
    wanted = phrase.split()
    return any(words[i:i + len(wanted)] == wanted for i in range(len(words) - len(wanted) + 1))


def question(text):
    """What a spoken question asks of the sensors, or None if it is not one.

    Returns (what, names): what is "temperature", "humidity", "presence" or
    "everything"; names are the boards meant, every board when none was
    named. Needs something to ask about and somewhere to ask it -- a board
    by name, or "in here" -- so "what's the temperature outside" and "is
    anyone there" go on to the usual routing. Mentioning the sensors
    themselves ("how are the sensors") is enough on its own.
    """
    words = _words(text)

    # Spoken politeness in front of the question.
    while words and words[0] in ("jarvis", "hey", "ok", "okay", "so", "and", "please"):
        words = words[1:]

    if not words or words[0] not in _QUESTION_STARTS:
        return None

    with _lock:
        names = list(_sensors)

    asked = [what for what, cues in _ASKING.items() if any(word in cues for word in words)]

    if not asked:
        return None

    named = [name for name in names if _has_phrase(words, " ".join(_words(name)))]
    indoors = any(_has_phrase(words, phrase) for phrase in _INDOORS)
    about_sensors = "everything" in asked

    if not (named or indoors or about_sensors):
        return None

    # The most particular thing asked wins: "is anyone in the room" is
    # about presence even though "room" could be a board's reading too.
    for what in ("presence", "humidity", "temperature", "everything"):
        if what in asked:
            return what, named or names

    return None


def _ago(seconds):
    minutes = int(seconds // 60)

    if minutes < 1:
        return "just now"

    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def _place(name):
    """'the kitchen' -- how a room is named mid-sentence."""
    return f"the {name}" if not name.startswith(("the ", "my ")) else name


def _presence_words(name, board):
    moved = board["moved_ago"]

    if moved is None:
        return "no movement seen yet"

    if present(name):
        return f"someone there, movement {_ago(moved)}"

    return f"empty, no movement for {_ago(moved).replace(' ago', '')}"


def answer(asked):
    """A spoken answer to question()'s (what, names), from what the boards last said.

    One sentence per room, so asking after several reads as a list rather
    than one breathless run-on.
    """
    if not asked:
        return None

    what, names = asked
    boards = known()

    if not boards:
        return "No sensors have reported yet, sir."

    sentences = []

    for name in sorted(names):
        board = boards.get(name)

        if board is None:
            continue

        readings = board["readings"]
        place = _place(name)
        parts = []

        if what in ("temperature", "everything") and "temperature" in readings:
            parts.append(f"{readings['temperature']:.1f} degrees")

        if what in ("humidity", "everything") and "humidity" in readings:
            parts.append(f"{readings['humidity']:.0f} percent humidity")

        if what == "humidity" and parts:
            # "Humidity in the kitchen is 46 percent", not "the kitchen is 46 percent humidity".
            parts = []
            humidity_sentence = f"humidity in {place} is {readings['humidity']:.0f} percent"
        else:
            humidity_sentence = None

        if what == "presence" or (what == "everything" and not parts):
            words = _presence_words(name, board)

            if words.startswith("someone"):
                sentence = f"someone is in {place}, movement {_ago(board['moved_ago'])}"
            elif words.startswith("empty"):
                sentence = f"{place} looks {words}"
            else:
                sentence = f"no movement seen in {place} yet"
        elif humidity_sentence:
            sentence = humidity_sentence
        elif parts:
            sentence = f"{place} is " + " with ".join(parts)

            if what == "everything":
                sentence += f"; {_presence_words(name, board)}"
        else:
            continue

        if board["seen_ago"] > ABSENT_SECONDS:
            sentence += f", though its sensor last reported {_ago(board['seen_ago'])}"

        sentences.append(sentence[0].upper() + sentence[1:] + ".")

    if not sentences:
        spoken = " or ".join(_place(name) for name in sorted(names)) or "any room"
        kind = {"temperature": "a temperature", "humidity": "a humidity reading"}.get(what, "a reading")
        return f"I don't have {kind} from {spoken} yet, sir."

    # "sir" once, at the end of the first sentence, as he would say it.
    sentences[0] = sentences[0][:-1] + ", sir."
    return " ".join(sentences)
