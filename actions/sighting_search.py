"""Sightings asked about in plain words: when, and whether it was you, from the records kept.

The house cam keeps a folder for each sighting (actions/sightings.py), and its sighting.json says when
it began, how long it lasted and -- once JARVIS knows your face (actions/known_faces.py) -- whether it
was you. These questions are answered from those records alone, on this PC, with no model and no call:

    when was I last on the house cam              "You were last on your house cam at 19:55, ..."
    when did a stranger last come                 the newest not recognised as you
    show me sightings from last night             how many, when, who; the newest one's picture shown
    any strangers this week                       those not recognised as you
    how many times was I seen today               a count
    was anyone on the house cam yesterday

Times: today, yesterday, last night, this morning, this afternoon, this evening or tonight, this week,
last week, on Monday (the last one), in the last hour, in the last 3 hours or 20 minutes. None: all
the sightings kept.

"Not recognised as you" is what it says, never "a stranger was here": a sighting is you only when your
face was seen well enough (actions/known_faces.py), so one with your back to the camera is "someone".
One made before JARVIS knew your face is neither, until checked (sightings.check_unchecked, at start
and after "learn my face"); asked about you or others, how many are still to check is said.
Present-tense questions ("is anyone on the house cam") are left to what the camera sees now.
"""

import os
import re
from datetime import datetime, timedelta

from actions import known_faces, sightings

LISTED_AT_MOST = 5

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "twenty": 20, "thirty": 30, "forty": 40,
                 "forty five": 45, "fifteen": 15}

# What makes it about sightings at all: the camera, or the words for them.
_ABOUT = re.compile(r"\b(?:sightings?|strangers?|intruders?|visitors?|house\s+cam|cams?|cameras?)\b"
                    r"|\b(?:see|seen|saw|spotted)\s+me\b|\b(?:was|were)\s+i\s+(?:seen|spotted|on)\b"
                    r"|\bi\s+was\s+(?:seen|spotted|on)\b")
# The record of them, not what is in view now.
_HISTORY = re.compile(r"\b(?:sightings?|strangers?|intruders?|visitors?)\b")
_PAST = re.compile(r"\b(?:was|were|been|did|last|seen|saw|came|come|spotted|visited)\b")
_ASKING = re.compile(r"^(?:(?:jarvis|please|now|so|and|ok|okay|hey)\s+)*(?:show(?:\s+me)?|list|tell\s+me|when|was|were|"
                     r"has|have|had|did|any|how\s+many|how\s+often|who|what|is\s+there|are\s+there|were\s+there)\b")

_ME = re.compile(r"\b(?:i|me|my\s+face)\b")
_OTHERS = re.compile(r"\b(?:strangers?|intruders?|visitors?|anyone\s+else|anybody\s+else|someone\s+else|"
                     r"somebody\s+else|not\s+me|other\s+than\s+me|apart\s+from\s+me|but\s+me|someone\s+i\s+don'?t\s+know|"
                     r"people\s+i\s+don'?t\s+know|unknown)\b")
_LAST = re.compile(r"\bwhen\b.*\blast\b|\blast\s+time\b|\bmost\s+recent(?:ly)?\b|\bwhen\s+did\s+you\s+last\b"
                   r"|\bthe\s+last\s+(?:stranger|intruder|visitor)\b")
# Asking, not who: "show me", "tell me", "let me see".
_ASKER = re.compile(r"\b(?:show|tell|let|give)\s+me\b")
_COUNT = re.compile(r"\bhow\s+(?:many|often)\b")


def _words(text):
    return " ".join(re.sub(r"[^\w\s']", " ", str(text or "").casefold()).split())


def _number(text):
    text = text.strip()
    return int(text) if text.isdigit() else _NUMBER_WORDS.get(text)


def when(words, now=None):
    """(start, end, spoken) of the time [words] names, as aware datetimes, or (None, None, "") for all."""
    now = now or datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    match = re.search(r"\b(?:in\s+)?the\s+(?:last|past)\s+(?:(\w+(?:\s+\w+)?)\s+)?(hours?|minutes?|mins?)\b", words)

    if match:
        count = _number(match.group(1)) if match.group(1) else 1

        if count:
            unit = timedelta(hours=1) if match.group(2).startswith("hour") else timedelta(minutes=1)
            name = "hour" if unit == timedelta(hours=1) else "minute"
            spoken = f"in the last {name}" if count == 1 else f"in the last {count} {name}s"
            return now - count * unit, now, spoken

    if re.search(r"\blast\s+night\b", words):
        evening = today - timedelta(days=1) + timedelta(hours=18)
        return evening, min(now, today + timedelta(hours=6)), "last night"

    if re.search(r"\b(?:tonight|this\s+evening)\b", words):
        return today + timedelta(hours=18), now, "this evening"

    if re.search(r"\bthis\s+morning\b", words):
        return today + timedelta(hours=5), min(now, today + timedelta(hours=12)), "this morning"

    if re.search(r"\bthis\s+afternoon\b", words):
        return today + timedelta(hours=12), min(now, today + timedelta(hours=18)), "this afternoon"

    if re.search(r"\byesterday\b", words):
        return today - timedelta(days=1), today, "yesterday"

    if re.search(r"\btoday\b", words):
        return today, now, "today"

    if re.search(r"\blast\s+week\b", words):
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday, "last week"

    if re.search(r"\bthis\s+week\b", words):
        return today - timedelta(days=today.weekday()), now, "this week"

    match = re.search(r"\b(?:on\s+)?(?:last\s+)?(" + "|".join(_DAYS) + r")\b", words)

    if match:
        back = (today.weekday() - _DAYS.index(match.group(1))) % 7 or 7
        day = today - timedelta(days=back)
        return day, day + timedelta(days=1), f"on {match.group(1).capitalize()}"

    return None, None, ""


def asked(text):
    """{"who": "you"|"others"|"any", "mode": "last"|"count"|"list", "start", "end", "spoken"} for a
    question about kept sightings, else None."""
    words = _words(text)

    if not words or not _ABOUT.search(words) or not _ASKING.match(words):
        return None

    start, end, spoken = when(words)

    if start is None and not _HISTORY.search(words) and not _PAST.search(words):
        return None     # "is anyone on the house cam": what it sees now, not what it kept

    who = "others" if _OTHERS.search(words) else ("you" if _ME.search(_ASKER.sub(" ", words)) else "any")
    mode = "count" if _COUNT.search(words) else ("last" if _LAST.search(words) else "list")
    return {"who": who, "mode": mode, "start": start, "end": end, "spoken": spoken}


def _began(record):
    try:
        return datetime.fromisoformat(record["began"])
    except (KeyError, TypeError, ValueError):
        return None


def _is_you(record):
    return (record.get("recognised") or {}).get("name") == known_faces.YOU


def _when_spoken(began, now):
    day = began.date()
    today = now.date()
    said = began.strftime("%H:%M")

    if day == today:
        return said

    if day == today - timedelta(days=1):
        return f"{said} yesterday"

    return f"{said} on {began.strftime('%A')} {began.day} {began.strftime('%B')}"


def _checked(record):
    """Whether a sighting was checked for your face: at its end, or since (sightings.check_unchecked)."""
    return "recognised" in record


def find(query):
    """([(folder, record, began)] of the sightings [query] (asked()) asks about, newest first; how many
    in its time were never checked for your face, so are neither you nor anyone else)."""
    found = []
    unchecked = 0

    for path, record in sightings._records():
        began = _began(record)

        if began is None:
            continue

        if query["start"] is not None and not (query["start"] <= began < query["end"]):
            continue

        if query["who"] != "any" and not _checked(record):
            unchecked += 1
            continue

        if query["who"] == "you" and not _is_you(record):
            continue

        if query["who"] == "others" and _is_you(record):
            continue

        found.append((path, record, began))

    return found, unchecked


def answer(text, now=None):
    """(what to say, the picture to show or None, its title) for a question [text] about sightings."""
    query = asked(text)

    if query is None:
        return None, None, None

    now = now or datetime.now().astimezone()
    knows_you = known_faces.anyone_known()

    if query["who"] != "any" and not knows_you:
        return ("I don't know your face yet, sir, so I can't tell you from anyone else. Stand in front of "
                "the house cam and say 'learn my face'.", None, None)

    found, unchecked = find(query)

    # Asked when you were last seen: only those since then, unchecked, could be later.
    if query["mode"] == "last" and found:
        unchecked = len([1 for _, record, began in find(dict(query, who="any"))[0]
                         if began > found[0][2] and not _checked(record)])

    when_said = f" {query['spoken']}" if query["spoken"] else ""
    not_yet = (f" {unchecked} {'sighting hasn' if unchecked == 1 else 'sightings haven'}'t been checked for your "
               f"face yet; I check them each time I start." if unchecked else "")
    # Within one day asked about, the day need not be said again with each time.
    one_day = query["start"] is not None and query["end"] - query["start"] <= timedelta(days=1)
    who_said = {"you": "you", "others": "anyone not recognised as you", "any": "anyone"}[query["who"]]

    if not found:
        none = {"you": "I haven't seen you", "others": "I've seen no one but you",
                "any": "I've seen no one"}[query["who"]]
        return f"{none}{when_said}, sir.{not_yet}", None, None

    path, record, began = found[0]
    picture = os.path.join(path, "best.jpg")
    picture = picture if os.path.isfile(picture) else None
    title = f"SIGHTING {record.get('camera', 'camera').upper()}"

    if query["mode"] == "count":
        times = "once" if len(found) == 1 else ("twice" if len(found) == 2 else f"{len(found)} times")
        subject = {"you": "You were seen", "others": "Someone not recognised as you was seen",
                   "any": "Someone was seen"}[query["who"]]
        return f"{subject} {times}{when_said}, sir.{not_yet}", picture, title

    if query["mode"] == "last":
        seconds = max(1, round(record.get("seconds") or 0))
        subject = "You were" if _is_you(record) else ("Someone not recognised as you was" if knows_you else "Someone was")
        return (f"{subject} last on your {record.get('camera', 'camera')} at {_when_spoken(began, now)}, for "
                f"{seconds} seconds, sir.{not_yet}", picture, title)

    listed = []

    for _, each, each_began in found[:LISTED_AT_MOST]:
        label = "you" if _is_you(each) else ("someone" if _checked(each) else "someone (not checked yet)")
        at = each_began.strftime("%H:%M") if one_day else _when_spoken(each_began, now)
        listed.append(f"{at}, {label}" if knows_you else at)

    more = len(found) - LISTED_AT_MOST
    and_more = f", and {more} more" if more > 0 else ""
    count = "One sighting" if len(found) == 1 else f"{len(found)} sightings"
    of_whom = "" if query["who"] == "any" else f" of {who_said}"
    return f"{count}{of_whom}{when_said}, sir: {'; '.join(listed)}{and_more}.{not_yet}", picture, title


def picture_of(name):
    """The best picture of the sighting in folder [name] (a folder's own name, nothing more), or None."""
    base = sightings.folder(make=False)

    if not base or not re.fullmatch(r"[a-z0-9-]+", str(name or "")):
        return None

    path = os.path.join(base, name, "best.jpg")
    return path if os.path.isfile(path) else None
