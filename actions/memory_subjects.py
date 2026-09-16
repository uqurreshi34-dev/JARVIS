"""Resolve spoken subjects against what JARVIS already knows, locally.

Three questions are answered here without reaching a language model:

    "what type of thing is BMW"   -> the collection it belongs to
    "BMW fuel economy"            -> only the matching stored fact
    "what were my old gym days"   -> the value archived when it changed

Neither is driven by phrasing. The gate is whether the utterance names a
subject JARVIS actually holds, in a collection or as stored facts, so no
subject, category or domain is written down anywhere in this file.
"""

import re
import threading
from difflib import SequenceMatcher

from actions import memory, memory_collections


_lock = threading.RLock()
_installed = False
_original_answer = None
_original_local_question = None
_original_compound_request = None

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

# Words that mark a question as being about a past value. Ordinary English,
# not domain knowledge, and deliberately NOT in _NOISE: they have to survive
# tokenising long enough to be detected, then are stripped before the key is
# resolved.
_HISTORICAL = frozenset({
    "old", "older", "oldest", "previous", "previously", "prior", "former",
    "formerly", "earlier", "before", "past", "used",
})

# A history answer replaces a keyed fact, so only a confident key match
# should claim the question.
_MIN_HISTORY_SCORE = 0.80

# Speech recognition mishears a key as often as not -- "gym days" arrives as
# "jim days". Token matching cannot bridge that, so fall back to character
# similarity. The runner-up margin matters more than the floor: a genuine
# mishearing beats every other key by a wide gap, while a question that is
# simply about something else scores low against all of them.
_MIN_SIMILARITY = 0.70
_MIN_MARGIN = 0.20

# A single misheard word is repaired against the stored vocabulary before
# anything else runs. This can be looser than whole-label matching because
# the margin check does the real work: a mishearing beats its neighbours
# clearly, while an ordinary unrelated word does not.
_MIN_WORD_SIMILARITY = 0.62

# A subject needs at least this share of its tokens present for a partial
# match such as "BMW" against "BMW 3 Series" to count.
_MIN_SUBJECT_TOKENS = 1

# An attribute answer replaces one the language model would have given, so
# a weak match must decline rather than recite the nearest stored fact.
#
# Measured, not guessed. tools/probe_attribute_scores.py encoded eleven
# questions against real stored facts: the weakest question that SHOULD be
# answered scored 0.293, the strongest that should be DECLINED scored
# 0.245. This sits between them. Note how wrong the old numbers were --
# 0.55 was above every correct answer, and semantic_memory's shared 0.38
# retrieval floor was above both groups.
#
# The band is only 0.048 wide, so re-run the probe after adding subjects.
_MIN_ATTRIBUTE_SCORE = 0.27


def _tokens(text):
    return re.findall(r"[a-z0-9]+", str(text or "").casefold())


def _identity(text):
    return "".join(_tokens(text))


def _content_tokens(text):
    # Tokens under three characters carry no meaning and match everything:
    # "what's" yields "s", and the lexical scorer's prefix match then hits
    # series, saloon and systems alike.
    #
    # A short token that carries a digit is the exception. "a4", "x5" and
    # the "3" in "3 series" are what tell one model from its sibling, and
    # dropping them made "audi a4" and "audi a6" the same subject. The
    # "s" from "what's" has no digit, so it is still dropped.
    return [
        token
        for token in _tokens(text)
        if token not in _NOISE
        and (len(token) >= 3 or any(ch.isdigit() for ch in token))
    ]


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
    index = {}

    # subjects.txt is where learned facts live now. memory.txt is still
    # read afterwards, because a fact typed there by hand should work the
    # same way, and because this has to keep answering during the move.
    try:
        from actions import subject_store

        for label, stored in subject_store.facts().items():
            grouped[label] = list(stored)
            index[_identity(label)] = label

    except Exception:
        pass

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

        # The same subject under a different spelling is the same subject.
        label = index.setdefault(_identity(subject), subject)

        if fact not in grouped.setdefault(label, []):
            grouped[label].append(fact)

    return grouped


def _keyed_labels():
    """Memory keys, which are the only things that carry a history.

    Collections keep no archive: their items have created_at and updated_at
    but nothing is retained when a value is replaced. So a question about a
    past value can only ever be answered from the keyed fact's archive,
    even when a collection happens to share the name.
    """
    labels = []

    try:
        for key, _value in memory.facts():
            if key:
                labels.append(str(key))
    except Exception:
        return []

    return labels


def _plural_verb(value):
    """"were" for a list, "was" for a single value."""
    lowered = str(value or "").casefold()

    if "," in lowered or re.search(r"\band\b", lowered):
        return "were"

    return "was"


def _known_tokens():
    """Every word that appears in something JARVIS has stored."""
    words = set()

    for collection_key in _collections():
        words.update(_tokens(collection_key))

        for item in memory_collections.items(collection_key):
            words.update(_tokens(item))

    for subject in _subject_facts():
        words.update(_tokens(subject))

    for label in _keyed_labels():
        words.update(_tokens(label))

    return words


def correct(text):
    """Repair misheard words against the vocabulary JARVIS actually holds.

    Speech recognition returns "jim days" for "gym days" and "bmv" for
    "bmw". Correcting once, up front, means every route below sees the same
    repaired utterance rather than each having to cope with the mishearing
    separately.

    Only words that are close to something stored are touched, and only
    when one candidate clearly beats the rest. Everything else is left
    exactly as spoken.
    """
    known = _known_tokens()

    if not known:
        return text

    repaired = []
    changed = False

    for token in _tokens(text):
        if token in known or token in _NOISE or len(token) < 3:
            repaired.append(token)
            continue

        scored = sorted(
            (
                (SequenceMatcher(None, token, word).ratio(), word)
                for word in known
            ),
            reverse=True,
        )

        score, word = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0

        if score >= _MIN_WORD_SIMILARITY and score - runner_up >= _MIN_MARGIN:
            repaired.append(word)
            changed = True
            continue

        repaired.append(token)

    if not changed:
        return text

    return " ".join(repaired)


def _remaining_tokens(text, label):
    """Spoken tokens not accounted for by the resolved label.

    A token may have been misheard -- "toyoda" resolved to "toyota" -- so
    comparing against the label by equality alone would count the subject
    itself as an unanswered attribute. Anything close enough to a label
    token counts as consumed.
    """
    label_tokens = _tokens(label)
    remaining = []

    for token in _content_tokens(text):
        if token in label_tokens:
            continue

        if any(
            SequenceMatcher(None, token, other).ratio() >= _MIN_SIMILARITY
            for other in label_tokens
        ):
            continue

        remaining.append(token)

    return remaining


def _closest_label(spoken, entries):
    """Match a misheard name against stored labels, or None.

    Speech recognition mangles names constantly -- "gym days" arrives as
    "jim days", "toyota" as "toyoda". Token matching cannot bridge that, so
    fall back to character similarity over whatever happens to be stored.

    Requires a floor AND a clear margin over the runner-up. A misheard name
    beats every other label by a wide gap, while an utterance that is simply
    about something else scores low against all of them, so it declines
    rather than grabbing the nearest.

    `entries` is a sequence of (label, collection_key or None).
    """
    if not spoken or not entries:
        return None

    scored = sorted(
        (
            (
                SequenceMatcher(
                    None,
                    spoken,
                    str(label).casefold(),
                ).ratio(),
                str(label),
                collection_key,
            )
            for label, collection_key in entries
        ),
        key=lambda item: item[0],
        reverse=True,
    )

    score, label, collection_key = scored[0]

    if score < _MIN_SIMILARITY:
        return None

    if len(scored) > 1 and score - scored[1][0] < _MIN_MARGIN:
        return None

    return score, label, collection_key


def history_answer(text):
    """Answer "what were my old X" from the archive, or None."""
    spoken = _tokens(text)

    if not _HISTORICAL.intersection(spoken):
        return None

    query = [
        token
        for token in _content_tokens(text)
        if token not in _HISTORICAL
    ]

    if not query:
        return None

    labels = _keyed_labels()
    best = None

    for label in labels:
        score = _subject_matches(query, label)

        if score >= _MIN_HISTORY_SCORE and (best is None or score > best[0]):
            best = (score, label)

    if best is None:
        closest = _closest_label(
            " ".join(query),
            [(label, None) for label in labels],
        )

        if closest:
            best = (closest[0], closest[1])

    if not best:
        return None

    _score, label = best

    try:
        from actions import memory_history

        data = memory_history._load() or {}
    except Exception:
        return None

    archived = [
        record
        for record in data.get("history", [])
        if isinstance(record, dict)
        and _identity(record.get("key")) == _identity(label)
        and str(record.get("value") or "").strip()
    ]

    if not archived:
        return None

    # One step back only. "the one before that" is not how anyone speaks,
    # and guessing at ordinals is worse than letting the model try.
    previous = max(
        archived,
        key=lambda record: str(record.get("archived_at") or ""),
    )

    value = str(previous.get("value")).strip()

    return f"Your previous {label} {_plural_verb(value)} {value}, sir."


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

    # Neither whole direction fits when a stored label is longer than the
    # spoken name AND the utterance carries an attribute too: "bmw fuel
    # economy" against "bmw 3 series" shares only the leading word. Match
    # on a leading run of the label, which is how people shorten names.
    leading = 0

    for token in label_tokens:
        if any(spoken.startswith(token) for spoken in query_tokens):
            leading += 1
            continue

        break

    if leading and len(label_tokens[0]) >= 3:
        return 0.60 + 0.04 * leading

    return 0.0


# The verbs JARVIS already answers to, learned from his own phrase table
# at startup rather than written down here. "open the toyota folder" names
# a stored subject too, and a list of verbs kept in this file would go
# stale the moment a command is added. None means the table could not be
# read, in which case the subject gate below stays off rather than
# guessing.
# Always a set, never None: an empty one already means "not learned", so
# a second way of saying it only bought a None check at every use site.
_ACTION_WORDS = frozenset()


def _learn_action_words(commands):
    """The opening word of every phrase JARVIS already recognises."""
    table = getattr(commands, "_FAST_PHRASES", None)

    if not table:
        return frozenset()

    words = set()

    try:
        for phrases, _intent in table:
            for phrase in phrases:
                # The first word that carries meaning, not simply the first
                # word. 191 of the 510 phrases open with "what", "how" or
                # "the", so position zero taught the guard nothing at all
                # for those: "whats the volume" contributed no word, and
                # "volume" stayed invisible. Taking the first content word
                # instead raises the guard from 102 words to 155 while
                # blocking no more questions than before.
                content = [
                    token
                    for token in _tokens(phrase)
                    if len(token) > 2 and token not in _NOISE
                ]

                if content:
                    words.add(content[0])

    except (TypeError, ValueError):
        return frozenset()

    return frozenset(words)


def _states_rather_than_asks(text):
    """True when an utterance tells JARVIS something instead of asking.

    "i like the audi a4" names a stored subject without being a question
    about it: it is a new preference, and the collection layer learns it.
    The mark separating the two is a first-person opening, which is closed
    grammar -- English has a handful of first-person forms and the set
    never grows, unlike subject names or command verbs, which is exactly
    why both of those are derived at runtime instead of listed.

    routing_guard already owns that definition, so this borrows it rather
    than keeping a second copy here to drift out of step.
    """
    if str(text or "").strip().endswith("?"):
        return False

    tokens = _tokens(text)

    if not tokens:
        return False

    try:
        from actions import routing_guard

        personal = routing_guard._MEMORY_PERSONAL_WORDS
    except Exception:
        return False

    return tokens[0] in personal


def _collection_words():
    """Every word naming one of the user's own collections, singular too.

    Read from their data, not listed here: add a collection tomorrow and
    this covers it with no edit.
    """
    words = set()

    try:
        data = memory_collections._ensure_data()
        keys = list((data or {}).get("collections") or {})
    except Exception:
        return words

    for key in keys:
        for token in _tokens(key):
            words.add(token)

            # "add it to my car" means the cars collection.
            if (
                len(token) > 3
                and token.endswith("s")
                and not token.endswith("ss")
            ):
                words.add(token[:-1])

    return words


def _subject_route_may_claim(text):
    """Whether the subject route may claim an utterance at all.

    One gate, checked before every branch. The guards used to sit inside
    names_known_subject -- the last of three checks -- while local_answer
    ran first and claimed anything it could produce words for. That is how
    "add audi a3 to my cars" came back as a description of the A4 instead
    of being stored.

    Three ways an utterance is not a question about a subject. None of
    them is a list of phrasings:

      - it opens in the first person, so it states something about the
        user rather than asking about a subject
      - it names one of the user's own collections, so the collection
        layer owns it, whatever else it mentions
      - what remains after removing the subject holds a verb JARVIS
        already answers to, so it is a command
    """
    if _states_rather_than_asks(text):
        return False

    if _collection_words() & set(_tokens(text)):
        return False

    # A comparison asks, however it opens. "show me the difference between
    # the A4 and the A6" begins with a command verb, and the guard below
    # would read the whole thing as a command.
    if asks_to_compare(text):
        return True

    if not _ACTION_WORDS:
        return True

    # Judge the opening word, not every word. _learn_action_words takes the
    # first content word of each command phrase, so the symmetric test is
    # the first content word of the utterance: an imperative puts its verb
    # there. "system" and "top" really are command words -- they open
    # "system status" and "top stories" -- but in "toyota production
    # system" and "bmw top speed" they are plainly nouns, and matching them
    # anywhere in the sentence rejected both questions.
    opening = [
        token
        for token in _tokens(text)
        if len(token) > 2 and token not in _NOISE
    ]

    return not (opening and opening[0] in _ACTION_WORDS)


def names_known_subject(text):
    """True when an utterance asks something about a subject JARVIS holds.

    Deliberately not "can I answer this". The existing gate routes on
    whether a stored fact clears the confidence floor, so everything else
    goes to the classifier -- which answers "that's beyond me for now"
    when the model would have answered perfectly well. Asking about the
    history of a car whose facts mention 1975 is a real question about a
    real subject, and it should reach the model rather than a shrug.

    Whether the stored facts actually answer it is decided later, in
    attribute_answer, which still declines and falls through.
    """
    if not _ACTION_WORDS:
        return False

    # A statement about the user is not a question about a subject, even
    # when it names one. This route exists so questions reach the model
    # instead of the classifier's shrug; claiming statements too stops the
    # classifier ever seeing a new preference to learn.
    if _states_rather_than_asks(text):
        return False

    corrected = correct(text)
    resolved = resolve(corrected)

    if not resolved:
        return False

    label = resolved[0]
    identity = _identity(label)

    # Stored facts, not merely membership of a collection. A bare
    # collection item has nothing to answer an attribute question with,
    # and category questions are already handled by category_answer.
    if not any(
        _identity(subject) == identity
        for subject in _subject_facts()
    ):
        return False

    attribute = _remaining_tokens(corrected, label)

    if not attribute:
        return False

    # A command that happens to name a subject is still a command.
    if _ACTION_WORDS & set(attribute):
        return False

    return True


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
        # Nothing matched on tokens, so the name was probably misheard.
        entries = [
            (item, collection_key)
            for collection_key in _collections()
            for item in memory_collections.items(collection_key)
        ] + [(subject, None) for subject in _subject_facts()]

        closest = _closest_label(" ".join(query_tokens), entries)

        if not closest:
            return None

        score, label, collection_key = closest

        return label, collection_key, score

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
        entries = [
            (item, collection_key)
            for collection_key in _collections()
            for item in memory_collections.items(collection_key)
        ]

        closest = _closest_label(" ".join(query_tokens), entries)

        if not closest:
            return None

        score, label, collection_key = closest

        return label, collection_key, score

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
    if _remaining_tokens(text, label):
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

        vectors = semantic_memory._encode([attribute] + list(facts))

        if vectors is None or len(vectors) < 2:
            return None

        query_vector = vectors[0]
        scores = [float(vector @ query_vector) for vector in vectors[1:]]
    except Exception:
        return None

    best = max(scores)

    # Relative, not absolute. A short query against a long sentence scores
    # low in absolute terms even when it is the right sentence -- which is
    # why the shared 0.38 retrieval floor rejected every BMW fact.
    if best < _MIN_ATTRIBUTE_SCORE:
        return None

    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)

    chosen = [
        facts[i]
        for i in ranked
        if scores[i] >= best - 0.06
    ][:2]

    return chosen or None


# Words that ask for two things to be weighed against each other, and the
# words that join the two. Language, not data: nothing here names a
# subject, so it stays true whatever ends up in subjects.txt.
_COMPARISON_CUES = frozenset({
    "compare", "compared", "comparison", "difference", "differences",
    "differ", "versus", "vs", "better", "worse", "between",
})

# How English joins the two sides of a comparison. Closed grammar, like
# the cues above: "better THAN the mercedes", "the a4 OR the a6", "bmw
# VERSUS mercedes".
_COMPARISON_JOINERS = (
    "versus", "vs", "against", "than", "with", "and", "or", "to",
)

# How many paired facts a spoken comparison should carry. Beyond this it
# stops being an answer and becomes a recital.
_MAX_COMPARISON_PAIRS = 3


def _stored_facts_for(label):
    """The stored facts for a resolved label, matched on identity."""
    identity = _identity(label)

    for subject, facts in _subject_facts().items():
        if _identity(subject) == identity:
            return facts

    return []


def _resolve_with_facts(text, labels=None):
    """The stored subject a piece of a comparison names, or None.

    resolve() ranks collection items first, which is right for "what type
    of thing is BMW" and wrong here. When "BMW" is a bare collection item
    and the facts sit under "BMW 3 Series", resolve() picks the item, the
    item has nothing to compare, and the whole question went to the model.
    A comparison can only ever use subjects with facts, so only those are
    considered.
    """
    query_tokens = _content_tokens(text)

    if not query_tokens:
        return None

    if labels is None:
        labels = list(_subject_facts())

    candidates = []

    for label in labels:
        score = _subject_matches(query_tokens, label)

        if score:
            candidates.append((score, label))

    if not candidates:
        closest = _closest_label(
            " ".join(query_tokens),
            [(label, None) for label in labels],
        )

        return closest[1] if closest else None

    candidates.sort(key=lambda item: (item[0], -len(item[1])), reverse=True)

    return candidates[0][1]


def _comparison_pair(text):
    """The two stored subjects an utterance asks to compare, or None."""
    tokens = _tokens(text)

    if not (_COMPARISON_CUES & set(tokens)):
        return None

    labels = list(_subject_facts())

    for index, token in enumerate(tokens):
        if token not in _COMPARISON_JOINERS:
            continue

        left = _resolve_with_facts(" ".join(tokens[:index]), labels)
        right = _resolve_with_facts(" ".join(tokens[index + 1:]), labels)

        if not left or not right:
            continue

        if _identity(left) == _identity(right):
            continue

        return left, right

    return None


# Most subjects a spoken comparison may name before it stops being
# something to read aloud.
_MAX_COMPARISON_SUBJECTS = 5


def _comparison_segments(text):
    """The utterance cut at commas and joining words, as text pieces.

    Commas matter here and _tokens() discards them, so this keeps them as
    their own marks. Pieces made only of question words and comparison
    cues ("which is better", "compare") carry no subject and are dropped.
    """
    marks = re.findall(r"[a-z0-9]+|,", str(text or "").casefold())
    segments = []
    current = []

    for mark in marks:
        if mark == "," or mark in _COMPARISON_JOINERS:
            if current:
                segments.append(current)
            current = []
            continue

        current.append(mark)

    if current:
        segments.append(current)

    return [
        " ".join(segment)
        for segment in segments
        if set(_content_tokens(" ".join(segment))) - _COMPARISON_CUES
    ]


def asks_to_compare(text):
    """True when an utterance asks for two or more things to be compared.

    Deliberately independent of whether those things are held locally.
    That distinction is the whole point: "compare the A4 and the A5" with
    only the A4 stored is still a comparison, and answering it from the
    A4's facts alone reads like an answer while silently dropping half of
    what was asked.
    """
    if not (_COMPARISON_CUES & set(_tokens(text))):
        return False

    return len(_comparison_segments(text)) >= 2


def _comparison_subjects(text):
    """Every stored subject a comparison names, or None.

    Only claims comparisons of three or more. Two subjects stay with
    _comparison_pair, which tries each joining word in turn and so copes
    with a name that itself contains "and".

    All or nothing: if any named piece does not resolve, or two pieces
    resolve to the same subject, this declines. Quietly dropping one of
    three cars would read like an answer while leaving out what was asked.
    """
    if not (_COMPARISON_CUES & set(_tokens(text))):
        return None

    segments = _comparison_segments(text)

    if len(segments) < 3 or len(segments) > _MAX_COMPARISON_SUBJECTS:
        return None

    labels = list(_subject_facts())
    subjects = []
    seen = set()

    for segment in segments:
        label = _resolve_with_facts(segment, labels)

        if not label:
            return None

        identity = _identity(label)

        if identity in seen:
            return None

        seen.add(identity)
        subjects.append(label)

    return subjects


def _names_several(text):
    """True when an utterance names three or more things to compare."""
    if not (_COMPARISON_CUES & set(_tokens(text))):
        return False

    return len(_comparison_segments(text)) >= 3


def _compared_subjects(text):
    """Every stored subject a comparison names, or None. No encoder."""
    several = _comparison_subjects(text)

    if several:
        return several

    # Three or more were named but not all are held. The pair finder would
    # happily compare two of them and drop the rest, so decline instead.
    if _names_several(text):
        return None

    pair = _comparison_pair(text)

    return list(pair) if pair else None


def _mutual_best(left, right):
    """Pair each fact with its counterpart, where both sides agree.

    Mutual nearest neighbours: a is paired with b only when b is a's
    closest match and a is b's closest. That needs no threshold, which
    matters because a threshold here could not be measured the way
    _MIN_ATTRIBUTE_SCORE was -- there is no set of right answers to
    probe against, only pairs that either correspond or do not.

    A fact with no counterpart is simply left out. Boot space against
    boot space is a comparison; boot space against founding date is not.
    """
    try:
        import numpy as np

        from actions import semantic_memory

        vectors = semantic_memory._encode(list(left) + list(right))
    except Exception:
        return []

    if vectors is None or len(vectors) != len(left) + len(right):
        return []

    scores = vectors[:len(left)] @ vectors[len(left):].T
    pairs = []

    for row in range(len(left)):
        column = int(np.argmax(scores[row]))

        if int(np.argmax(scores[:, column])) == row:
            pairs.append(
                (float(scores[row][column]), left[row], right[column]))

    pairs.sort(key=lambda item: item[0], reverse=True)

    return [(first, second) for _score, first, second in pairs]


def comparison_answer(text):
    """Set two to five stored subjects side by side, or None.

    Every subject has to be held locally. If one is missing the question
    goes to the model, which is the right outcome: half a comparison built
    from one side's facts and nothing for the other would read like an
    answer while being worthless.

    Once every subject is held, this always answers. Facts are lined up by
    meaning where the encoder can do it, and otherwise each subject's own
    facts are read out in turn.
    """
    subjects = _compared_subjects(text)

    if not subjects:
        return None

    facts = [_stored_facts_for(subject) for subject in subjects]

    if any(not stored for stored in facts):
        return None

    if len(subjects) == 2:
        lines = [
            f"{subjects[0]}: {one} {subjects[1]}: {two}"
            for one, two in _mutual_best(facts[0], facts[1])
        ]
    else:
        lines = _several_lines(subjects, facts)

    if not lines:
        lines = _listed_lines(subjects, facts)

    names = ", ".join(subjects[:-1]) + f" and {subjects[-1]}"

    return (
        f"Comparing {names}, sir. "
        + " ".join(lines[:_MAX_COMPARISON_PAIRS])
    )


def _listed_lines(subjects, facts):
    """Each subject's own facts, when nothing could be lined up.

    Only reached when the encoder is unavailable, or when three or more
    subjects share no attribute at all. Still the stored facts, still no
    model: the listener hears what is held and can compare it themselves.
    """
    per_subject = max(1, _MAX_COMPARISON_PAIRS - 1)

    return [
        f"{subject}: " + " ".join(stored[:per_subject])
        for subject, stored in zip(subjects, facts)
    ]


def _several_lines(subjects, facts):
    """Rows of matching facts across three or more subjects.

    Mutual nearest neighbours only pair two lists, so the first subject's
    facts lead: each one becomes a row, and every other subject contributes
    its own closest fact to that row. A row is kept only when each of those
    facts agrees in return that the lead fact is its closest, the same
    agreement _mutual_best asks for. Rows that some subject cannot answer
    are left out rather than padded, and an empty result falls back to
    _listed_lines in comparison_answer.
    """
    lead = facts[0]
    rows = None

    for other in facts[1:]:
        agreed = dict(_mutual_best(lead, other))

        if rows is None:
            rows = {fact: [agreed[fact]] for fact in lead if fact in agreed}
        else:
            rows = {
                fact: matched + [agreed[fact]]
                for fact, matched in rows.items()
                if fact in agreed
            }

        if not rows:
            return []

    lines = []

    for fact in lead:
        if fact not in rows:
            continue

        parts = [f"{subjects[0]}: {fact}"] + [
            f"{subject}: {matched}"
            for subject, matched in zip(subjects[1:], rows[fact])
        ]
        lines.append(" ".join(parts))

    return lines


# Words that turn a comparison into a sequence of tasks. Any of these and
# the compound planner keeps its say.
_SEQUENCE_WORDS = frozenset({"then", "after", "afterwards", "followed"})

# Conversational words that happen to open a command phrase somewhere in
# the table, so they appear in _ACTION_WORDS, but do not make "show me the
# difference between X and Y" a command.
_CONVERSATIONAL = frozenset({"jarvis", "show", "know", "tell", "now"})


def local_comparison(text):
    """True when an utterance is a comparison answered from stored facts.

    The compound planner is offered anything containing "and", and asks the
    model whether it is a sequence of tasks. "compare X and Y" never is, so
    that call was spent on every comparison before the local answer ran.

    Conservative on purpose. Any sequencing word, or any leftover word that
    opens a known command ("compare audi and open the bmw folder"), leaves
    the planner in charge.
    """
    if not _ACTION_WORDS:
        return False

    corrected = correct(text)

    if _SEQUENCE_WORDS & set(_tokens(corrected)):
        return False

    subjects = _compared_subjects(corrected)

    if not subjects:
        return False

    leftover = set(_content_tokens(corrected)) - _COMPARISON_CUES

    for label in subjects:
        leftover -= set(_tokens(label))

    if (leftover - _CONVERSATIONAL) & _ACTION_WORDS:
        return False

    # Memoised, so the routing gate and the answer that follow reuse this.
    return local_answer(text) is not None


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
    attribute = _remaining_tokens(text, label)

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
    """The full local route: history, comparison, attribute, category.

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

    asked = text

    try:
        # Repair the utterance once so every route below agrees on what
        # was actually said.
        text = correct(text)

        answer = history_answer(text) or comparison_answer(text)

        # A comparison that could not be completed belongs to the model,
        # and nothing else local may have a go at it. attribute_answer
        # would resolve whichever subject IS stored and answer about that
        # one, which is how "compare the A4 and the A5" came back as a
        # description of the A4.
        if answer is None and not asks_to_compare(text):
            answer = attribute_answer(text) or category_answer(text)
    except Exception as error:
        print(f"[JARVIS] local subject answer failed: {error}")
        answer = None

    with _lock:
        # The key is what was asked, not the corrected form. The next
        # caller passes the same raw utterance, so storing the corrected
        # text meant any misheard question missed the memo and ran the
        # encoder twice.
        _last_query = asked
        _last_answer = answer

    return answer


def _wrapped_answer(question):
    local = local_answer(question)

    if local:
        print("[local] subject answer (no API call)")
        return local

    return _original_answer(question)


def _wrapped_compound_request(command):
    """Keep a locally answerable comparison away from the planner."""
    try:
        if local_comparison(command):
            print("[local] comparison, not a compound task (no planner call)")
            return None
    except Exception as error:
        print(f"[JARVIS] comparison check failed: {error}")

    return _original_compound_request(command)


def _wrapped_local_question(command):
    """Route to the local answer when the utterance names a known subject.

    The existing gate requires a first-person word, so "what type of thing
    is BMW" was sent to the interpreter even though the answer was sitting
    in memory. Naming something JARVIS holds is evidence enough.
    """
    # Personal-memory questions first: they are not about subjects and
    # have their own gate.
    if _original_local_question is not None:
        try:
            if _original_local_question(command):
                return True
        except Exception:
            pass

    # Everything below is the subject route, so the gate goes here -- once,
    # ahead of all of it -- rather than inside the last branch.
    try:
        if not _subject_route_may_claim(command):
            return False
    except Exception:
        return False

    try:
        if local_answer(command) is not None:
            return True
    except Exception:
        return False

    # Nothing stored answers it well enough, but it still asks about
    # something JARVIS holds. Route it anyway: the wrapped answer falls
    # through to the model, which beats the classifier's "unknown".
    try:
        return names_known_subject(command)
    except Exception:
        return False


def install_runtime(commands):
    """Install local subject resolution once, after commands is loaded."""
    global _installed, _original_answer, _original_local_question
    global _original_compound_request

    with _lock:
        if _installed:
            return

        # One-time move of learned facts out of memory.txt. Does nothing
        # once it has run, and nothing at all on a fresh install.
        try:
            from actions import subject_store

            subjects, moved = subject_store.migrate_from_memory()

            if subjects:
                print(
                    f"[JARVIS] moved {moved} learned fact(s) about "
                    f"{subjects} subject(s) into subjects.txt"
                )

            # subjects.txt is meant to be edited in Notepad while JARVIS is
            # closed, so the mirror is reconciled at every startup rather
            # than only after a write. Without this, a hand edit stays
            # invisible to memory.json until the next research command.
            subject_store.sync_to_json()

        except Exception as error:
            print(f"[JARVIS] subject migration skipped: {error}")

        global _ACTION_WORDS

        _ACTION_WORDS = _learn_action_words(commands)

        if not _ACTION_WORDS:
            print("[JARVIS] command phrase table unreadable; "
                  "subject routing limited to answerable questions")

        _original_answer = commands.answer
        commands.answer = _wrapped_answer

        # _handle_command looks this up by name at call time, so replacing
        # the module attribute is enough.
        original_compound = getattr(commands, "_compound_request", None)

        if original_compound is not None:
            _original_compound_request = original_compound
            commands._compound_request = _wrapped_compound_request

        try:
            import llm

            _original_local_question = llm._local_memory_question
            llm._local_memory_question = _wrapped_local_question
        except Exception as error:
            print(f"[JARVIS] subject routing unavailable: {error}")

        _installed = True
