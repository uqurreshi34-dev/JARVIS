"""Cut a web page down to the passages that matter, before a model reads it.

Research used to send the first seven thousand characters of every page to
the model. The start of a page is mostly menus, cookie notices and
headlines for other stories, so JARVIS paid to send noise and could miss
the paragraph that actually answered the question further down.

This reads the whole page instead, splits it into short passages, and keeps
only the ones closest to what was asked, up to a fixed budget, in their
original order. Closeness is measured on this PC with the same small
embedding model memory search uses, so choosing costs nothing; if that model
is unavailable, word overlap does the choosing instead. Passages carrying
figures and dates get a slight lift, because those are what a report is
built from. A passage already kept from another page is not sent twice.

Nothing is rewritten: every kept sentence reaches the model exactly as the
page said it, so the evidence is shortened, never paraphrased.
"""

import math
import re


# Roughly a long sentence or two. Long enough to carry a fact with its
# context, short enough that a budget buys several different facts.
PASSAGE_CHARS = 320

# Upper bound on passages scored per page, so an enormous page cannot make
# the local model the slow part of a research run.
MAX_PASSAGES = 240

# How much a passage containing digits is lifted, on a 0 to 1 scale.
FIGURE_LIFT = 0.05

# A passage must score at least this share of the best passage's score to be
# kept. Without it, a generous budget would be topped up with noise.
RELEVANCE_FLOOR = 0.35

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
_WORD = re.compile(r"[a-z0-9]{3,}")


def passages(text, size=PASSAGE_CHARS):
    """Split text into passages of about [size] characters on sentence edges."""
    units = []

    for block in re.split(r"\n\s*\n|\n", str(text or "")):
        block = " ".join(block.split())

        if not block:
            continue

        units.extend(part.strip() for part in _SENTENCE_END.split(block) if part.strip())

    chunks = []
    current = ""

    for unit in units:
        # A single run-on 'sentence' longer than a passage is cut on spaces.
        while len(unit) > size * 2:
            cut = unit.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            pieces = unit[:cut].strip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(pieces)
            unit = unit[cut:].strip()

        if current and len(current) + 1 + len(unit) > size:
            chunks.append(current)
            current = unit
        else:
            current = f"{current} {unit}".strip()

    if current:
        chunks.append(current)

    return chunks


def _key(passage):
    """What makes two passages the same, ignoring case, spacing and punctuation."""
    return " ".join(_WORD.findall(passage.casefold()))


def _lexical(focus, chunks):
    """Word-overlap scores in 0..1, rarer words counting for more."""
    focus_words = set(_WORD.findall(focus.casefold()))

    if not focus_words or not chunks:
        return [0.0] * len(chunks)

    chunk_words = [set(_WORD.findall(chunk.casefold())) for chunk in chunks]
    total = len(chunks)

    weight = {
        word: math.log(1 + total / (1 + sum(word in words for words in chunk_words)))
        for word in focus_words
    }
    best = sum(weight.values()) or 1.0

    return [sum(weight[w] for w in focus_words & words) / best for words in chunk_words]


def _semantic(focus, chunks):
    """Cosine scores from the local embedding model, or None if it is unavailable."""
    try:
        from actions import semantic_memory

        vectors = semantic_memory._encode([focus] + list(chunks))
    except Exception:
        return None

    if vectors is None:
        return None

    return [float(value) for value in vectors[1:] @ vectors[0]]


def reduce(text, focus, budget, seen=None):
    """The passages of [text] most relevant to [focus], within [budget] characters.

    [seen] is an optional set shared across pages; passages already in it are
    skipped and the kept ones are added, so evidence repeated across sources
    is sent once.
    """
    chunks = passages(text)[:MAX_PASSAGES]

    if seen is not None:
        chunks = [chunk for chunk in chunks if _key(chunk) not in seen]

    if not chunks:
        return ""

    lexical = _lexical(focus, chunks)
    semantic = _semantic(focus, chunks)

    scores = []

    for index, chunk in enumerate(chunks):
        score = lexical[index] if semantic is None else semantic[index] + 0.15 * lexical[index]

        if score > 0 and any(ch.isdigit() for ch in chunk):
            score += FIGURE_LIFT

        scores.append(score)

    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    floor = max(scores[ranked[0]], 0.0) * RELEVANCE_FLOOR

    kept = []
    used = 0

    for index in ranked:
        if scores[index] <= 0 or scores[index] < floor:
            break

        size = len(chunks[index]) + 1

        if used + size > budget:
            continue

        kept.append(index)
        used += size

    kept.sort()

    if seen is not None:
        seen.update(_key(chunks[index]) for index in kept)

    # A gap between kept passages is marked, so the model does not read two
    # distant sentences as one continuous thought.
    out = []
    previous = None

    for index in kept:
        if previous is not None and index != previous + 1:
            out.append("[...]")
        out.append(chunks[index])
        previous = index

    return " ".join(out)
