"""Resolve spoken subjects against what JARVIS already knows, locally.

Two questions are answered here without reaching a language model:

    "what type of thing is BMW"   -> the collection it belongs to
    "BMW fuel economy"            -> only the matching stored fact

Neither is driven by phrasing. The gate is whether the utterance names a
subject JARVIS actually holds, in a collection or as stored facts, so no
subject, category or domain is written down anywhere in this file.
"""

import re
import threading

from actions import memory, memory_collections


_lock = threading.RLock()
_installed = False
_original_answer = None
_original_local_question = None

# Words that never identify a subject. Question words, pronouns, and the
# handful of verbs and prepositions that carry a category question.
_NOISE = frozenset({
    "a", "about", "an", "and", "any", "are", "as", "at", "belong",
    "belongs", "by", "can", "category", "class", "did", "do", "does",
    "for", "from", "group", "has", "have", "how", "i", "im", "in", "into",
    "is", "it", "its", "ive", "kind", "me", "mine", "my", "of", "on",
    "one", "or", "part", "sort", "thing", "to", "tell", "type", "was",
    "were", "what", "whats", "when", "where", "which", "who", "with",
    "would", "you", "your",
    # Ordinary English filler. Without these, an attribute such as "the top
    # speed" matches every stored fact on the word "the" alone.
    "be", "been", "being", "but", "give", "had", "if", "many", "much",
    "not", "so", "some", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "us", "we", "will",
})

# A subject needs at least this share of its tokens present for a partial
# match such as "BMW" against "BMW 3 Series" to count.
_MIN_SUBJECT_TOKENS = 1

# An attribute answer replaces one the language model would have given, so
# a weak match must decline rather than recite the nearest stored fact.
# semantic_memory's own floor of 0.38 is a retrieval threshold, not enough
# confidence to answer instead of the model.
_MIN_ATTRIBUTE_SCORE = 0.55


def _tokens(text):
    return re.findall(r"[a-z0-9]+", str(text or "").casefold())


def _identity(text):
    return "".join(_tokens(text))


def _content_tokens(text):
    return [token for token in _tokens(text) if token not in _NOISE]


def _collections():
    try:
        data = memory_collections._ensure_data()
    except Exception:
        return {}

    collections = data.get("collections")

    return collections if isinstance(collections, dict) else {}


def _subject_facts():
    """Unkeyed memory lines of the form "Subject: fact", grouped by subject.

    learn_subject() stores facts this way, and facts() reports them as
    unkeyed sentences because the subject is not one of KNOWN_KEYS. That
    makes the subject invisible to keyed retrieval, which is why asking
    about one attribute returns every fact about it.
    """
    grouped = {}

    try:
        entries = memory.facts()
    except Exception:
        return grouped

    for key, value in entries:
        if key is not None:
            continue

        subject, separator, fact = str(value or "").partition(":")

        if not separator:
            continue

        subject = subject.strip()
        fact = fact.strip()

        if not subject or not fact:
            continue

        grouped.setdefault(subject, []).append(fact)

    return grouped


def _subject_matches(query_tokens, label):
    """Score how well the spoken tokens identify this stored label."""
    label_tokens = _tokens(label)

    if not query_tokens or not label_tokens:
        return 0.0

    if query_tokens == label_tokens:
        return 1.0

    # "BMW" should reach "BMW 3 Series", so every spoken token must appear
    # in the label, in order, but the label may carry extra words.
    position = 0
    matched = 0

    for token in query_tokens:
        while position < len(label_tokens):
            if label_tokens[position].startswith(token):
                matched += 1
                position += 1
                break

            position += 1

    if matched >= max(_MIN_SUBJECT_TOKENS, len(query_tokens)):
        # A shorter label is a tighter fit for the same spoken tokens.
        return 0.9 - min(0.3, 0.02 * len(label_tokens))

    # The other direction: "BMW fuel economy" carries the subject plus the
    # attribute, so the label is contained in the utterance rather than the
    # other way round. A longer label matching this way is more specific.
    if all(
        any(spoken.startswith(token) for spoken in query_tokens)
        for token in label_tokens
    ):
        return 0.80 + min(0.08, 0.02 * len(label_tokens))

    return 0.0


def resolve(text):
    """Find the stored subject an utterance names, or None.

    Returns (label, collection_key or None, score).
    """
    query_tokens = _content_tokens(text)

    if not query_tokens:
        return None

    candidates = []

    for collection_key, _items in _collections().items():
        for item in memory_collections.items(collection_key):
            score = _subject_matches(query_tokens, item)

            if score:
                candidates.append((score, str(item), collection_key))

    for subject in _subject_facts():
        score = _subject_matches(query_tokens, subject)

        if score:
            candidates.append((score - 0.05, subject, None))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], -len(item[1])), reverse=True)

    score, label, collection_key = candidates[0]

    return label, collection_key, score


def resolve_in_collections(text):
    """Find the collection item an utterance names, ignoring loose facts."""
    query_tokens = _content_tokens(text)

    if not query_tokens:
        return None

    candidates = []

    for collection_key in _collections():
        for item in memory_collections.items(collection_key):
            score = _subject_matches(query_tokens, item)

            if score:
                candidates.append((score, str(item), collection_key))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], -len(item[1])), reverse=True)

    return candidates[0][1], candidates[0][2], candidates[0][0]


def category_answer(text):
    """Say which collection a named subject belongs to, or None."""
    # A collection item answers a category question better than a loose
    # subject label does, even when the label matches more exactly.
    resolved = resolve_in_collections(text) or resolve(text)

    if not resolved:
        return None

    label, collection_key, _score = resolved

    # Only a category question, not any question that happens to name a
    # known subject. "what type of thing is BMW" leaves nothing over once
    # the subject and the question words are removed; "what is the BMW top
    # speed" leaves "top speed", which is a request this cannot answer.
    subject_tokens = set(_tokens(label))
    leftover = [
        token
        for token in _content_tokens(text)
        if token not in subject_tokens
    ]

    if leftover:
        return None

    if collection_key:
        return f"{label} is one of your {collection_key}, sir."

    # Known as a subject with stored facts, but not filed in a collection.
    return (
        f"I know about {label}, sir, but it isn't in any of your "
        "collections."
    )


def _rank_semantically(attribute, facts):
    """Rank a subject's facts by meaning, or None when unavailable.

    Word overlap cannot connect "fuel economy" to "55 mpg combined". The
    local ONNX encoder already loaded for memory retrieval can, and it
    costs no API call.
    """
    try:
        from actions import semantic_memory

        documents = [(None, fact, fact) for fact in facts]
        matches = semantic_memory._semantic_rank(attribute, documents)
    except Exception:
        return None

    if not matches:
        return None

    best = matches[0][1]

    if best < _MIN_ATTRIBUTE_SCORE:
        # Nothing stored really answers this. Let the model have it.
        return None

    # Keep only what matches the attribute nearly as well as the best, so
    # asking about fuel economy does not recite the boot space too.
    chosen = [
        documents[index][1]
        for index, score in matches
        if score >= best - 0.06
    ][:2]

    return chosen or None


def attribute_answer(text):
    """Answer a question about one attribute of a known subject, or None."""
    resolved = resolve(text)

    if not resolved:
        return None

    label, _collection_key, _score = resolved
    grouped = _subject_facts()

    facts = None
    identity = _identity(label)

    for subject, stored in grouped.items():
        if _identity(subject) == identity:
            facts = stored
            break

    if not facts:
        return None

    # Whatever is left after the subject is the attribute being asked about.
    subject_tokens = set(_tokens(label))
    attribute = [
        token
        for token in _content_tokens(text)
        if token not in subject_tokens
    ]

    if not attribute:
        return None

    chosen = _rank_semantically(" ".join(attribute), facts)

    if chosen is not None:
        return f"{label}: " + " ".join(chosen)

    scored = []

    for fact in facts:
        fact_tokens = set(_tokens(fact))
        hits = sum(
            1
            for token in attribute
            if token in fact_tokens
            or any(word.startswith(token) for word in fact_tokens)
        )

        if hits:
            scored.append((hits, fact))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][0]

    # Only report facts that match the attribute as well as the best one
    # does, so asking about fuel economy does not recite the boot space.
    chosen = [fact for hits, fact in scored if hits == best][:2]

    return f"{label}: " + " ".join(chosen)


_last_query = None
_last_answer = None


def local_answer(text):
    """The full local route: attribute first, then category.

    Memoised on the last utterance because the routing gate and the answer
    itself both ask, and each call reads every collection and runs the
    encoder. Computing it twice per question is pure waste.
    """
    global _last_query, _last_answer

    if not str(text or "").strip():
        return None

    with _lock:
        if _last_query == text:
            return _last_answer

    try:
        answer = attribute_answer(text) or category_answer(text)
    except Exception as error:
        print(f"[JARVIS] local subject answer failed: {error}")
        answer = None

    with _lock:
        _last_query = text
        _last_answer = answer

    return answer


def _wrapped_answer(question):
    local = local_answer(question)

    if local:
        print("[local] subject answer (no API call)")
        return local

    return _original_answer(question)


def _wrapped_local_question(command):
    """Route to the local answer when the utterance names a known subject.

    The existing gate requires a first-person word, so "what type of thing
    is BMW" was sent to the interpreter even though the answer was sitting
    in memory. Naming something JARVIS holds is evidence enough.
    """
    if _original_local_question is not None:
        try:
            if _original_local_question(command):
                return True
        except Exception:
            pass

    try:
        return local_answer(command) is not None
    except Exception:
        return False


def install_runtime(commands):
    """Install local subject resolution once, after commands is loaded."""
    global _installed, _original_answer, _original_local_question

    with _lock:
        if _installed:
            return

        _original_answer = commands.answer
        commands.answer = _wrapped_answer

        try:
            import llm

            _original_local_question = llm._local_memory_question
            llm._local_memory_question = _wrapped_local_question
        except Exception as error:
            print(f"[JARVIS] subject routing unavailable: {error}")

        _installed = True
