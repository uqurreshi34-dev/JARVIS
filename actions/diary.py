"""A calendar JARVIS keeps for you, plus a file for your calendar app.

Events live in a plain text file in the JARVIS folder, so nothing depends on
which calendar application is installed and nothing can be lost to a
sign-in. Each new event also writes an `.ics` file, which Outlook, Google
Calendar and Apple Calendar all understand: double-click it and the event is
added properly.

Named `diary` rather than `calendar` so it cannot be confused with Python's
own calendar module.
"""

import os
import re
import threading
from datetime import date, datetime, timedelta

import phrases
from actions import files, safety


FILENAME = "calendar.txt"

# Where the .ics files are written, inside the JARVIS folder.
INVITE_FOLDER = "invites"

# Read aloud at most, since more is unusable by voice.
SPOKEN_LIMIT = 4

_lock = threading.Lock()

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "twenty first": 21,
    "twenty second": 22, "twenty third": 23, "twenty fourth": 24,
    "twenty fifth": 25, "twenty sixth": 26, "twenty seventh": 27,
    "twenty eighth": 28, "twenty ninth": 29, "thirtieth": 30,
    "thirty first": 31,
}

# "2nd", "3rd", "21st" and so on.
_ORDINAL_SUFFIX = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)

_TIME = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|o'?clock)?\b",
    re.IGNORECASE,
)


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
                line.rstrip("\n")
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]

    except OSError as error:
        print(f"[JARVIS] could not read the calendar: {error}")
        return []


def _write(lines):
    path = _path()

    if not path:
        return False

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "# Your calendar. One event per line: date, tab, title.\n"
            )

            for line in sorted(lines):
                handle.write(f"{line}\n")

        return True

    except OSError as error:
        print(f"[JARVIS] could not write the calendar: {error}")
        return False


def parse_date(text, today=None):
    """Turn spoken words into a date, or None.

    Understands "March the second twenty twenty seven", "2nd of March",
    "tomorrow", "next Friday" and "the 15th".
    """
    if not text:
        return None

    today = today or date.today()
    spoken = " ".join(str(text).casefold().split())
    spoken = spoken.replace("the ", " ").replace(" of ", " ")
    spoken = _ORDINAL_SUFFIX.sub(r"\1", spoken)

    # Longest first: otherwise "twenty fifth" matches "fifth" and becomes
    # "twenty 5", which then reads as the fifth.
    for word in sorted(_ORDINALS, key=len, reverse=True):
        spoken = re.sub(rf"\b{word}\b", str(_ORDINALS[word]), spoken)

    spoken = " ".join(spoken.split())

    if "today" in spoken:
        return today

    if "tomorrow" in spoken:
        return today + timedelta(days=1)

    # "next friday", "on monday"
    for name, index in _WEEKDAYS.items():
        if name in spoken:
            ahead = (index - today.weekday()) % 7

            # "next" always means the following week, never today.
            if ahead == 0 or "next" in spoken:
                ahead = ahead or 7

            return today + timedelta(days=ahead)

    day = None
    month = None
    year = None

    for name, number in _MONTHS.items():
        if re.search(rf"\b{name}\b", spoken):
            month = number
            break

    numbers = [int(n) for n in re.findall(r"\b\d{1,4}\b", spoken)]

    for number in numbers:
        if number > 1900:
            year = number
        elif month is None and 1 <= number <= 12 and day is not None:
            month = number
        elif day is None and 1 <= number <= 31:
            day = number

    # "twenty twenty seven" arrives as "20 20 7" after the ordinals pass.
    if year is None:
        pair = re.search(r"\b20\s+(\d{1,2})\b", spoken)

        if pair:
            year = 2000 + int(pair.group(1))

    if month is None or day is None:
        return None

    if year is None:
        year = today.year

        # A date already gone means next year.
        try:
            if date(year, month, day) < today:
                year += 1
        except ValueError:
            return None

    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time(text):
    """A time of day from spoken words, as (hour, minute), or None."""
    if not text:
        return None

    spoken = str(text).casefold()

    if "midday" in spoken or "noon" in spoken:
        return 12, 0

    if "midnight" in spoken:
        return 0, 0

    match = _TIME.search(spoken)

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    marker = (match.group(3) or "").replace("'", "")

    if hour > 23 or minute > 59:
        return None

    if marker == "pm" and hour < 12:
        hour += 12
    elif marker == "am" and hour == 12:
        hour = 0

    return hour, minute


def _split(line):
    """A stored line as (when, title, event id). The id may be empty."""
    parts = line.split("\t")

    when = parts[0].strip() if parts else ""
    title = parts[1].strip() if len(parts) > 1 else ""
    event_id = parts[2].strip() if len(parts) > 2 else ""

    return when, title, event_id


def events():
    """Every event as (datetime or date, title), soonest first."""
    entries = []

    for line in _read():
        when, title, _ = _split(line)

        if not title:
            continue

        moment = _to_moment(when)

        if moment:
            entries.append((moment, title))

    return sorted(entries, key=lambda pair: _sort_key(pair[0]))


def entries():
    """Every stored line as (moment, title, event id)."""
    found = []

    for line in _read():
        when, title, event_id = _split(line)

        if not title:
            continue

        moment = _to_moment(when)

        if moment:
            found.append((moment, title, event_id))

    return sorted(found, key=lambda item: _sort_key(item[0]))


def _to_moment(text):
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)

            return parsed if " " in text else parsed.date()
        except ValueError:
            continue

    return None


def _sort_key(moment):
    if isinstance(moment, datetime):
        return moment

    return datetime.combine(moment, datetime.min.time())


def _stored(moment):
    if isinstance(moment, datetime):
        return moment.strftime("%Y-%m-%d %H:%M")

    return moment.strftime("%Y-%m-%d")


def add_to_outlook(title, moment):
    """Add the event to the Microsoft calendar.

    Returns the event's identifier when Outlook gives one back, True when
    it succeeded without one, or False on failure. The identifier is what
    makes deletion exact rather than a search by title.
    """
    try:
        from outlook import create_event
    except ImportError as error:
        print(f"[JARVIS] Outlook integration unavailable: {error}")
        return False

    try:
        outcome = create_event(title, moment)

    except Exception as error:
        print(f"[JARVIS] could not add to Outlook: {error}")
        return False

    # create_event may return an id, a dictionary from Graph, or a bool.
    if isinstance(outcome, dict):
        return outcome.get("id") or True

    return outcome


def remove_from_outlook(event_id):
    """Delete one event from the Microsoft calendar by its identifier."""
    if not event_id:
        return False

    try:
        from outlook import delete_event
    except ImportError:
        # Deleting is newer than creating, so an older outlook.py may not
        # have it. The local removal still stands.
        print("[JARVIS] outlook.py has no delete_event; removed locally only")
        return False

    try:
        return bool(delete_event(event_id))

    except Exception as error:
        print(f"[JARVIS] could not remove from Outlook: {error}")
        return False


def add(title, when, at=None):
    """Add an event. Returns the .ics path, or None."""
    title = safety.clean(title, 120)

    if not title or when is None:
        return None

    moment = when

    if at is not None and not isinstance(when, datetime):
        hour, minute = at
        moment = datetime.combine(when, datetime.min.time()).replace(
            hour=hour, minute=minute
        )

    # The .ics is always written, whether or not Outlook took it: it is the
    # record, and the way in for any other calendar.
    invite = write_invite(title, moment)

    event_id = ""

    if _wants_outlook():
        outcome = add_to_outlook(title, moment)

        if isinstance(outcome, str):
            event_id = outcome

    with _lock:
        existing = _read()
        line = f"{_stored(moment)}\t{title}"

        if event_id:
            line += f"\t{event_id}"

        # An entry for the same moment and title is replaced, so the id is
        # not lost if the event is added twice.
        kept = [
            other for other in existing
            if _split(other)[:2] != (_stored(moment), title)
        ]

        kept.append(line)

        if not _write(kept):
            return None

    return invite


def _wants_outlook():
    """Whether to try putting appointments into Outlook directly.

    Controlled by memory, so "remember my calendar is local" turns it off
    without touching any code.
    """
    try:
        from actions import memory

        preference = (memory.get("calendar") or "").strip().casefold()

    except Exception:
        return True

    if preference in ("local", "jarvis", "none", "off", "file", "ics"):
        return False

    return True


def remove(title, when=None):
    """Remove matching events everywhere. Returns how many went.

    Removal is symmetrical with adding: the Outlook appointment, the local
    line and the .ics all go. Outlook is told by identifier, so the right
    appointment is deleted rather than the first one with a similar name.
    """
    wanted = safety.clean(title).casefold()

    if not wanted:
        return 0

    stamp = _stored(when) if when is not None else None

    with _lock:
        existing = _read()
        kept = []
        going = []

        for line in existing:
            stored_when, stored_title, event_id = _split(line)

            matches_title = wanted in stored_title.casefold()
            matches_date = stamp is None or stored_when.startswith(stamp[:10])

            if matches_title and matches_date:
                going.append((stored_when, stored_title, event_id))
            else:
                kept.append(line)

        if going:
            _write(kept)

    # Outside the lock: these reach the network and the file system, and
    # holding the lock across them would block everything else.
    for stored_when, stored_title, event_id in going:
        if event_id:
            remove_from_outlook(event_id)

        _delete_invite(stored_title, _to_moment(stored_when))

    return len(going)


def clear():
    """Remove every event everywhere. Returns how many there were."""
    with _lock:
        existing = _read()

        if existing:
            _write([])

    for line in existing:
        stored_when, stored_title, event_id = _split(line)

        if event_id:
            remove_from_outlook(event_id)

        _delete_invite(stored_title, _to_moment(stored_when))

    return len(existing)


def _delete_invite(title, moment):
    """Remove the .ics written for an event, if it is still there."""
    if not moment:
        return False

    folder = _invite_folder()

    if not folder:
        return False

    if isinstance(moment, datetime):
        start = moment.strftime("%Y%m%dT%H%M%S")
    else:
        start = moment.strftime("%Y%m%d")

    safe = re.sub(r"[^\w \-]", "", title).strip() or "event"
    path = os.path.join(folder, f"{safe} {start}.ics")

    if not os.path.exists(path):
        return False

    try:
        os.remove(path)

        print(f"[JARVIS] removed {os.path.basename(path)}")

        return True

    except OSError as error:
        print(f"[JARVIS] could not remove the invite: {error}")
        return False


def upcoming(limit=None):
    """Events still to come, soonest first."""
    now = datetime.now()

    ahead = [
        (moment, title) for moment, title in events()
        if _sort_key(moment) >= now.replace(hour=0, minute=0, second=0,
                                            microsecond=0)
    ]

    return ahead[:limit] if limit else ahead


def spoken_when(moment):
    """A date said the way a person would say it."""
    if isinstance(moment, datetime):
        day = moment.date()
    else:
        day = moment

    today = date.today()

    if day == today:
        prefix = "today"
    elif day == today + timedelta(days=1):
        prefix = "tomorrow"
    else:
        prefix = f"{day.strftime('%A')} the {_ordinal(day.day)} of " \
            f"{day.strftime('%B')}"

        if day.year != today.year:
            prefix += f" {day.year}"

    if isinstance(moment, datetime):
        return f"{prefix} at {_spoken_clock(moment)}"

    return prefix


# Kept out of the f-string below: a dictionary literal inside one parses
# only on newer Pythons and defeats every linter.
_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(number):
    """1 -> "1st", 2 -> "2nd", 11 -> "11th"."""
    if 11 <= number % 100 <= 13:
        return f"{number}th"

    suffix = _ORDINAL_SUFFIXES.get(number % 10, "th")

    return f"{number}{suffix}"


def _spoken_clock(moment):
    """The clock time as the voice should say it: "four oh five PM".

    The one speech-safe clock in system.py. This used to be a copy that
    wrote "4 05 PM", which the voice read as "four five".
    """
    from actions import system

    return system._spoken_clock(moment)


def describe(limit=SPOKEN_LIMIT):
    """A spoken summary of what is coming up."""
    ahead = upcoming()

    if not ahead:
        total = len(events())

        if total:
            return "Nothing coming up, sir. Everything in the calendar has passed."

        return "Your calendar is empty, sir."

    shown = ahead[:limit]

    parts = [
        f"{title}, {spoken_when(moment)}" for moment, title in shown
    ]

    listed = ". ".join(parts)

    if len(ahead) <= limit:
        word = "event" if len(ahead) == 1 else "events"

        return (
            f"You have {phrases.number(len(ahead))} {word}, sir. {listed}."
        )

    return (
        f"You have {phrases.number(len(ahead))} events coming up, sir. "
        f"The next {phrases.number(len(shown))} are: {listed}."
    )


def _opening(word):
    """Capitalise a word that begins a spoken sentence."""
    return word[:1].upper() + word[1:] if word else word


def briefing(days=2):
    """What is coming up in the next day or two, or None.

    Said when JARVIS starts, so the calendar is something he brings to you
    rather than something you have to remember to ask about.
    """
    horizon = date.today() + timedelta(days=days - 1)

    soon = []

    for moment, title in upcoming():
        day = moment.date() if isinstance(moment, datetime) else moment

        if day <= horizon:
            soon.append((moment, title))

    if not soon:
        return None

    parts = [f"{title}, {spoken_when(moment)}" for moment, title in soon[:3]]
    listed = ". ".join(parts)

    if len(soon) == 1:
        return f"One thing in the diary, sir. {listed}."

    if len(soon) <= 3:
        return (
            f"{_opening(phrases.number(len(soon)))} things in the diary, "
            f"sir. {listed}."
        )

    return (
        f"{_opening(phrases.number(len(soon)))} things in the diary, sir. "
        f"The first three: {listed}."
    )


def _invite_folder():
    base = files.root()

    if not base:
        return None

    folder = os.path.join(base, INVITE_FOLDER)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not create {folder}: {error}")
        return None

    return folder


def write_invite(title, moment):
    """Write an .ics file for one event. Returns the path, or None."""
    folder = _invite_folder()

    if not folder:
        return None

    if isinstance(moment, datetime):
        start = moment.strftime("%Y%m%dT%H%M%S")
        finish = (moment + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
        times = f"DTSTART:{start}\nDTEND:{finish}"
    else:
        start = moment.strftime("%Y%m%d")
        finish = (moment + timedelta(days=1)).strftime("%Y%m%d")
        times = f"DTSTART;VALUE=DATE:{start}\nDTEND;VALUE=DATE:{finish}"

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^\w \-]", "", title).strip() or "event"

    path = os.path.join(folder, f"{safe} {start}.ics")

    body = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//JARVIS//EN\n"
        "CALSCALE:GREGORIAN\n"
        "METHOD:PUBLISH\n"
        "BEGIN:VEVENT\n"
        f"UID:{stamp}-{abs(hash(title)) % 100000}@jarvis\n"
        f"DTSTAMP:{stamp}\n"
        f"{times}\n"
        f"SUMMARY:{title}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )

    try:
        with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(body)

    except OSError as error:
        print(f"[JARVIS] could not write the invite: {error}")
        return None

    print(f"[JARVIS] invite written to {path}")

    return path
