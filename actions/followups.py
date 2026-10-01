"""Follow-ups to a sensor question: "and humidity?", "what about yesterday?".

After "how warm did my room get overnight", the next question is often a
fragment: "and humidity?", "what about yesterday?", "and the lowest?",
"how about room 4?", "and now?", "show me that again". A fragment like
that names only what changed. This keeps what the last sensor question
was about -- the reading, how it was asked, the stretch of time, and the
room -- for FOLLOW_SECONDS, and turns a fragment into the whole question,
which then goes the ordinary way: the same answer, chart and lit room as
asking it in full, and never a model call.

A fragment is only taken when every word in it is accounted for: a
reading, a stretch of time, a room, "highest" or "lowest", or the small
words around them. "And the weather?" or "what about tomorrow?" are not
follow-ups to the room and are left alone. Any other command in between
ends the context, so "what about yesterday" after a calendar question is
never taken for the room.
"""

import re
import time

from actions import house, sensor_history, sensors


# The same window as "it" and "that" in commands.py: long enough to think,
# short enough that a fragment an hour later is not taken for this.
FOLLOW_SECONDS = 60.0

# The intents a follow-up carries on from. Any other command ends it.
INTENTS = frozenset({"sensor_history", "house_question", "sensor_question"})

_context = {"at": 0.0, "slots": None, "said": None}

_WORD = re.compile(r"[a-z0-9]+")

_LEADS = ("and what about", "and how about", "what about", "how about", "and the", "and", "then", "also")
_NOW = ("now", "right now", "currently", "at the moment")
_AGAIN = ("again",)

# The small words a fragment may carry besides what it names.
_FILLER = {
    "the", "a", "an", "in", "it", "its", "for", "of", "on", "over", "during", "was", "is", "did", "get", "got",
    "please", "jarvis", "sir", "then", "instead", "there", "that", "this", "so", "ok", "okay", "what", "whats",
    "how", "hows", "about", "and", "show", "me", "chart", "graph", "bring", "up", "once", "more", "one",
    "my", "it", "same", "too",
}
_TIME_WORDS = {
    "overnight", "night", "last", "tonight", "today", "yesterday", "morning", "afternoon", "evening", "since",
    "week", "past", "previous", "hour", "hours", "minute", "minutes", "min", "mins", "day", "days", "half",
    "through", "far", "couple", "few", "now", "right", "currently", "moment", "at",
} | {word for words in sensor_history._NUMBER_WORDS for word in words.split()}
_HIGH = set(sensor_history._HIGH) | {"high", "higher", "warmest", "hottest", "peak"}
_LOW = set(sensor_history._LOW) | {"low", "lower"}
_SUMMARY = {"average", "range", "overall", "generally"}


def _words(text):
    return _WORD.findall(str(text or "").casefold().replace("'", ""))


def forget():
    _context.update(at=0.0, slots=None, said=None)


def handled(intent):
    """commands.py, after every command it understood: anything but a sensor question ends the context."""
    if intent not in INTENTS:
        forget()


def _place(text):
    """How the room in [text] is said in a question: "in my room", "in room 4", "in here", or None."""
    room = house.named_room(text)

    if room is not None:
        return f"in {room[0]}"

    words = _words(text)
    names = sorted(set(sensors.known()) | set(sensor_history._boards_with_history()), key=len, reverse=True)

    for name in names:
        if sensors._has_phrase(words, " ".join(_words(name))):
            return f"in {name}"

    if any(sensors._has_phrase(words, phrase) for phrase in sensors._INDOORS):
        return "in here"

    return None


def remember(text):
    """A sensor question was answered: keep what it was about, so a fragment can follow it."""
    asked = sensor_history.question(text)

    if asked is not None:
        slots = {"what": asked["what"], "aspect": asked["aspect"],
                 "window": asked["window"]["spoken"] if asked["window"]["start"] is not None else None}
    else:
        now = house.question(text) or sensors.question(text)

        if now is None or now[0] not in ("temperature", "humidity", "presence"):
            return

        slots = {"what": now[0], "aspect": "summary", "window": None}

    place = _place(text)

    if place is None:
        return

    slots["place"] = place
    _context.update(at=time.monotonic(), slots=slots, said=text)


def _sentence(slots):
    """The whole question for [slots], worded the way the router already understands."""
    what, place, window = slots["what"], slots["place"], slots["window"]

    if what == "presence":
        return f"was anyone {place} {window}" if window else f"is anyone {place}"

    if not window:
        return f"what's the {what} {place}"

    lead = {"high": "highest ", "low": "lowest "}.get(slots["aspect"], "")
    return f"what was the {lead}{what} {place} {window}"


def _strip_lead(words):
    said = " ".join(words)

    for lead in sorted(_LEADS, key=len, reverse=True):
        if said == lead:
            return []

        if said.startswith(lead + " "):
            return said[len(lead) + 1:].split()

    return words


def rewrite(text):
    """The whole question a fragment stands for, or None when [text] is not a follow-up."""
    slots = _context["slots"]

    if not slots or time.monotonic() - _context["at"] > FOLLOW_SECONDS:
        return None

    words = _strip_lead(_words(text))

    while words and words[0] in ("jarvis", "ok", "okay", "so"):
        words = words[1:]

    if not words or len(words) > 8:
        return None

    # A question complete in itself is asked as it is.
    if sensor_history.question(text) or house.question(text) or sensors.question(text):
        return None

    said = " ".join(words)

    if "again" in words and set(words) <= _FILLER | set(_AGAIN):
        return _context["said"]

    changed = dict(slots)
    known = set(_FILLER)

    for what in ("presence", "humidity", "temperature"):
        cues = set(sensors._ASKING[what]) | set(sensor_history._SUPERLATIVES.get(what, ()))

        if cues & set(words):
            changed["what"] = what
            known |= cues
            break

    if _HIGH & set(words):
        changed["aspect"] = "high"
    elif _LOW & set(words):
        changed["aspect"] = "low"
    elif _SUMMARY & set(words):
        changed["aspect"] = "summary"

    known |= _HIGH | _LOW | _SUMMARY

    if any(f" {phrase} " in f" {said} " for phrase in _NOW):
        changed["window"] = None
        known |= _TIME_WORDS
    else:
        stretch = sensor_history.window(said)

        if stretch is not None:
            changed["window"] = stretch["spoken"] if stretch["start"] is not None else None
            known |= _TIME_WORDS | {word for word in words if word.isdigit()}

    place = _place(said)

    if place is not None:
        changed["place"] = place
        known |= set(_words(place))
        room = house.named_room(said)

        if room is not None:
            known |= {word for name in [room[0]] + _aliases(room[0]) for word in _words(name)}

        if place == "in here":
            known |= {word for phrase in sensors._INDOORS for word in phrase.split()}

    # Every word accounted for, and something changed: a follow-up.
    if not set(words) <= known or changed == slots:
        return None

    return _sentence(changed)


def _aliases(room_name):
    try:
        for room in house._rooms(house.places()[house.active_place()]):
            if room["name"] == room_name:
                return list(room["aliases"])
    except Exception:
        pass

    return []
