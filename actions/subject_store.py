"""Everything JARVIS has learned about the world, kept apart from you.

memory.txt is a short file about one person: your name, where you live,
which days you go to the gym. Reference facts about cars, books and
languages are a different kind of thing. They arrive in bulk, they are
replaceable, and there is no reason a hundred of them should push your
name off the end of a file.

So they live here, in subjects.txt, with the properties that make
memory.txt worth trusting: plain text, one fact per line, every line
self-contained, editable in Notepad. Delete a line and that fact is
gone; nothing else notices.

    bmw 3 series: Returns around 55 mpg combined on diesel. [learned]
    bmw 3 series: Boot space is 480 litres in the saloon. [you]

The trailing tag records where a fact came from, so when one turns out
to be wrong you know whether to distrust the model or yourself. It is
optional, and a line without one still reads and works fine.

Facts are data, never instructions, exactly as in memory.py.
"""

import os
import re
import threading

from actions import files, safety


FILENAME = "subjects.txt"

# Reference data arrives in bulk, so the ceiling is high. It exists to
# stop a runaway loop filling the disk, not to keep the file short.
MAX_LINES = 5000

# One subject holding four hundred facts is a bug, not a well-researched
# subject.
MAX_FACTS_PER_SUBJECT = 24

# Long facts rank worse: the encoder does better with one idea per
# sentence. learn_subject already applies this; repeating it here holds a
# hand-edited line to the same standard.
MAX_FACT_LENGTH = 180

MAX_SUBJECT_LENGTH = 60

# Where a fact came from. Anything else in trailing brackets is left
# alone and treated as part of the fact.
SOURCES = ("learned", "you")

# A loose memory.txt line only migrates when its name appears this often.
# One "Note to self: buy milk" is a note. Six lines starting "bmw 3
# series:" are a subject.
MIN_LINES_TO_MIGRATE = 2

_lock = threading.RLock()

_LINE = re.compile(r"^\s*([^:]{1,%d}?)\s*:\s*(.+?)\s*$" % MAX_SUBJECT_LENGTH)
_SOURCE = re.compile(r"\s*\[(%s)\]$" % "|".join(SOURCES), re.IGNORECASE)

_cache_stamp = None
_cache_groups = None


def _path():
    base = files.root()

    return os.path.join(base, FILENAME) if base else None


def _key(subject):
    """The lookup form of a name. Case, spacing and punctuation vanish."""
    return " ".join(re.findall(r"[a-z0-9]+", str(subject or "").casefold()))


def _stamp(path):
    """Enough of the file's state to notice a hand edit."""
    try:
        info = os.stat(path)
    except OSError:
        return None

    return (info.st_mtime_ns, info.st_size)


def _invalidate():
    global _cache_stamp, _cache_groups

    with _lock:
        _cache_stamp = None
        _cache_groups = None


def _read_lines():
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
        print(f"[JARVIS] could not read subjects: {error}")
        return []


def _split_source(fact):
    """Separate a trailing [learned] or [you] tag from the fact itself."""
    match = _SOURCE.search(fact)

    if not match:
        return fact, None

    return fact[:match.start()].rstrip(), match.group(1).casefold()


def _groups():
    """Stored subjects in file order, as key -> {label, facts}.

    Re-parsed only when the file changes on disk, so an edit made in
    Notepad is picked up on the next question without the file being read
    and split for every utterance.
    """
    global _cache_stamp, _cache_groups

    path = _path()
    stamp = _stamp(path) if path else None

    with _lock:
        if (
            stamp is not None
            and stamp == _cache_stamp
            and _cache_groups is not None
        ):
            return _cache_groups

        groups = {}

        for line in _read_lines():
            match = _LINE.match(line)

            if not match:
                continue

            label = match.group(1).strip()
            fact, source = _split_source(match.group(2).strip())
            key = _key(label)

            if not key or not fact:
                continue

            group = groups.setdefault(key, {"label": label, "facts": []})
            group["facts"].append((fact, source))

        _cache_stamp = stamp
        _cache_groups = groups

        return groups


def _copy(groups):
    """A mutable copy, so the parse cache is never edited in place."""
    return {
        key: {"label": group["label"], "facts": list(group["facts"])}
        for key, group in groups.items()
    }


def _trim(groups):
    """Drop whole subjects, oldest first, when the file is over its limit.

    Losing three of a car's six facts is worse than losing the car: the
    remaining three still answer questions, wrongly and confidently. So
    whole subjects go, and what went is named.
    """
    total = sum(len(group["facts"]) for group in groups.values())

    if total <= MAX_LINES:
        return groups

    kept = dict(groups)

    for key in list(kept):
        if total <= MAX_LINES:
            break

        removed = kept.pop(key)
        total -= len(removed["facts"])

        print(
            f"[JARVIS] subjects.txt is over {MAX_LINES} lines; forgetting "
            f"{removed['label']!r} entirely"
        )

    return kept


def _write(groups):
    """Replace the file atomically, so a crash cannot leave it half done."""
    path = _path()

    if not path:
        return False

    lines = []

    for group in groups.values():
        for fact, source in group["facts"]:
            suffix = f" [{source}]" if source in SOURCES else ""
            lines.append(f"{group['label']}: {fact}{suffix}")

    temporary = f"{path}.tmp"

    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(
                "# What JARVIS has learned about the world.\n"
                '# One fact per line, as "subject: fact". Delete any line\n'
                "# that is wrong; nothing else depends on it.\n"
            )

            for line in lines:
                handle.write(f"{line}\n")

        os.replace(temporary, path)

    except OSError as error:
        print(f"[JARVIS] could not write subjects: {error}")

        try:
            os.remove(temporary)
        except OSError:
            pass

        return False

    _invalidate()

    return True


def add(subject, fact, source="learned"):
    """Store one fact about a subject. True when it was stored or already
    present, False when it was refused."""
    label = safety.clean(subject, MAX_SUBJECT_LENGTH)
    fact = safety.clean(fact, MAX_FACT_LENGTH)

    if not label or not fact:
        return False

    # A colon in the name would make the line ambiguous on re-read, and a
    # line that cannot be re-read is worse than no line.
    if ":" in label:
        return False

    if safety.looks_like_instruction(fact):
        print(f"[JARVIS] refusing to store an instruction: {fact[:60]!r}")
        return False

    source = str(source or "").casefold()
    source = source if source in SOURCES else None

    key = _key(label)

    if not key:
        return False

    with _lock:
        groups = _copy(_groups())
        group = groups.get(key)

        if group is None:
            group = {"label": label, "facts": []}
            groups[key] = group

        if any(
            stored.casefold() == fact.casefold()
            for stored, _source in group["facts"]
        ):
            return True

        if len(group["facts"]) >= MAX_FACTS_PER_SUBJECT:
            print(
                f"[JARVIS] {group['label']} already holds "
                f"{MAX_FACTS_PER_SUBJECT} facts; not adding another"
            )
            return False

        group["facts"].append((fact, source))

        return _write(_trim(groups))


def add_many(subject, new_facts, source="learned"):
    """Store several facts about one subject in a single write.

    add() rewrites the whole file, which is right for one fact and wasteful
    for six. learn_subject() arrives with six at a time, and at a few
    thousand lines the difference between one write and six is real.
    Returns how many were stored.
    """
    label = safety.clean(subject, MAX_SUBJECT_LENGTH)

    if not label or ":" in label:
        return 0

    key = _key(label)

    if not key:
        return 0

    with _lock:
        groups = _copy(_groups())
        group = groups.get(key)

        if group is None:
            group = {"label": label, "facts": []}
            groups[key] = group

        tag = str(source or "").casefold()
        tag = tag if tag in SOURCES else None

        known = {stored.casefold() for stored, _source in group["facts"]}
        stored_count = 0

        for fact in new_facts or ():
            fact = safety.clean(fact, MAX_FACT_LENGTH)

            if not fact or fact.casefold() in known:
                continue

            if safety.looks_like_instruction(fact):
                print(
                    f"[JARVIS] refusing to store an instruction: {fact[:60]!r}")
                continue

            if len(group["facts"]) >= MAX_FACTS_PER_SUBJECT:
                print(
                    f"[JARVIS] {group['label']} already holds "
                    f"{MAX_FACTS_PER_SUBJECT} facts; ignoring the rest"
                )
                break

            group["facts"].append((fact, tag))
            known.add(fact.casefold())
            stored_count += 1

        if not stored_count:
            return 0

        return stored_count if _write(_trim(groups)) else 0


def facts():
    """Every subject as {label: [fact, ...]}.

    Deliberately the same shape memory_subjects._subject_facts() returns,
    so resolution can read from here without changing how it thinks.
    """
    return {
        group["label"]: [fact for fact, _source in group["facts"]]
        for group in _groups().values()
    }


def facts_for(subject):
    """Just the fact text for one subject, or an empty list."""
    group = _groups().get(_key(subject))

    return [fact for fact, _source in group["facts"]] if group else []


def entries(subject):
    """(fact, source) pairs for one subject. source may be None."""
    group = _groups().get(_key(subject))

    return list(group["facts"]) if group else []


def label_for(subject):
    """The stored spelling of a subject name, or None."""
    group = _groups().get(_key(subject))

    return group["label"] if group else None


def subjects():
    """Stored subject names, in file order."""
    return [group["label"] for group in _groups().values()]


def forget(subject):
    """Remove a subject entirely. Returns how many facts went."""
    key = _key(subject)

    with _lock:
        groups = _copy(_groups())
        group = groups.pop(key, None)

        if not group:
            return 0

        if not _write(groups):
            return 0

        return len(group["facts"])


def line_count():
    """How many facts are stored across every subject."""
    return sum(len(group["facts"]) for group in _groups().values())


def migrate_from_memory():
    """Move subject facts out of memory.txt. Returns (subjects, facts).

    Only groups of lines move. A single line that merely contains a colon
    -- "Note to self: buy milk" -- is a note, not a subject, and stays
    exactly where it is. Both files are plain text, so a wrong call here
    is repaired by moving one line back by hand.
    """
    from actions import memory, memory_collections

    # memory.txt ends with a mirrored block of collection text, kept there
    # by a wrapper memory_collections installs over memory._write. Without
    # that wrapper in place, rewriting the file here would drop the block.
    # Installing it is idempotent and safe to call from a bare script.
    try:
        memory_collections._install_memory_writer()
    except Exception as error:
        print(f"[JARVIS] collection mirror unavailable: {error}")

    with _lock:
        lines = memory._read()
        candidates = {}

        for index, line in enumerate(lines):
            if memory._is_keyed(line):
                continue

            match = _LINE.match(line)

            if not match:
                continue

            label = match.group(1).strip()
            fact, source = _split_source(match.group(2).strip())
            key = _key(label)

            if not key or not fact or len(fact) > MAX_FACT_LENGTH:
                continue

            candidates.setdefault(key, []).append((index, label, fact, source))

        moving = {
            key: found
            for key, found in candidates.items()
            if len(found) >= MIN_LINES_TO_MIGRATE
        }

        if not moving:
            return 0, 0

        groups = _copy(_groups())
        moved = 0

        for key, found in moving.items():
            group = groups.get(key)

            if group is None:
                group = {"label": found[0][1], "facts": []}
                groups[key] = group

            known = {stored.casefold() for stored, _source in group["facts"]}

            for _index, _label, fact, source in found:
                if fact.casefold() in known:
                    continue

                group["facts"].append((fact, source or "learned"))
                known.add(fact.casefold())
                moved += 1

        if not _write(_trim(groups)):
            return 0, 0

        doomed = {index for found in moving.values() for index, *_ in found}
        memory._write(
            [line for index, line in enumerate(lines) if index not in doomed]
        )

        return len(moving), moved
