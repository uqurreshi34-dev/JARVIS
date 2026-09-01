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

    # Replace only the old combined project classifier. "I'm working on ..."
    # remains an additive activity record, while explicit default/main/current
    # project wording becomes the standing default-project fact.
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

    # Migrate the existing human-readable "project:" entry in place. This is
    # a schema rename, not a new memory or a historical replacement.
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

    # Keep the existing metadata for the renamed record instead of creating a
    # fresh timestamp on every migration. The sync below will then treat it as
    # the same current memory under its new explicit key.
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

        # Exact matches are resolved before fuzzy matching, but keep this
        # guard explicit so the rule remains safe if call order changes.
        if commands._FAST_LOOKUP.get(text) == intent:
            return intent

        # A strong local semantic memory match is more trustworthy than a
        # character-level fuzzy collision with a command such as read_notes.
        # Let the normal memory route answer it instead.
        try:
            if _local_memory_question(text):
                return None
        except Exception:
            pass

        return intent

    return guarded


def _project_inventory_request(text):
    """Recognise a request for the user's Cursor project list locally."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").casefold()))

    if not words & {"project", "projects"}:
        return False

    # These mean the question is about remembered/default work context,
    # not the Cursor project inventory.
    if words & {"working", "default", "main", "current"}:
        return False

    return bool(
        words
        & {
            "have",
            "recent",
            "recently",
            "opened",
            "workspace",
            "workspaces",
            "list",
        }
    )


def _guarded_fast_path(commands, original):
    """Route generic personal-memory statements through the existing remember intent."""
    def guarded(command):
        result = original(command)

        if result is not None:
            return result

        text = commands._normalise(command)

        if _project_inventory_request(text):
            return commands._blank_result("list_projects")

        # Once a collection meaning has been learned, route matching
        # memory statements locally instead of sending them back to the
        # provider. This keeps later collection updates fast and prevents
        # the model from inventing a new synonymous key.
        if not (
            text.endswith("?")
            or text.split(" ", 1)[0] in _MEMORY_QUESTION_WORDS
        ):
            from actions import memory_collection_intelligence

            if memory_collection_intelligence.locally_known(text):
                print(
                    "[fast] remember "
                    "(learned collection; no API call)"
                )

                return commands._blank_result(
                    "remember",
                    text=(command or "").strip(),
                )

        # Do not treat questions as memory writes. For non-question
        # statements, let the existing classifier determine whether this is
        # a known keyed personal fact. This adds no vocabulary and no new
        # intent: it reuses memory.classify() and the existing remember path.
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

        # "my project is ..." is deliberately additive unless the user
        # explicitly says default/main/current. It is stored as a plain
        # memory entry beside the standing default project.
        if re.match(r"^my\s+project\s+is\s+.+$", text, re.I):
            return commands._blank_result(
                "remember",
                text=(command or "").strip(),
            )

        return None

    return guarded


def _guarded_forget(memory_module, original):
    """Try exact deletion first, then a strong local semantic match."""
    def guarded(text):
        removed = original(text)

        if removed:
            return removed

        cleaned = memory_module.safety.clean(text, 200)

        if not cleaned:
            return 0

        candidates = [cleaned]

        # Speech recognition can leave a small connector in the deletion
        # target. Try the meaningful content as well, still entirely locally.
        if cleaned.casefold().startswith(("on ", "about ")):
            candidates.append(cleaned.split(" ", 1)[1].strip())

        try:
            from actions import semantic_memory

            documents = semantic_memory._documents()

            for candidate in candidates:
                matches = semantic_memory._semantic_rank(candidate, documents)

                if not matches:
                    continue

                index, score = matches[0]

                if score < 0.62:
                    continue

                _key, value, display = documents[index]
                target = display if display else value
                removed = original(target)

                if removed:
                    return removed

        except Exception as error:
            print(f"[JARVIS] semantic memory forget failed: {error}")

        return 0

    return guarded


def install(commands):
    """Install routing safeguards once, after commands.py is fully loaded."""
    global _installed

    if _installed:
        return

    import llm
    from actions import memory, memory_history
    from actions import memory_collection_intelligence

    _prepare_default_project_memory()
    memory_history.install()
    memory_collection_intelligence.install_runtime()

    # Keep the original detector's vocabulary available to callers, but make
    # its final confidence decision semantic rather than lexical. This is the
    # same local retrieval path used by the answer and offline fallback.
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

    memory.forget = _guarded_forget(memory, memory.forget)

    _installed = True
