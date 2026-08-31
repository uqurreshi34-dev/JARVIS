"""Small routing safeguards installed by the application composition root.

These guards do not replace the command system. They only tighten two local
ambiguity points that are otherwise difficult to fix safely inside the large
command router: yes/no personal-memory questions and fuzzy matches that are
actually personal-memory questions.
"""

_EXTRA_MEMORY_QUESTION_WORDS = frozenset({
    "do", "does", "did", "can", "could", "would", "have", "has",
})

_MEMORY_QUESTION_WORDS = frozenset({
    "what", "whats", "which", "when", "where", "who", "how",
}) | _EXTRA_MEMORY_QUESTION_WORDS

_MEMORY_PERSONAL_WORDS = frozenset({
    "my", "mine", "me", "i", "im", "ive",
})

_installed = False


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

    import re

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


def install(commands):
    """Install routing safeguards once, after commands.py is fully loaded."""
    global _installed

    if _installed:
        return

    import llm

    # Keep the original detector's vocabulary available to callers, but make
    # its final confidence decision semantic rather than lexical. This is the
    # same local retrieval path used by the answer and offline fallback.
    llm._MEMORY_QUESTION_WORDS = _MEMORY_QUESTION_WORDS
    llm._local_memory_question = _local_memory_question

    commands._fuzzy_intent = _guarded_fuzzy(
        commands,
        commands._fuzzy_intent,
    )

    _installed = True
