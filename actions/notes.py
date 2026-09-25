import os
import re
from datetime import date, datetime, timedelta


# Notes live in the JARVIS folder with everything else. Built from
# files.root() like every other store, so the test sandbox redirects it too.
_FILENAME = "notes.txt"

# Reading every note aloud gets tedious, so only the most recent are spoken.
_SPEAK_LIMIT = 5


def _notes_path():
    from actions import files

    folder = files.root()

    return os.path.join(folder, _FILENAME) if folder else None


def add(text):
    """Append a note with a timestamp. Returns True on success."""
    note = " ".join((text or "").split())

    if not note:
        return False

    path = _notes_path()

    if not path:
        return False

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {note}\n")

    except OSError as error:
        print(f"[JARVIS] could not save the note: {error}")
        return False

    print(f"[JARVIS] note saved to {path}")

    return True


def _read_lines():
    path = _notes_path()

    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return [line.strip() for line in handle if line.strip()]

    except OSError as error:
        print(f"[JARVIS] could not read your notes: {error}")
        return []


def _without_stamp(line):
    """Drop the leading timestamp so it is not read aloud."""
    if line.startswith("[") and "]" in line:
        return line.split("]", 1)[1].strip()

    return line


def count():
    return len(_read_lines())


def describe():
    """Spoken summary of the most recent notes."""
    lines = _read_lines()

    if not lines:
        return "You have no notes, sir."

    recent = [_without_stamp(line) for line in lines[-_SPEAK_LIMIT:]]

    if len(lines) == 1:
        return f"One note, sir: {recent[0]}."

    if len(lines) <= _SPEAK_LIMIT:
        listed = "; ".join(recent)
        return f"You have {len(lines)} notes, sir: {listed}."

    listed = "; ".join(recent)

    return (
        f"You have {len(lines)} notes, sir. The most recent {len(recent)}: "
        f"{listed}."
    )


def remove(text):
    """Delete notes containing this text. Returns (removed, remaining)."""
    wanted = " ".join((text or "").split()).casefold()

    if not wanted:
        return 0, count()

    lines = _read_lines()

    if not lines:
        return 0, 0

    kept = []
    removed = []

    for line in lines:
        if wanted in _without_stamp(line).casefold():
            removed.append(line)
        else:
            kept.append(line)

    if not removed:
        return 0, len(kept)

    path = _notes_path()

    if not path:
        return 0, len(lines)

    try:
        with open(path, "w", encoding="utf-8") as handle:
            for line in kept:
                handle.write(f"{line}\n")

    except OSError as error:
        print(f"[JARVIS] could not update your notes: {error}")
        return 0, len(lines)

    print(f"[JARVIS] removed {len(removed)} note(s) mentioning {text!r}")

    return len(removed), len(kept)


def describe_removal(text):
    """Remove notes and report what happened."""
    removed, remaining = remove(text)

    if not removed:
        return f"I couldn't find a note mentioning {text}, sir."

    if removed == 1:
        left = "none left" if not remaining else f"{remaining} left"

        return f"Removed {text} from your notes, sir. {left.capitalize()}."

    return f"Removed {removed} notes mentioning {text}, sir."


def clear():
    """Delete every note. Returns how many were removed."""
    lines = _read_lines()

    if not lines:
        return 0

    path = _notes_path()

    if not path:
        return 0

    try:
        os.remove(path)
    except OSError as error:
        print(f"[JARVIS] could not clear your notes: {error}")
        return 0

    return len(lines)


# ---- finding a note by what it is about -----------------------------------
#
# "What did I note about the boiler?" is matched by shape, the way presence
# and thanks are: a way of asking (what did I note, find my notes, do I have
# a note, what do my notes say), a word meaning "about", then the topic.
# The topic is then found by meaning, so "heating" finds the note about the
# boiler, with a shared word as a second signal for the short, general
# topics ("my car", "my cousin") that meaning alone scores low. Both are
# local; nothing is sent anywhere.

_ABOUT = (
    r"(?:about|on|regarding|concerning|for|mentioning|that\s+mentions?|"
    r"to\s+do\s+with|with)"
)

_ASKING = (
    r"(?:"
    r"(?:what|which)\s+(?:did|have)\s+i\s+(?:note|noted|jot|jotted|write|wrote|written)"
    r"(?:\s+down)?"
    r"|(?:what|which)\s+notes?\s+(?:do|did|have)\s+i\s+"
    r"(?:have|got|make|made|write|wrote|written|take|took|taken|save|saved)"
    r"|(?:what|which)\s+(?:do|did)\s+(?:my|the)\s+notes?\s+say"
    r"|(?:whats|what\s+is)\s+in\s+(?:my|the)\s+notes"
    r"|(?:find|search|search\s+for|search\s+through|look\s+for|look\s+up|"
    r"look\s+through|look\s+in|read|read\s+out|show|check|get|pull\s+up|"
    r"bring\s+up|give|tell)(?:\s+me)?(?:\s+(?:my|the|a|any|all|all\s+my))?\s+notes?"
    r"|(?:do|did|have)\s+i\s+(?:have|got|make|made|write|wrote|written|leave|"
    r"left|take|took|taken|save|saved)\s+(?:a|any|some)\s+notes?"
    r"|(?:is|are)\s+there\s+(?:a|any)\s+notes?"
    r"|any\s+notes?"
    r")"
)

_SEARCH = re.compile(rf"^{_ASKING}\s+{_ABOUT}\s+(.+)$")

# Nothing to search for.
_EMPTY_TOPICS = frozenset({"it", "that", "this", "them", "those", "these", "anything", "something"})

# Measured on the local model with a spread of real notes: every note that
# was about the topic scored 0.29 or more, every unrelated topic below 0.30.
# A shared word adds enough to carry the short topics the model underrates.
_MINIMUM = 0.30
_SHARED_WORD = 0.25

# Only notes nearly as good as the best are read out, so one clear answer
# is not followed by a weak second.
_CLOSE = 0.10
_SEARCH_LIMIT = 3

_SMALL_WORDS = frozenset({"my", "the", "a", "an", "your", "our", "any", "some", "all"})

# "anything mentioning the dentist" is a search for the dentist.
_VAGUE_LEAD = re.compile(
    r"^(?:anything|something|stuff|things|everything)\s+"
    r"(?:mentioning|about|on|regarding|to\s+do\s+with|with|that\s+mentions?)\s+"
)

_PLAIN_WORDS = frozenset({
    "the", "and", "for", "about", "my", "your", "with", "that", "this",
    "from", "what", "note", "notes", "did", "have", "any", "all", "our",
    "are", "was", "were", "has", "had", "been", "into", "onto", "its",
})


def search_topic(text):
    """The topic of a request to find notes, or None. [text] is normalised."""
    match = _SEARCH.match(" ".join(str(text or "").split()))

    if not match:
        return None

    topic = _VAGUE_LEAD.sub("", match.group(1).strip()).strip()

    if not topic or topic in _EMPTY_TOPICS:
        return None

    return topic


def search(topic, limit=_SEARCH_LIMIT):
    """Notes about [topic], best first, as (stamp, text) pairs."""
    topic = " ".join(str(topic or "").split())

    if not topic:
        return []

    lines = [line for line in _read_lines() if _without_stamp(line)]

    if not lines:
        return []

    bodies = [_without_stamp(line) for line in lines]

    try:
        from actions import semantic_memory

        meaning = _meaning(semantic_memory, topic, bodies)
    except Exception as error:
        print(f"[JARVIS] searching notes by meaning failed, using words: {error}")
        meaning = None

    from actions import semantic_memory

    sharing = semantic_memory.shares_words(topic, bodies, _PLAIN_WORDS)
    scored = []

    for index, body in enumerate(bodies):
        shared = sharing[index]

        if meaning is None:
            score = 1.0 if shared else 0.0
        else:
            score = meaning[index] + (_SHARED_WORD if shared else 0.0)

        if score >= _MINIMUM:
            scored.append((score, index))

    if not scored:
        return []

    scored.sort(reverse=True)
    best = scored[0][0]

    return [
        (_stamp(lines[index]), bodies[index])
        for score, index in scored[:max(1, limit)]
        if score >= best - _CLOSE
    ]


def _meaning(semantic_memory, topic, bodies):
    """How close each note is in meaning to [topic], or None without the model.

    Scored twice, as the bare topic and as "a note about" it, keeping the
    better. Measured on real notes, that lifted the true matches the bare
    topic left just short ("heating", "trains") without lifting unrelated
    ones past the minimum. "my" and "the" are left out first: in a topic of
    one or two words they outweighed the word that mattered.
    """
    core = " ".join(word for word in topic.casefold().split() if word not in _SMALL_WORDS) or topic
    plain = semantic_memory.similarities(core, bodies)

    if plain is None:
        return None

    framed = semantic_memory.similarities(f"a note about {core}", bodies)

    if framed is None:
        return plain

    return [max(left, right) for left, right in zip(plain, framed)]


def _stamp(line):
    """When a note was made, or None for a line without a stamp."""
    match = re.match(r"^\[(\d{4}-\d{2}-\d{2})", line)

    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _spoken_day(day, today=None):
    today = today or date.today()

    if day == today:
        return "today"

    if day == today - timedelta(days=1):
        return "yesterday"

    spoken = f"on {day.day} {day.strftime('%B')}"

    return spoken if day.year == today.year else f"{spoken} {day.year}"


def _yours(topic):
    """The topic as JARVIS says it back: "my car" becomes "your car"."""
    swaps = {"my": "your", "mine": "yours", "me": "you", "i": "you", "myself": "yourself"}

    return " ".join(swaps.get(word, word) for word in topic.split())


def describe_search(topic):
    """Spoken answer to "what did I note about ...?"."""
    if not _read_lines():
        return "You have no notes, sir."

    found = search(topic)
    spoken_topic = _yours(" ".join(str(topic or "").split()))

    if not found:
        return f"I can't find a note about {spoken_topic}, sir."

    def said(text):
        return text.rstrip(" .")

    if len(found) == 1:
        day, text = found[0]

        if day is None:
            return f"You noted: {said(text)}."

        when = _spoken_day(day)

        return f"{when[0].upper()}{when[1:]} you noted: {said(text)}."

    parts = [
        f"{_spoken_day(day)}: {said(text)}" if day else said(text)
        for day, text in found
    ]

    return (
        f"{len(found)} notes about {spoken_topic}, sir. "
        + "; ".join(parts)
        + "."
    )
