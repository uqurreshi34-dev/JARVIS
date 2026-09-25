"""'When did I last ask about bitcoin?' -- searching what you have asked, by meaning.

Every command JARVIS hears is already written to jarvis-log.txt, word for
word, with the date. This answers questions about that history:

    when did I last ask about bitcoin        -> the most recent one, and when
    have I asked you about the weather before -> yes or no, how often, latest
    how many times did I ask about github     -> a count
    what did I ask you about the markets      -> the latest few, as said

with an optional "today", "yesterday", "this week", "last week", "this
month" or "last month".

The question is matched by shape, like presence and note searches; the
topic is matched by meaning, so "crypto" finds "what's the bitcoin price".
Each logged command is encoded once and kept in the vector store, so a
long history costs nothing to search after the first time, and that first
time happens quietly in the background shortly after start-up.

Spoken commands are short, and a short command scores surprisingly high
against unrelated topics ("bitcoin" against "what's on my calendar" is
0.31). So a command counts only when it is nearly as close as the closest
one, not merely above a floor -- measured on real command wording, that is
what keeps counts honest.

Nothing is sent anywhere.
"""

import ast
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

from actions import files, journal


# ---- recognising the question ---------------------------------------------

_ASKED = r"(?:ask|asked|asking|mention|mentioned|talk|talked|speak|spoke|spoken)"

_YOU = r"(?:\s+(?:you|jarvis))?"

_ABOUT = r"(?:about|for|regarding|to|on|with)"

_WINDOW = (
    r"(?:\s+(?P<window>today|yesterday|this\s+week|last\s+week|this\s+month|"
    r"last\s+month|recently|lately|before|ever|previously|at\s+all))?"
)

_QUESTIONS = (
    ("last", re.compile(
        rf"^when\s+did\s+i\s+last\s+{_ASKED}{_YOU}(?:\s+(?:to|with))?\s+{_ABOUT}\s+(?P<topic>.+?){_WINDOW}$")),
    ("last", re.compile(
        rf"^when\s+(?:was|is)\s+the\s+last\s+time\s+i\s+{_ASKED}{_YOU}(?:\s+(?:to|with))?\s+"
        rf"{_ABOUT}\s+(?P<topic>.+?){_WINDOW}$")),
    ("last", re.compile(
        rf"^when\s+did\s+i\s+{_ASKED}{_YOU}(?:\s+(?:to|with))?\s+{_ABOUT}\s+(?P<topic>.+?)(?:\s+last)?{_WINDOW}$")),
    ("count", re.compile(
        rf"^how\s+(?:many\s+times|often)\s+(?:have|did|do|had)\s+i\s+(?:ever\s+)?{_ASKED}{_YOU}"
        rf"(?:\s+(?:to|with))?\s+{_ABOUT}\s+(?P<topic>.+?){_WINDOW}$")),
    ("ever", re.compile(
        rf"^(?:have|did|had)\s+i\s+(?:ever\s+)?(?:already\s+)?{_ASKED}{_YOU}(?:\s+(?:to|with))?\s+"
        rf"{_ABOUT}\s+(?P<topic>.+?){_WINDOW}$")),
    ("list", re.compile(
        rf"^what\s+(?:did|have)\s+i\s+(?:ever\s+)?{_ASKED}{_YOU}(?:\s+(?:to|with))?\s+"
        rf"{_ABOUT}\s+(?P<topic>.+?){_WINDOW}$")),
)

_EMPTY_TOPICS = frozenset({"it", "that", "this", "them", "something", "anything", "things", "stuff"})

# Used when a question reached here through the model rather than by shape:
# the wording still says which kind of answer is wanted.
_MODE_WORDS = (
    ("count", re.compile(r"\bhow\s+(?:many\s+times|often)\b")),
    ("ever", re.compile(r"^(?:have|did|had)\s+i\b|\bever\b|\bbefore\b")),
    ("list", re.compile(r"^what\s+(?:did|have)\s+i\b")),
)


def _normalise(said):
    text = str(said or "").casefold().replace("'", "").replace("’", "")
    text = " ".join(re.sub(r"[^\w\s]", " ", text).split())

    previous = None

    while previous != text:
        previous = text
        text = re.sub(r"^(?:jarvis|hey|please|ok|okay)\s+", "", text)

    return text


def question(said):
    """(mode, topic, window) for a question about past commands, or None.

    [said] may be raw or already normalised.
    """
    text = _normalise(said)

    for mode, pattern in _QUESTIONS:
        match = pattern.match(text)

        if not match:
            continue

        topic = match.group("topic").strip()
        window = " ".join((match.group("window") or "").split()) or None

        if window in ("recently", "lately", "before", "ever", "previously", "at all"):
            window = None

        if not topic or topic in _EMPTY_TOPICS:
            return None

        return mode, topic, window

    return None


def _mode_of(said):
    text = _normalise(said)

    for mode, pattern in _MODE_WORDS:
        if pattern.search(text):
            return mode

    return "last"


def _window_of(said):
    text = _normalise(said)

    for window in ("yesterday", "today", "this week", "last week", "this month", "last month"):
        if re.search(rf"\b{window}\b", text):
            return window

    return None


# ---- the log ---------------------------------------------------------------

_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+command\s+(?P<rest>.+)$"
)

_TAIL = re.compile(r" -> (?P<intent>\w+)(?: \[(?P<detail>.*)\])? \((?P<route>\w+)\)$")

# Commands that are about the log itself are not what anyone is looking for.
_OWN_INTENTS = frozenset({"search_log", "read_log", "log_summary"})

_cache = {"key": None, "entries": []}
_cache_lock = threading.Lock()


def _log_files():
    base = files.root()

    if not base:
        return []

    try:
        names = [
            name for name in os.listdir(base)
            if name == journal.FILENAME
            or (name.startswith("jarvis-log-") and name.endswith(".txt"))
        ]
    except OSError:
        return []

    return [os.path.join(base, name) for name in sorted(names)]


def _parse(line):
    """(moment, said, intent) for one command line, or None."""
    match = _LINE.match(line.rstrip("\n"))

    if not match:
        return None

    rest = match.group("rest")
    tail = _TAIL.search(rest)

    if not tail:
        return None

    try:
        said = ast.literal_eval(rest[:tail.start()])
    except (ValueError, SyntaxError):
        return None

    if not isinstance(said, str) or not said.strip():
        return None

    try:
        moment = datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    return moment, " ".join(said.split()), tail.group("intent")


def entries():
    """Every logged command, oldest first, as (moment, said, intent)."""
    paths = _log_files()
    key = []

    for path in paths:
        try:
            status = os.stat(path)
        except OSError:
            continue

        key.append((path, status.st_mtime_ns, status.st_size))

    key = tuple(key)

    with _cache_lock:
        if key == _cache["key"]:
            return list(_cache["entries"])

    found = []

    for path, _mtime, _size in key:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    parsed = _parse(line)

                    if parsed:
                        found.append(parsed)
        except OSError:
            continue

    found.sort(key=lambda item: item[0])

    with _cache_lock:
        _cache["key"] = key
        _cache["entries"] = found

    return list(found)


def _searchable(items):
    """Commands worth matching: not questions about the log itself."""
    return [
        item for item in items
        if item[2] not in _OWN_INTENTS and question(item[1]) is None
    ]


# ---- matching --------------------------------------------------------------

# Measured on real command wording with the local model. A command must
# score at least this, and be nearly as close as the closest command.
_MINIMUM = 0.35
_MARGIN = 0.12

_SMALL_WORDS = frozenset({"my", "the", "a", "an", "your", "our", "any", "some", "all"})

_PLAIN_WORDS = frozenset({
    "the", "and", "for", "about", "my", "your", "with", "that", "this", "from",
    "what", "whats", "did", "have", "any", "all", "our", "are", "was", "were",
    "has", "had", "been", "into", "its", "you", "jarvis", "please", "can",
    "could", "would", "will", "tell", "give", "show", "how", "when", "where",
})


def _words(text):
    found = set()

    for word in re.findall(r"[a-z0-9]+", str(text or "").casefold()):
        if len(word) < 3 or word in _PLAIN_WORDS:
            continue

        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]

        found.add(word)

    return found


def _scores(topic, texts):
    """(meaning, shared word) for each text against [topic].

    Meaning is None throughout when the model is unavailable.
    """
    core = " ".join(word for word in topic.casefold().split() if word not in _SMALL_WORDS) or topic
    wanted = _words(core)

    try:
        from actions import semantic_memory

        meaning = semantic_memory.similarities(core, texts)
    except Exception as error:
        print(f"[JARVIS] searching the log by meaning failed, using words: {error}")
        meaning = None

    return [
        (None if meaning is None else meaning[index], bool(wanted & _words(text)))
        for index, text in enumerate(texts)
    ]


def _relevant(scores):
    """Which texts count, given their (meaning, shared word) scores.

    A command that contains the topic's own word always counts. Otherwise
    it must be close in meaning: above the minimum, and nearly as close as
    the closest command -- which is what keeps "bitcoin" from counting
    "what's on my calendar".
    """
    meanings = [meaning for meaning, _shared in scores if meaning is not None]
    best = max(meanings) if meanings else None
    cut = None if best is None or best < _MINIMUM else max(_MINIMUM, best - _MARGIN)

    return [
        shared or (cut is not None and meaning is not None and meaning >= cut)
        for meaning, shared in scores
    ]


def _in_window(moment, window, today):
    if not window:
        return True

    day = moment.date()
    monday = today - timedelta(days=today.weekday())

    if window == "today":
        return day == today

    if window == "yesterday":
        return day == today - timedelta(days=1)

    if window == "this week":
        return day >= monday

    if window == "last week":
        return monday - timedelta(days=7) <= day < monday

    if window == "this month":
        return (day.year, day.month) == (today.year, today.month)

    if window == "last month":
        first = today.replace(day=1)
        previous = first - timedelta(days=1)
        return (day.year, day.month) == (previous.year, previous.month)

    return True


def search(topic, window=None, today=None):
    """Logged commands about [topic], oldest first, as (moment, said, intent)."""
    topic = " ".join(str(topic or "").split())

    if not topic:
        return []

    today = today or date.today()
    candidates = [
        item for item in _searchable(entries())
        if _in_window(item[0], window, today)
    ]

    if not candidates:
        return []

    texts = list(dict.fromkeys(item[1].casefold() for item in candidates))
    counts = dict(zip(texts, _relevant(_scores(topic, texts))))

    return [item for item in candidates if counts[item[1].casefold()]]


# ---- what is said ----------------------------------------------------------

def _spoken_moment(moment, today):
    from actions import diary

    day = moment.date()
    clock = diary._spoken_clock(moment)

    if day == today:
        return f"today at {clock}"

    if day == today - timedelta(days=1):
        return f"yesterday at {clock}"

    spoken = f"on {day.day} {day.strftime('%B')}"

    if day.year != today.year:
        spoken = f"{spoken} {day.year}"

    return f"{spoken} at {clock}"


def _spoken_day(day, today):
    if day == today:
        return "today"

    if day == today - timedelta(days=1):
        return "yesterday"

    spoken = f"{day.day} {day.strftime('%B')}"

    return spoken if day.year == today.year else f"{spoken} {day.year}"


def _yours(topic):
    swaps = {"my": "your", "mine": "yours", "me": "you", "i": "you", "myself": "yourself"}

    return " ".join(swaps.get(word.casefold(), word) for word in topic.split())


def _times(count):
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def answer(said, topic=None, mode=None, window=None, today=None):
    """The spoken answer to a question about past commands."""
    today = today or date.today()
    asked = question(said)

    if asked:
        mode = mode or asked[0]
        topic = topic or asked[1]
        window = window or asked[2]

    mode = mode or _mode_of(said)
    window = window or _window_of(said)
    topic = " ".join(str(topic or "").split())

    if not topic:
        return "What would you like me to look for in the log, sir?"

    everything = _searchable(entries())

    if not everything:
        return "There's nothing in the log to search yet, sir."

    found = search(topic, window=window, today=today)
    about = _yours(topic)
    during = f" {window}" if window else ""
    since = _spoken_day(everything[0][0].date(), today)

    if not found:
        if window:
            return f"I can't find you asking about {about}{during}, sir."

        return f"I can't find you asking about {about}, sir. The log goes back to {since}."

    latest_moment, latest_said, _intent = found[-1]
    latest = _spoken_moment(latest_moment, today)

    if mode == "count":
        return f"{_times(len(found)).capitalize()}{during}, sir. Most recently {latest}: {latest_said}."

    if mode == "ever":
        return f"Yes, sir, {_times(len(found))}{during}. Most recently {latest}: {latest_said}."

    if mode == "list":
        shown = []

        for moment, text, _intent in reversed(found):
            if text.casefold() not in {item[1].casefold() for item in shown}:
                shown.append((moment, text))

            if len(shown) == 3:
                break

        parts = "; ".join(f"{text}, {_spoken_moment(moment, today)}" for moment, text in shown)

        return f"{_times(len(found)).capitalize()}{during}, sir. Most recently: {parts}."

    return f"{latest[0].upper()}{latest[1:]}, sir. You said: {latest_said}."


# ---- indexing in the background --------------------------------------------

def index_quietly(delay=30.0, batch=256, pause=0.2):
    """Encode logged commands the vector store does not have yet.

    Run once on a background thread after start-up, so the first question
    about the log does not wait for a long history to be encoded. Small
    batches with pauses, so it never competes with listening.
    """
    time.sleep(delay)

    try:
        from actions import semantic_memory, vector_store

        texts = list(dict.fromkeys(item[1].casefold() for item in _searchable(entries())))
        done = 0

        for start in range(0, len(texts), batch):
            if vector_store.vectors(texts[start:start + batch], semantic_memory._encode,
                                    semantic_memory.MODEL_ID) is None:
                return

            done += len(texts[start:start + batch])
            time.sleep(pause)

        if texts:
            print(f"[JARVIS] command log indexed for search ({done} commands)")
    except Exception as error:
        print(f"[JARVIS] could not index the command log: {error}")
