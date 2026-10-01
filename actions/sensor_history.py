"""What the boards have said over time: "how warm did my room get overnight".

sensors.py knows what each board said last; this keeps every reading, so
JARVIS can answer for a stretch of time, draw it, and notice when a room
has been too damp or too warm for too long. Nothing here touches the
network: sensors.report() hands each report over (set_recorder, wired in
by main.py), and the record stays on this PC, in sensor-history.sqlite in
the JARVIS folder, for keep_days and no longer.

Asked aloud, and never a model call:

    how warm did my room get overnight         the highest, and when
    how cold did it get in here last night     the lowest, and when
    what was the humidity in my room today     the range, the average, and now
    was anyone in my room overnight            movement: when, and how often
    show me the temperature in my room over the last 6 hours
    how cold did it get in my room             no stretch named, in the past tense: today

Each answer puts a chart of it on the HUD, from where the record begins
if that is partway through. And the first time you speak to him each
morning, after his answer, he tells you how the night went. A question needs a stretch of
time, something to ask about, and somewhere: a room on the house plan, a
board by name, or "in here", so "how cold did it get last night" alone is
still the weather's.

Everything that could be a number in the code is yours, in sensors.json in
the JARVIS folder (none of it is needed; these are the defaults):

    {
      "keep_days": 30,
      "overnight": {"from": "22:00", "to": "07:00"},
      "day_parts": {"morning": ["06:00", "12:00"], "afternoon": ["12:00", "18:00"],
                    "evening": ["18:00", "23:59"]},
      "quiet_hours": {"from": "23:00", "to": "07:00"},
      "nudge_again_hours": 6,
      "morning_report": {"until": "12:00"},      (false for none)
      "nudges": [
        {"reading": "humidity", "above": 70, "for_minutes": 120,
         "say": "Humidity in {place} has been over {limit} percent for {duration}, sir. ..."},
        {"reading": "temperature", "above": 27, "for_minutes": 30},
        {"reading": "temperature", "below": 16, "for_minutes": 30}
      ]
    }

A nudge is said once a reading has held past its limit for the whole of
for_minutes, with no gap in the record, and not again for that room for
nudge_again_hours; never in quiet hours. "say" may use {place}, {Place},
{limit}, {value} and {duration}.
"""

import io
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from actions import files, house, sensors


HISTORY_NAME = "sensor-history.sqlite"
CONFIG_NAME = "sensors.json"

FIELDS = ("temperature", "humidity")

DEFAULTS = {
    "keep_days": 30,
    "overnight": {"from": "22:00", "to": "07:00"},
    "day_parts": {"morning": ["06:00", "12:00"], "afternoon": ["12:00", "18:00"], "evening": ["18:00", "23:59"]},
    "quiet_hours": {"from": "23:00", "to": "07:00"},
    "nudge_again_hours": 6,
    "morning_report": {"until": "12:00"},
    "nudges": [
        {"reading": "humidity", "above": 70, "for_minutes": 120,
         "say": "Humidity in {place} has been over {limit} percent for {duration}, sir. "
                "It's {value} percent now; opening a window would help."},
        {"reading": "temperature", "above": 27, "for_minutes": 30,
         "say": "{Place} has been over {limit} degrees for {duration}, sir. It's {value} degrees now."},
        {"reading": "temperature", "below": 16, "for_minutes": 30,
         "say": "{Place} has been under {limit} degrees for {duration}, sir. It's {value} degrees now."},
    ],
}

# A board reports about every half minute. A longer silence than this is a
# hole in the record: a reading cannot be said to have held across it.
GAP_SECONDS = 20 * 60

# Separate movements closer together than this are one spell of it.
SPELL_SECONDS = 5 * 60

# How often old readings are cleared out.
PRUNE_EVERY_SECONDS = 3600

# The most points a chart draws; a week of readings is averaged down to it.
CHART_POINTS = 400

# The time, as the wall clock has it. A function, so tests can move it.
_clock = time.time

_lock = threading.Lock()
_pruned_at = None
_nudged = {}        # (board, nudge index) -> when it was said


# ---- sensors.json ---------------------------------------------------------------------------

def _path(name):
    root = files.root()
    return os.path.join(root, name) if root else None


def _parse_time(text):
    """"22:00" as (22, 0), or None for anything that is not a time of day."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))

    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return int(match.group(1)), int(match.group(2))

    return None


def _pair(raw):
    """{"from": a, "to": b} or [a, b] as [a, b]; anything else as [None, None]."""
    if isinstance(raw, dict):
        return [raw.get("from"), raw.get("to")]

    return list(raw) if isinstance(raw, (list, tuple)) and len(raw) == 2 else [None, None]


def _span(raw, default):
    """{"from": "22:00", "to": "07:00"} (or ["22:00", "07:00"]) as ((22, 0), (7, 0)); [default] fills a bad end."""
    given, fallback = _pair(raw), _pair(default)
    return tuple(_parse_time(value) or _parse_time(spare) or (0, 0) for value, spare in zip(given, fallback))


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _nudge_rules(raw):
    rules = []

    for index, rule in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(rule, dict) or rule.get("reading") not in FIELDS:
            print(f"[JARVIS] {CONFIG_NAME}: nudge {index + 1} needs a reading (temperature or humidity); left out")
            continue

        above, below, minutes = _number(rule.get("above")), _number(rule.get("below")), _number(rule.get("for_minutes"))

        if (above is None) == (below is None) or not minutes or minutes <= 0:
            print(f"[JARVIS] {CONFIG_NAME}: nudge {index + 1} needs one of above or below, and for_minutes; left out")
            continue

        rules.append({"reading": rule["reading"], "above": above, "below": below, "minutes": minutes,
                      "say": str(rule.get("say") or "").strip()})

    return rules


_settings = {"stamp": None, "value": None}


def settings():
    """sensors.json over the defaults, checked; read again only when the file changes."""
    path = _path(CONFIG_NAME)

    try:
        status = os.stat(path) if path else None
        stamp = (path, status.st_mtime_ns, status.st_size) if status else (path, None, None)
    except OSError:
        stamp = (path, None, None)

    if _settings["stamp"] != stamp or _settings["value"] is None:
        _settings["value"] = _read_settings(path)
        _settings["stamp"] = stamp

    return _settings["value"]


def _read_settings(path):
    data = {}

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[JARVIS] could not read {CONFIG_NAME}: {error}")

    data = data if isinstance(data, dict) else {}
    keep = _number(data.get("keep_days"))
    again = _number(data.get("nudge_again_hours"))
    parts = dict(DEFAULTS["day_parts"])

    if isinstance(data.get("day_parts"), dict):
        parts.update({str(key).casefold(): value for key, value in data["day_parts"].items()})

    return {
        "keep_days": keep if keep and keep > 0 else DEFAULTS["keep_days"],
        "overnight": _span(data.get("overnight"), DEFAULTS["overnight"]),
        "day_parts": {name: _span(span, DEFAULTS["day_parts"].get(name, ["00:00", "23:59"]))
                      for name, span in parts.items()},
        "quiet_hours": _span(data.get("quiet_hours"), DEFAULTS["quiet_hours"]),
        "nudge_again_hours": again if again is not None and again >= 0 else DEFAULTS["nudge_again_hours"],
        "nudges": _nudge_rules(data["nudges"] if "nudges" in data else DEFAULTS["nudges"]),
        # Until when in the morning the night's report may be given; None for never.
        "morning_until": None if data.get("morning_report") is False else
        _parse_time((data.get("morning_report") or {}).get("until") if isinstance(data.get("morning_report"), dict) else None)
        or _parse_time(DEFAULTS["morning_report"]["until"]),
    }


# ---- the record ------------------------------------------------------------------------------

def _connect():
    path = _path(HISTORY_NAME)

    if not path:
        return None

    connection = sqlite3.connect(path, timeout=5)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS readings ("
        " ts REAL NOT NULL, board TEXT NOT NULL,"
        " temperature REAL, humidity REAL, motion INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS readings_board_ts ON readings (board, ts)")
    return connection


def _rows(sql, parameters=()):
    with _lock:
        connection = _connect()

        if connection is None:
            return []

        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()


def _prune(connection, now, keep_days):
    global _pruned_at

    if _pruned_at is not None and now - _pruned_at < PRUNE_EVERY_SECONDS:
        return

    _pruned_at = now
    connection.execute("DELETE FROM readings WHERE ts < ?", (now - keep_days * 86400,))


def record(name, event, readings):
    """Keep one report (sensors.set_recorder). Returns a nudge to say, or None."""
    name = str(name or "").strip().casefold()
    readings = {field: float(readings[field]) for field in FIELDS if _number((readings or {}).get(field)) is not None}
    motion = event == "motion"

    if not name or not (readings or motion):
        return None

    now = _clock()
    rules = settings()

    with _lock:
        connection = _connect()

        if connection is None:
            return None

        try:
            with connection:
                connection.execute(
                    "INSERT INTO readings (ts, board, temperature, humidity, motion) VALUES (?, ?, ?, ?, ?)",
                    (now, name, readings.get("temperature"), readings.get("humidity"), int(motion)))
                _prune(connection, now, rules["keep_days"])
        finally:
            connection.close()

    return _nudge(name, readings, now, rules) if readings else None


def forget():
    """Clear the record and what has been nudged. For tests."""
    global _pruned_at

    _nudged.clear()
    _pruned_at = None
    _settings["stamp"] = None
    path = _path(HISTORY_NAME)

    with _lock:
        if path and os.path.exists(path):
            os.remove(path)


# ---- nudges ----------------------------------------------------------------------------------

def _minutes_of(when):
    moment = datetime.fromtimestamp(when)
    return moment.hour * 60 + moment.minute


def _within(span, when):
    """Whether the wall-clock time [when] falls in a daily ((h, m), (h, m)) span, across midnight too."""
    start, end = span[0][0] * 60 + span[0][1], span[1][0] * 60 + span[1][1]
    minute = _minutes_of(when)
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def _held(rule, board, now):
    """Whether [rule]'s limit has been passed for all of its minutes, with no hole in the record."""
    field = rule["reading"]
    span = rule["minutes"] * 60
    rows = _rows(f"SELECT ts, {field} FROM readings WHERE board = ? AND {field} IS NOT NULL"
                 " AND ts >= ? AND ts <= ? ORDER BY ts", (board, now - span - GAP_SECONDS, now))

    def past(value):
        return value > rule["above"] if rule["above"] is not None else value < rule["below"]

    # The reading just before the stretch began says whether it held from its start.
    before = [row for row in rows if row[0] <= now - span]
    during = [row for row in rows if row[0] > now - span]

    if not before or not during:
        return False

    chain = [before[-1]] + during

    if any(later[0] - earlier[0] > GAP_SECONDS for earlier, later in zip(chain, chain[1:])):
        return False

    return all(past(value) for _, value in chain)


def _duration(minutes):
    minutes = int(round(minutes))

    if minutes < 60:
        return f"{minutes} minutes"

    hours, left = divmod(minutes, 60)

    if left == 30:
        return "an hour and a half" if hours == 1 else f"{hours} and a half hours"

    if left:
        return f"{hours} hour{'s' if hours != 1 else ''} and {left} minutes"

    return "an hour" if hours == 1 else f"{hours} hours"


def _plain_number(value):
    return f"{value:g}"


def _said_value(field, value):
    return f"{value:.1f}" if field == "temperature" else f"{value:.0f}"


class _Blanks(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _nudge(board, readings, now, rules):
    if _within(rules["quiet_hours"], now):
        return None

    said = []

    for index, rule in enumerate(rules["nudges"]):
        field = rule["reading"]

        if field not in readings:
            continue

        last = _nudged.get((board, index))

        if last is not None and now - last < rules["nudge_again_hours"] * 3600:
            continue

        if not _held(rule, board, now):
            continue

        _nudged[(board, index)] = now
        place = _place(board)
        limit = rule["above"] if rule["above"] is not None else rule["below"]
        words = rule["say"] or ("{Place} has been " + ("over" if rule["above"] is not None else "under")
                                + " {limit} " + ("degrees" if field == "temperature" else "percent humidity")
                                + " for {duration}, sir.")
        said.append(words.format_map(_Blanks(
            place=place, Place=place[:1].upper() + place[1:], limit=_plain_number(limit),
            value=_said_value(field, readings[field]), duration=_duration(rule["minutes"]))))

    return " ".join(said) or None


# ---- a stretch of time, as it is said ---------------------------------------------------------------

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty five": 45, "twenty four": 24, "forty eight": 48,
    "couple of": 2, "couple": 2, "few": 3,
}
_UNITS = {"minute": 60, "minutes": 60, "min": 60, "mins": 60, "hour": 3600, "hours": 3600,
          "day": 86400, "days": 86400}

_NIGHT_WORDS = ("overnight", "over night", "last night", "through the night", "during the night", "in the night")

_WORD = re.compile(r"[a-z0-9]+")


def _words(text):
    return _WORD.findall(str(text or "").casefold().replace("'", ""))


def _at(day, hour_minute):
    return datetime(day.year, day.month, day.day, hour_minute[0], hour_minute[1])


def _wraps(span):
    return span[0] > span[1]


def _latest(span, now):
    """The latest stretch of a daily span ((h, m), (h, m)) that has begun by [now]: (start, end), end no later than now."""
    moment = datetime.fromtimestamp(now)

    for back in range(0, 3):
        day = moment.date() - timedelta(days=back)
        began = _at(day, span[0])
        ended = _at(day + timedelta(days=1) if _wraps(span) else day, span[1])

        if began <= moment:
            return began.timestamp(), min(ended, moment).timestamp()

    return None


def _night(now, overnight):
    """The night just gone, or the one still under way: "overnight" at 11 pm means last night, not the last hour."""
    if not _wraps(overnight):
        return _latest(overnight, now)

    moment = datetime.fromtimestamp(now)
    began = _at(moment.date() - timedelta(days=1), overnight[0])
    return began.timestamp(), min(_at(moment.date(), overnight[1]), moment).timestamp()


_HALF = re.compile(r" (?:in |over |during |for )?(?:the )?(?:last|past|previous) half (?:an )?hour ")
_LAST = re.compile(r" (?:in |over |during |for )?(?:the )?(?:last|past|previous) "
                   r"(?:(\d+|[a-z]+(?: [a-z]+)?) )?(minutes?|mins?|hours?|days?) ")


def _count(words):
    """"24", "twenty four", "couple of", or nothing (one) as a number; None for anything else."""
    if not words:
        return 1

    if words.isdigit():
        return int(words)

    return _NUMBER_WORDS.get(words, _NUMBER_WORDS.get(words.split()[-1]))


def window(text, now=None, rules=None):
    """The stretch of time [text] asks about: {"start", "end", "spoken"}, or None.

    A stretch that has not begun yet ("this evening", asked at noon) has a
    start of None, so it can be said so.
    """
    now = _clock() if now is None else now
    rules = rules or settings()
    said = " " + " ".join(_words(text)) + " "
    moment = datetime.fromtimestamp(now)
    midnight = datetime(moment.year, moment.month, moment.day)

    if _HALF.search(said):
        return {"start": now - 1800, "end": now, "spoken": "in the last half hour"}

    match = _LAST.search(said)

    if match:
        count, name = _count(match.group(1)), match.group(2).rstrip("s")
        name = "minute" if name == "min" else name

        if not count:
            return None

        spoken = f"in the last {name}" if count == 1 else f"in the last {count} {name}s"
        return {"start": now - count * _UNITS[name], "end": now, "spoken": spoken}

    for phrase in _NIGHT_WORDS:
        if f" {phrase} " in said:
            start, end = _night(now, rules["overnight"])
            return {"start": start, "end": end, "spoken": "last night" if phrase == "last night" else "overnight"}

    if " tonight " in said:
        # From the evening on, through the small hours: "tonight" at 1 am is the evening before.
        evening = rules["day_parts"].get("evening", rules["overnight"])
        tonight = (evening[0], rules["overnight"][1])
        stretch = _latest(tonight, now) if _wraps(tonight) else None

        # Ended already (at 3 pm the latest is last night's): tonight has not begun.
        if stretch is None or stretch[1] < now - 60:
            return {"start": None, "end": None, "spoken": "tonight"}

        return {"start": stretch[0], "end": stretch[1], "spoken": "tonight"}

    since = " since " in said

    for part, span in rules["day_parts"].items():
        earlier = f" yesterday {part} " in said

        if not (f" this {part} " in said or earlier or (since and f" {part} " in said)):
            continue

        day = midnight - timedelta(days=1) if earlier else midnight
        began, ended = _at(day, span[0]).timestamp(), _at(day, span[1]).timestamp()

        if began > now:
            return {"start": None, "end": None, "spoken": f"this {part}"}

        if since:
            return {"start": began, "end": now, "spoken": f"since {'yesterday' if earlier else 'this'} {part}"}

        return {"start": began, "end": min(ended, now), "spoken": f"{'yesterday' if earlier else 'this'} {part}"}

    if " yesterday " in said:
        start = midnight - timedelta(days=1)

        if " since yesterday " in said:
            return {"start": start.timestamp(), "end": now, "spoken": "since yesterday"}

        return {"start": start.timestamp(), "end": midnight.timestamp(), "spoken": "yesterday"}

    if " today " in said or " so far today " in said:
        return {"start": midnight.timestamp(), "end": now, "spoken": "today"}

    if " this week " in said:
        monday = midnight - timedelta(days=moment.weekday())
        return {"start": monday.timestamp(), "end": now, "spoken": "this week"}

    return None


# ---- what is asked ---------------------------------------------------------------------------------

_STARTS = ("what", "whats", "how", "hows", "was", "were", "did", "has", "have", "is", "when", "show", "chart",
           "graph", "plot", "give", "tell", "any", "anyone", "anybody", "draw", "pull", "bring")
_POLITE = ("jarvis", "hey", "ok", "okay", "so", "and", "please", "can", "you", "could", "would")
_FUTURE = ("will", "tomorrow", "forecast", "gonna", "going")

_HIGH = ("warmest", "hottest", "highest", "peak", "peaked", "max", "maximum", "top", "most", "dampest", "muggiest")
_LOW = ("coldest", "coolest", "chilliest", "lowest", "min", "minimum", "least", "driest")
_HIGH_WHEN_REACHED = ("warm", "warmer", "hot", "hotter", "high", "higher", "humid", "damp", "muggy", "up")
_LOW_WHEN_REACHED = ("cold", "colder", "cool", "cooler", "chilly", "low", "lower", "dry", "down")
# "the coldest it got": asking after a reading, though sensors.py's own cues are for now.
_SUPERLATIVES = {"temperature": ("warmest", "hottest", "coldest", "coolest", "chilliest"),
                 "humidity": ("dampest", "muggiest", "driest")}
_REACHED = ("get", "got", "reach", "reached", "go", "went", "climb", "climbed", "drop", "dropped", "fall", "fell")


# Asked in the past tense with no stretch named ("how cold did it get in my
# room", "what was the warmest my room got"), a question is about today so
# far; "how cold is my room" is the reading now, and stays the plan's.
_PAST = ("did", "got", "reached", "went", "dropped", "fell", "climbed", "peaked", "was", "were", "been")

# Before this hour, "today" is too short to say much: the last 24 hours instead.
_SO_FAR_HOURS = 3


def _so_far(now=None):
    """Today so far, or the last 24 hours in the small hours."""
    now = _clock() if now is None else now
    moment = datetime.fromtimestamp(now)

    if moment.hour < _SO_FAR_HOURS:
        return {"start": now - 86400, "end": now, "spoken": "in the last 24 hours"}

    return {"start": datetime(moment.year, moment.month, moment.day).timestamp(), "end": now, "spoken": "today"}


def _strip_polite(words):
    while words and words[0] in _POLITE:
        words = words[1:]

    return words


def _place(board):
    """A board's room as said to you: the house plan's name for it ("your room"), else the board's own."""
    try:
        room = house.room_for(board)
    except Exception:
        room = None

    return house.spoken_room(room) if room else sensors._place(board)


def _boards_with_history():
    return [row[0] for row in _rows("SELECT DISTINCT board FROM readings")]


def _where(words, text):
    """The place asked after: (spoken, [board names], one sentence each?) or a refusal string, or None."""
    room = house.named_room(text)

    if room is not None:
        name, boards = room
        spoken = house.spoken_room(name)
        # And the boards that reported there before JARVIS last started.
        boards = sorted(set(boards) | {board for board in _boards_with_history() if house.room_for(board) == name})

        if not boards:
            return f"{spoken[:1].upper() + spoken[1:]} has no sensor yet, sir."

        return [(spoken, boards)]

    boards = sorted(set(_boards_with_history()) | set(sensors.known()))
    named = [name for name in boards if sensors._has_phrase(words, " ".join(_words(name)))]

    if named:
        longest = max(named, key=len)
        return [(_place(longest), [longest])]

    if any(sensors._has_phrase(words, phrase) for phrase in sensors._INDOORS) and boards:
        return [(_place(name), [name]) for name in boards]

    return None


def question(text):
    """What [text] asks of the record, or None if it is not a question for it.

    {"what": "temperature" | "humidity" | "presence", "aspect": "high" | "low" | "summary",
     "window": window(), "where": [(spoken place, [boards])] or a sentence saying why not}
    """
    words = _strip_polite(_words(text))

    if not words or words[0] not in _STARTS or any(word in _FUTURE for word in words):
        return None

    asked = [what for what in ("presence", "humidity", "temperature")
             if any(word in sensors._ASKING[what] + _SUPERLATIVES.get(what, ()) for word in words)]

    if not asked:
        return None

    stretch = window(text)

    if stretch is None and any(word in _PAST for word in words):
        stretch = _so_far()

    if stretch is None:
        return None

    where = _where(words, text)

    if where is None:
        return None

    if any(word in _HIGH for word in words):
        aspect = "high"
    elif any(word in _LOW for word in words):
        aspect = "low"
    elif any(word in _REACHED for word in words) and any(word in _HIGH_WHEN_REACHED for word in words):
        aspect = "high"
    elif any(word in _REACHED for word in words) and any(word in _LOW_WHEN_REACHED for word in words):
        aspect = "low"
    else:
        aspect = "summary"

    return {"what": asked[0], "aspect": aspect, "window": stretch, "where": where}


# ---- answering -------------------------------------------------------------------------------------

def _clock_words(when, stretch, bare=False):
    """"at 2:14 am", "yesterday at 9 pm", "on Tuesday at 3:05 pm": a moment, as said.

    [bare] drops a leading "at", for "from 2 am to 3 am".
    """
    moment = datetime.fromtimestamp(when)
    hour = moment.hour % 12 or 12
    said = f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}" if moment.minute else \
        f"{hour} {'am' if moment.hour < 12 else 'pm'}"

    if stretch["end"] - stretch["start"] <= 86400 + 3600:
        return said if bare else f"at {said}"

    today = datetime.fromtimestamp(_clock()).date()

    if moment.date() == today:
        return f"today at {said}"

    if moment.date() == today - timedelta(days=1):
        return f"yesterday at {said}"

    return f"on {moment.strftime('%A')} at {said}"


def _first_up(text):
    return text[:1].upper() + text[1:]


def _readings(field, boards, stretch):
    marks = ",".join("?" for _ in boards)
    return _rows(f"SELECT ts, {field} FROM readings WHERE board IN ({marks}) AND {field} IS NOT NULL"
                 " AND ts >= ? AND ts <= ? ORDER BY ts", (*boards, stretch["start"], stretch["end"]))


def _movements(boards, stretch):
    marks = ",".join("?" for _ in boards)
    return [row[0] for row in _rows(f"SELECT ts FROM readings WHERE board IN ({marks}) AND motion = 1"
                                    " AND ts >= ? AND ts <= ? ORDER BY ts",
                                    (*boards, stretch["start"], stretch["end"]))]


def _reported(boards, stretch):
    marks = ",".join("?" for _ in boards)
    return bool(_rows(f"SELECT 1 FROM readings WHERE board IN ({marks}) AND ts >= ? AND ts <= ? LIMIT 1",
                      (*boards, stretch["start"], stretch["end"])))


def _since_note(boards, stretch):
    """" I've only been keeping a record since 9:14 pm.", when the record starts partway through."""
    marks = ",".join("?" for _ in boards)
    first = _rows(f"SELECT MIN(ts) FROM readings WHERE board IN ({marks})", tuple(boards))
    first = first[0][0] if first else None

    if first is None or first <= stretch["start"] + GAP_SECONDS:
        return ""

    return f" I've only been keeping a record since {_clock_words(first, stretch, bare=True)}."


def _trend(field, rows, now):
    """"and rising", "and falling", "and steady": how the last half hour has gone."""
    recent = [value for ts, value in rows if ts >= now - 10 * 60]
    earlier = [value for ts, value in rows if now - 40 * 60 <= ts < now - 10 * 60]

    if not recent or not earlier:
        return ""

    change = sum(recent) / len(recent) - sum(earlier) / len(earlier)
    step = 0.3 if field == "temperature" else 2.0

    return " and rising" if change > step else " and falling" if change < -step else " and steady"


def _sentence(what, aspect, spoken, boards, stretch, rows):
    unit = " degrees" if what == "temperature" else " percent"
    value = lambda number: _said_value(what, number)  # noqa: E731

    if not rows:
        if _reported(boards, stretch):
            kind = "temperature" if what == "temperature" else "humidity"
            return f"I have no {kind} reading from {spoken} {stretch['spoken']}, sir."

        return f"I have no record from {spoken} {stretch['spoken']}; its sensor wasn't reporting, sir."

    highest = max(rows, key=lambda row: row[1])
    lowest = min(rows, key=lambda row: row[1])
    subject = _first_up(spoken) if what == "temperature" else f"Humidity in {spoken}"

    if aspect == "high":
        verb = "got up to" if what == "temperature" else "peaked at"
        return f"{subject} {verb} {value(highest[1])}{unit} {stretch['spoken']}, {_clock_words(highest[0], stretch)}, sir."

    if aspect == "low":
        verb = "got down to" if what == "temperature" else "got as low as"
        return f"{subject} {verb} {value(lowest[1])}{unit} {stretch['spoken']}, {_clock_words(lowest[0], stretch)}, sir."

    average = sum(row[1] for row in rows) / len(rows)

    if highest[1] - lowest[1] < (0.2 if what == "temperature" else 1):
        said = f"{subject} stayed around {value(average)}{unit} {stretch['spoken']}, sir."
    else:
        said = (f"{subject} ranged from {value(lowest[1])} to {value(highest[1])}{unit} {stretch['spoken']}, "
                f"averaging {value(average)}, sir.")

    now = _clock()

    if stretch["end"] >= now - 60 and rows[-1][0] >= now - GAP_SECONDS:
        said += f" It's {value(rows[-1][1])} now{_trend(what, rows, now)}."

    return said


def _spells(moments):
    spells = []

    for moment in moments:
        if spells and moment - spells[-1][1] <= SPELL_SECONDS:
            spells[-1][1] = moment
        else:
            spells.append([moment, moment])

    return spells


def _movement_sentence(spoken, boards, stretch, moments):
    if not moments:
        if not _reported(boards, stretch):
            return f"I have no record from {spoken} {stretch['spoken']}; its sensor wasn't reporting, sir."

        return f"No movement in {spoken} {stretch['spoken']}, sir."

    spells = _spells(moments)

    if len(spells) == 1:
        if spells[0][1] - spells[0][0] < 60:
            return f"Movement in {spoken} once {stretch['spoken']}, {_clock_words(spells[0][0], stretch)}, sir."

        return (f"Movement in {spoken} {stretch['spoken']}, from {_clock_words(spells[0][0], stretch, bare=True)} "
                f"to {_clock_words(spells[0][1], stretch, bare=True)}, sir.")

    return (f"Movement in {spoken} {stretch['spoken']}, {len(spells)} separate times: the first "
            f"{_clock_words(spells[0][0], stretch)}, the last {_clock_words(spells[-1][1], stretch)}, sir.")


def answer(text):
    """Answer [text]: (what to say, chart png or None, chart title, [boards]); None if not a question for it."""
    asked = question(text)

    if asked is None:
        return None

    where, stretch, what = asked["where"], asked["window"], asked["what"]

    if isinstance(where, str):
        return where, None, "", []

    if stretch["start"] is None:
        return f"It isn't {stretch['spoken']} yet, sir.", None, "", []

    sentences, series, boards = [], [], []

    for spoken, names in where:
        boards += names

        if what == "presence":
            moments = _movements(names, stretch)
            sentences.append(_movement_sentence(spoken, names, stretch, moments))
            series.append((spoken, moments))
        else:
            rows = _readings(what, names, stretch)
            sentences.append(_sentence(what, asked["aspect"], spoken, names, stretch, rows))
            series.append((spoken, rows))

    said = " ".join(sentences) + _since_note(boards, stretch)

    if not any(points for _, points in series):
        return said, None, "", boards

    places = " & ".join(spoken for spoken, _ in where)
    title = " \u00b7 ".join((places.upper(), what.upper(), stretch["spoken"].upper()))
    return said, chart(what, series, chart_span(boards, stretch), title), title, boards


def chart_span(boards, stretch):
    """[stretch] as drawn: from where the record of [boards] starts, if that is partway through.

    A record begun at 3 pm, charted for "today", is otherwise mostly empty
    axis from midnight on.
    """
    marks = ",".join("?" for _ in boards)
    first = _rows(f"SELECT MIN(ts) FROM readings WHERE board IN ({marks}) AND ts >= ? AND ts <= ?",
                  (*boards, stretch["start"], stretch["end"])) if boards else []
    first = first[0][0] if first else None

    if first is None or first <= stretch["start"] + GAP_SECONDS:
        return stretch

    margin = max(60.0, (stretch["end"] - first) * 0.03)
    return dict(stretch, start=max(stretch["start"], first - margin))


# ---- the night, said in the morning ---------------------------------------------------------------------

def _given(day, mark=False):
    """Whether the night's report has been given on [day] ("2026-10-02"); with [mark], that it now has."""
    with _lock:
        connection = _connect()

        if connection is None:
            return True

        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS reports (day TEXT PRIMARY KEY)")

                if mark:
                    connection.execute("INSERT OR IGNORE INTO reports (day) VALUES (?)", (day,))
                    return True

                return connection.execute("SELECT 1 FROM reports WHERE day = ?", (day,)).fetchone() is not None
        finally:
            connection.close()


def _range_words(what, rows):
    low, high = min(value for _, value in rows), max(value for _, value in rows)
    unit = " degrees" if what == "temperature" else " percent"

    if high - low < (0.2 if what == "temperature" else 1):
        return f"stayed around {_said_value(what, sum(value for _, value in rows) / len(rows))}{unit}"

    return f"went from {_said_value(what, low)} to {_said_value(what, high)}{unit}"


def morning_report(now=None):
    """The night just gone, once a morning: what to say, or None.

    Given the first time it is asked for between the night's end and
    morning_report's "until" (noon, by default), and only once a day, kept
    in the record itself so a restart does not repeat it. None when the
    boards said nothing overnight, or sensors.json says "morning_report": false.
    """
    now = _clock() if now is None else now
    rules = settings()
    until = rules["morning_until"]

    if until is None:
        return None

    moment = datetime.fromtimestamp(now)
    minute = moment.hour * 60 + moment.minute
    night_end = rules["overnight"][1][0] * 60 + rules["overnight"][1][1]

    if not night_end <= minute < until[0] * 60 + until[1]:
        return None

    day = moment.date().isoformat()

    if _given(day):
        return None

    start, end = _night(now, rules["overnight"])
    stretch = {"start": start, "end": end, "spoken": "overnight"}
    rooms = {}

    for board in _boards_with_history():
        if _reported([board], stretch):
            rooms.setdefault(_place(board), []).append(board)

    _given(day, mark=True)

    if not rooms:
        return None

    sentences = []

    for spoken, boards in sorted(rooms.items()):
        parts = []
        temperature = _readings("temperature", boards, stretch)
        humidity = _readings("humidity", boards, stretch)
        moments = _movements(boards, stretch)

        if temperature:
            parts.append(_range_words("temperature", temperature))

        if humidity:
            parts.append(f"humidity {_range_words('humidity', humidity)}")

        if moments:
            parts.append(f"the last movement was {_clock_words(moments[-1], stretch)}")
        elif parts:
            parts.append("there was no movement at all")

        if not parts:
            continue

        said = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        sentences.append(f"{spoken} {said}.")

    if not sentences:
        return None

    sentences[0] = "Overnight, " + sentences[0][:-1] + ", sir."
    return " ".join(sentences[:1] + [_first_up(sentence) for sentence in sentences[1:]]) \
        + _since_note([board for boards in rooms.values() for board in boards], stretch)


# ---- the chart --------------------------------------------------------------------------------------

_BACKDROP = "#0a1017"
_PANEL = "#0d1620"
_INK = "#e4f0fa"
_FAINT = "#7d93a8"
_GRID = "#1d2c3a"
_LINES = ("#5fc8f5", "#f5b85f", "#8cf58a", "#f57a9b")
_HOT, _COLD = "#ff8a5b", "#6fb8ff"


def runs(rows):
    """[rows] split wherever the record has a hole longer than GAP_SECONDS (JARVIS closed, a board unplugged)."""
    pieces = []

    for row in rows:
        if pieces and row[0] - pieces[-1][-1][0] <= GAP_SECONDS:
            pieces[-1].append(row)
        else:
            pieces.append([row])

    return pieces


def _thinned(rows, most=CHART_POINTS):
    if len(rows) <= most:
        return rows

    size = len(rows) / most
    thinned = []

    for index in range(most):
        bucket = rows[int(index * size):int((index + 1) * size)] or rows[-1:]
        thinned.append((sum(ts for ts, _ in bucket) / len(bucket), sum(value for _, value in bucket) / len(bucket)))

    return thinned


def chart(what, series, stretch, title):
    """A PNG of [series] ([(label, rows)]) over [stretch], drawn to match the HUD; None if it cannot be drawn."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception as error:
        print(f"[JARVIS] could not draw the sensor chart: {error}")
        return None

    figure, axes = plt.subplots(figsize=(7.2, 4.0), dpi=110)
    figure.patch.set_facecolor(_BACKDROP)
    axes.set_facecolor(_PANEL)
    start, end = datetime.fromtimestamp(stretch["start"]), datetime.fromtimestamp(stretch["end"])

    if what == "presence":
        hours = max(1, int((stretch["end"] - stretch["start"] + 3599) // 3600))
        bucket = 3600 if hours <= 48 else 86400
        width = bucket / 86400 * 0.8

        for index, (label, moments) in enumerate(series):
            counts = {}

            for spell in _spells(moments):
                slot = stretch["start"] + ((spell[0] - stretch["start"]) // bucket) * bucket
                counts[slot] = counts.get(slot, 0) + 1

            axes.bar([datetime.fromtimestamp(slot + bucket / 2) for slot in sorted(counts)],
                     [counts[slot] for slot in sorted(counts)], width=width,
                     color=_LINES[index % len(_LINES)], alpha=0.85, label=label)

        axes.set_ylabel("spells of movement", color=_INK, fontsize=9)
        axes.yaxis.get_major_locator().set_params(integer=True)
    else:
        unit = "\u00b0C" if what == "temperature" else "%"
        everything = [value for _, rows in series for _, value in rows]
        floor = min(everything) - (0.5 if what == "temperature" else 2)
        ceiling = max(everything) + (0.5 if what == "temperature" else 2)
        axes.set_ylim(floor, ceiling)

        for index, (label, rows) in enumerate(series):
            if not rows:
                continue

            colour = _LINES[index % len(_LINES)]
            pieces = runs(rows)

            # Each unbroken run drawn on its own: a line across a hole in the
            # record would show readings that were never taken.
            for number, piece in enumerate(pieces):
                points = _thinned(piece, max(2, round(CHART_POINTS * len(piece) / len(rows))))
                times = [datetime.fromtimestamp(ts) for ts, _ in points]
                values = [value for _, value in points]
                axes.plot(times, values, color=colour, linewidth=2.0, label=label if number == 0 else None,
                          marker="o" if len(piece) == 1 else None, markersize=3)
                axes.fill_between(times, values, floor, color=colour, alpha=0.08)

            for before, after in zip(pieces, pieces[1:]):
                axes.axvspan(datetime.fromtimestamp(before[-1][0]), datetime.fromtimestamp(after[0][0]),
                             color=_GRID, alpha=0.35, linewidth=0)

            high, low = max(rows, key=lambda row: row[1]), min(rows, key=lambda row: row[1])

            for (ts, value), mark in ((high, _HOT), (low, _COLD)):
                moment = datetime.fromtimestamp(ts)
                # Near the right-hand edge, the label goes to the left of its point.
                late = ts > stretch["start"] + (stretch["end"] - stretch["start"]) * 0.8
                axes.scatter([moment], [value], color=mark, s=36, zorder=5)
                axes.annotate(f"{_said_value(what, value)}{unit}  {moment.strftime('%H:%M')}", (moment, value),
                              textcoords="offset points", xytext=(-6 if late else 6, 6 if mark == _HOT else -14),
                              ha="right" if late else "left", color=mark, fontsize=8)

        axes.set_ylabel(unit, color=_INK, fontsize=9)

    axes.set_xlim(start, end)
    span = stretch["end"] - stretch["start"]
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if span <= 2 * 86400 else "%a %d"))
    axes.set_title(title, color=_INK, fontsize=11, pad=12, loc="left")
    axes.tick_params(colors=_FAINT, labelsize=8)
    axes.grid(True, color=_GRID, linewidth=0.7)
    axes.set_axisbelow(True)

    for spine in axes.spines.values():
        spine.set_color(_GRID)

    if len(series) > 1:
        axes.legend(facecolor=_PANEL, edgecolor=_GRID, labelcolor=_INK, fontsize=8)

    figure.tight_layout()
    buffer = io.BytesIO()

    try:
        figure.savefig(buffer, format="png", facecolor=_BACKDROP)
    except Exception as error:
        print(f"[JARVIS] could not draw the sensor chart: {error}")
        return None
    finally:
        plt.close(figure)

    return buffer.getvalue()
