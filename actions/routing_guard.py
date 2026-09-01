"""Small routing safeguards installed by the application composition root.

These guards do not replace the command system. They only tighten two local
ambiguity points that are otherwise difficult to fix safely inside the large
command router: yes/no personal-memory questions and fuzzy matches that are
actually personal-memory questions.
"""

import re

_EXTRA_MEMORY_QUESTION_WORDS = frozenset({
    "do", "does", "did", "can", "could", "would", "have", "has",
})

_MEMORY_QUESTION_WORDS = frozenset({
    "what", "whats", "which", "when", "where", "who", "how",
}) | _EXTRA_MEMORY_QUESTION_WORDS

_MEMORY_PERSONAL_WORDS = frozenset({
    "my", "mine", "me", "i", "im", "ive",
})

_BARE_MEMORY_UPDATE = re.compile(
    r"^(?:my\s+new\s+.+?\s+(?:is|are)\s+.+|"
    r"my\s+.+?\s+(?:is|are)\s+now\s+.+|"
    r"my\s+project\s+is\s+.+)$",
    re.I,
)

_DEFAULT_PROJECT_PATTERN = re.compile(
    r"^my\s+(?:default|main|current)\s+project\s+is\s+(.+)$",
    re.I,
)

_WORKING_ON_PATTERN = re.compile(
    r"^i(?:'m|m| am)\s+working on\s+(.+)$",
    re.I,
)

_installed = False


def _prepare_default_project_memory():
    """Make the legacy project fact explicitly mean the default project."""
    from actions import memory, memory_history

    if "default project" not in memory.KNOWN_KEYS:
        memory.KNOWN_KEYS = tuple(memory.KNOWN_KEYS) + ("default project",)

    memory._KEYED_PATTERNS = tuple(
        pattern
        for pattern in memory._KEYED_PATTERNS
        if pattern[1] != "project"
    )
    memory._KEYED_PATTERNS = (
        (_DEFAULT_PROJECT_PATTERN, "default project"),
        (_WORKING_ON_PATTERN, "project"),
        *memory._KEYED_PATTERNS,
    )

    with memory._lock:
        existing = memory._read()
        has_default = any(
            (match := memory._LINE.match(line))
            and match.group(1).strip().casefold() == "default project"
            for line in existing
        )

        changed = False
        kept = []

        for line in existing:
            match = memory._LINE.match(line)

            if match and match.group(1).strip().casefold() == "project":
                if has_default:
                    changed = True
                    continue

                line = f"default project: {match.group(2).strip()}"
                has_default = True
                changed = True

            kept.append(line)

        if changed:
            memory._write(kept)

    data = memory_history._load()

    if data is not None:
        has_default = any(
            (record.get("key") or "").casefold() == "default project"
            for record in data.get("memories", [])
        )
        changed = False
        kept = []

        for record in data.get("memories", []):
            key = (record.get("key") or "").casefold()

            if key == "project":
                if has_default:
                    changed = True
                    continue

                record["key"] = "default project"
                has_default = True
                changed = True

            kept.append(record)

        if changed:
            data["memories"] = kept
            memory_history._save(data)


def _local_memory_question(text):
    """Use semantic retrieval itself as the confidence check.

    Once the local memory engine has returned a relevant memory, there is no
    reason to demand literal word overlap here as well. That second lexical
    gate defeats the point of semantic retrieval for paraphrases such as
    "do I have any projects?" versus a stored "project" fact.
    """
    text = (text or "").strip().casefold()

    if not text:
        return False

    words = re.findall(r"[a-z0-9]+", text)

    if not words:
        return False

    question_like = (
        words[0] in _MEMORY_QUESTION_WORDS
        or "?" in text
    )

    if not question_like:
        return False

    if not _MEMORY_PERSONAL_WORDS.intersection(words):
        return False

    from actions import memory

    try:
        return bool(memory.relevant_summary(text, limit=1))
    except Exception:
        return False


def _guarded_fuzzy(commands, original):
    """Wrap the old fuzzy matcher without changing its useful corrections."""
    def guarded(text):
        intent = original(text)

        if not intent:
            return None

        if commands._FAST_LOOKUP.get(text) == intent:
            return intent

        try:
            if _local_memory_question(text):
                return None
        except Exception:
            pass

        return intent

    return guarded


def _guarded_fast_path(commands, original):
    """Route generic personal-memory statements through the existing remember intent."""
    def guarded(command):
        result = original(command)

        if result is not None:
            return result

        text = commands._normalise(command)

        if text.endswith("?") or text.split(" ", 1)[0] in _MEMORY_QUESTION_WORDS:
            return None

        from actions import memory

        try:
            keyed = memory.classify(text)
        except Exception:
            keyed = None

        if keyed:
            return commands._blank_result(
                "remember",
                text=(command or "").strip(),
            )

        if re.match(r"^my\s+project\s+is\s+.+$", text, re.I):
            return commands._blank_result(
                "remember",
                text=(command or "").strip(),
            )

        return None

    return guarded


def install(commands):
    """Install routing safeguards once, after commands.py is fully loaded."""
    global _installed

    if _installed:
        return

    import llm
    from actions import memory_history

    _prepare_default_project_memory()
    memory_history.install()

    llm._MEMORY_QUESTION_WORDS = _MEMORY_QUESTION_WORDS
    llm._local_memory_question = _local_memory_question

    commands._fast_path = _guarded_fast_path(
        commands,
        commands._fast_path,
    )

    commands._fuzzy_intent = _guarded_fuzzy(
        commands,
        commands._fuzzy_intent,
    )

    _installed = True
