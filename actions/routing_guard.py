"""Small routing safeguards installed by the application composition root.

These guards do not replace the command system. They only tighten two local
ambiguity points that are otherwise difficult to fix safely inside the large
command router: yes/no personal-memory questions and fuzzy matches that are
actually personal-memory questions.
"""

from difflib import SequenceMatcher


_EXTRA_MEMORY_QUESTION_WORDS = frozenset({
    "do", "does", "did", "can", "could", "would", "have", "has",
})

_installed = False


def _content_words(commands, text):
    """Meaningful words for the fuzzy-similarity guard."""
    stopwords = {
        "the", "and", "are", "was", "were", "what", "when", "where",
        "which", "who", "how", "why", "does", "did", "do", "can", "could",
        "would", "should", "have", "has", "had", "that", "this", "these",
        "those", "about", "from", "with", "for", "into", "your", "you",
        "my", "me", "i", "is", "am", "to", "of", "on", "in", "a", "an",
        "tell", "remember", "know", "much", "many", "please", "sir",
    }

    words = []

    for word in commands._normalise(text).split():
        if len(word) > 2 and word not in stopwords:
            words.append(word)

    return words


def _best_phrase(commands, text, intent):
    """Best registered phrase for the fuzzy intent."""
    best_phrase = None
    best_score = 0.0

    for phrase, candidate_intent in commands._FAST_LOOKUP.items():
        if candidate_intent != intent:
            continue

        score = SequenceMatcher(None, text, phrase).ratio()

        if score > best_score:
            best_score = score
            best_phrase = phrase

    return best_phrase, best_score


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


def _local_memory_question(text):
    """Ask llm.py's local memory detector without importing it at module load."""
    try:
        import llm

        return bool(llm._local_memory_question(text))
    except Exception:
        return False


def install(commands):
    """Install routing safeguards once, after commands.py is fully loaded."""
    global _installed

    if _installed:
        return

    import llm

    llm._MEMORY_QUESTION_WORDS = (
        llm._MEMORY_QUESTION_WORDS | _EXTRA_MEMORY_QUESTION_WORDS
    )

    commands._fuzzy_intent = _guarded_fuzzy(
        commands,
        commands._fuzzy_intent,
    )

    _installed = True
