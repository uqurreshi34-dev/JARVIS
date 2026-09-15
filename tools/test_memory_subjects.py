"""Offline tests for local subject resolution.

No microphone, no provider, no network. Memory and collections are replaced
with fixtures so the result does not depend on what happens to be stored on
this machine today.
"""

import re
import sys
from pathlib import Path

import numpy as np
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import memory_history, memory_subjects, semantic_memory  # noqa: E402


# Two unrelated domains on purpose. Nothing in memory_subjects.py should
# care which one it is looking at.
_FACTS = [
    ("name", "Umer"),
    # Keyed facts are the only things that carry a history, so the archive
    # cases below need their keys to exist here.
    ("gym days", "sunday, tuesday and thursday"),
    ("default project", "c sharp"),
    # Deliberately a multi-word subject label, because learn_subject stores
    # facts under the full name while people say the short one: "bmw fuel
    # economy" has to reach "bmw 3 series".
    (None, "BMW 3 Series: Returns around 55 mpg combined on diesel."),
    (None, "BMW 3 Series: Boot space is 480 litres in the saloon."),
    (None, "BMW 3 Series: Seats five adults on long motorway journeys."),
    (None, "Dune: Frank Herbert published it in 1965."),
    (None, "Dune: It won both the Hugo and the Nebula awards."),
]

_HISTORY = [
    {"key": "gym days", "value": "monday",
     "archived_at": "2026-08-31T17:47:47+00:00"},
    {"key": "gym days", "value": "tuesday, thursday and sunday",
     "archived_at": "2026-09-01T10:56:48+00:00"},
    # The most recently archived one is the answer, regardless of order.
    {"key": "gym days", "value": "on monday, wednesday and friday",
     "archived_at": "2026-09-14T22:18:59+00:00"},
    {"key": "default project", "value": "c-shop",
     "archived_at": "2026-09-02T09:00:00+00:00"},
]

_COLLECTIONS = {
    "cars": ["BMW 3 Series", "Ferrari 488"],
    "novels": ["Dune", "Neuromancer"],
}


_TOPICS = {
    "mpg": ("mpg", ("fuel", "economy", "mpg", "diesel")),
    "boot": ("boot", ("boot", "space", "litres")),
    "seats": ("seat", ("seat", "seats", "passengers", "adults")),
    "herbert": ("herbert", ("wrote", "author", "published")),
    "hugo": ("hugo", ("award", "awards", "won", "prize")),
}


def _topic_of(text):
    """Which stored subject-attribute a question or a fact is about."""
    lowered = str(text or "").casefold()
    words = set(re.findall(r"[a-z]+", lowered))

    for name, (marker, cues) in _TOPICS.items():
        if marker in lowered or words & set(cues):
            return name

    return None


def _fake_encode(texts):
    """Stand in for the ONNX encoder with vectors of known geometry.

    _rank_semantically calls semantic_memory._encode directly, so THAT is
    the seam a test has to replace. Patching _semantic_rank -- which the
    module stopped calling -- left the whole semantic path untested while
    the suite still looked like it covered it.

    Two texts about the same attribute score 1.0 here, two about
    different attributes score 0.15, and anything nothing is stored about
    scores 0.0. Those are deliberately far from _MIN_ATTRIBUTE_SCORE:
    this asserts the module's plumbing, while the real calibration is
    what tools/probe_attribute_scores.py measures.
    """
    order = list(_TOPICS)
    width = len(order) + 2
    rows = []

    for text in texts:
        topic = _topic_of(text)
        vector = np.zeros(width, dtype=np.float32)

        if topic is None:
            # Orthogonal to every stored fact: nothing answers this.
            vector[-1] = 1.0
        else:
            vector[order.index(topic)] = 1.0
            # A shared direction, so unrelated pairs score low but not
            # zero, the way a real encoder behaves.
            vector[-2] = 0.42

        rows.append(vector / np.linalg.norm(vector))

    return np.vstack(rows)


def _fixtures(encode=_fake_encode):
    """Patch memory, collections and the encoder for one test."""
    memory_subjects._last_query = None
    memory_subjects._last_answer = None

    return (
        patch.object(
            memory_subjects.memory,
            "facts",
            return_value=_FACTS,
        ),
        patch.object(
            memory_subjects.memory_collections,
            "_ensure_data",
            return_value={"collections": _COLLECTIONS},
        ),
        patch.object(
            memory_subjects.memory_collections,
            "items",
            side_effect=lambda key: _COLLECTIONS.get(key, []),
        ),
        patch.object(
            semantic_memory,
            "_encode",
            side_effect=encode,
        ),
        patch.object(
            memory_history,
            "_load",
            return_value={"history": _HISTORY},
        ),
    )


def _answers(question, encode=_fake_encode):
    facts, data, items, ranker, history = _fixtures(encode)

    with facts, data, items, ranker, history:
        return memory_subjects.local_answer(question)


def _check_category(failures):
    """A named subject resolves to the collection holding it."""
    cases = (
        ("what type of thing is BMW", "cars"),
        ("what category does Dune belong to", "novels"),
        # No stored facts at all: collection membership is enough.
        ("what type of thing is Neuromancer", "novels"),
    )

    # The collection item must win over the bare subject label, or the
    # answer degrades to "I know about it but it isn't in a collection".
    membership = _answers("what type of thing is BMW") or ""

    if "isn't in any of your collections" in membership:
        failures.append(
            "a subject with stored facts was reported as uncollected even "
            "though it is in one"
        )

    for question, collection in cases:
        answer = _answers(question)

        if not answer:
            failures.append(f"{question!r} was not answered locally")
        elif collection not in answer:
            failures.append(
                f"{question!r} answered {answer!r}, expected the "
                f"{collection} collection"
            )


def _check_attribute(failures):
    """One attribute returns one fact, not everything about the subject."""
    cases = (
        ("BMW fuel economy", "mpg", ("Boot space", "Seats five")),
        ("what is the boot space on the BMW", "480 litres",
         ("mpg", "Seats five")),
        ("who wrote Dune", "Frank Herbert", ("Hugo",)),
        ("what awards did Dune win", "Nebula", ("Frank Herbert",)),
    )

    for question, expected, unwanted in cases:
        answer = _answers(question) or ""

        if expected not in answer:
            failures.append(
                f"{question!r} answered {answer!r}, expected {expected!r}"
            )

        for other in unwanted:
            if other in answer:
                failures.append(
                    f"{question!r} also recited {other!r}; the subject's "
                    "other facts should not come along"
                )


def _check_history(failures):
    """A past-value question is answered from the archive, one step back."""
    cases = (
        ("what were my old gym days", "monday, wednesday and friday"),
        ("what was my previous gym days", "monday, wednesday and friday"),
        ("what was my previous default project", "c-shop"),
        # Speech recognition mishears keys constantly: "gym" arrives as
        # "jim". Token matching cannot bridge that on its own.
        ("what were my old jim days", "monday, wednesday and friday"),
    )

    for question, expected in cases:
        answer = _answers(question) or ""

        if expected not in answer:
            failures.append(
                f"{question!r} answered {answer!r}, expected {expected!r}"
            )

        if "previous" not in answer.casefold():
            failures.append(
                f"{question!r} did not make clear the value is a past one"
            )

    # The current question must NOT be answered from the archive.
    current = _answers("what are my gym days")

    if current and "monday, wednesday and friday" in current:
        failures.append(
            "a question about the current value was answered from history"
        )

    # A key with no archive declines rather than inventing one.
    if _answers("what was my old name") is not None:
        failures.append(
            "a key with no archived value still produced a history answer"
        )

    # Fuzzy key matching must not claim a question about something else.
    for unrelated in (
        "what were my old calendar entries",
        "what did I do before lunch",
    ):
        if _answers(unrelated) is not None:
            failures.append(
                f"{unrelated!r} was matched to a memory key by similarity"
            )


def _check_misheard(failures):
    """A misheard name must still resolve, everywhere it is used."""
    cases = (
        # Category: the word itself is wrong.
        ("what type of thing is Dunes", "novels"),
        ("what type of thing is Nueromancer", "novels"),
    )

    for question, collection in cases:
        answer = _answers(question) or ""

        if collection not in answer:
            failures.append(
                f"misheard {question!r} answered {answer!r}, expected the "
                f"{collection} collection"
            )

    # Attribute: the subject is misheard but the attribute is not.
    answer = _answers("BMW 3 Serees fuel economy") or ""

    if "mpg" not in answer:
        failures.append(
            f"a misheard subject broke an attribute question: {answer!r}"
        )

    # A single misheard word is repaired against the stored vocabulary
    # before any route runs.
    facts, data, items, ranker, history = _fixtures()

    with facts, data, items, ranker, history:
        repairs = (
            ("BMV fuel economy", "bmw"),
            ("what type of thing is Dunne", "dune"),
        )

        for spoken, expected in repairs:
            corrected = memory_subjects.correct(spoken)

            if expected not in corrected.casefold():
                failures.append(
                    f"{spoken!r} corrected to {corrected!r}, expected "
                    f"{expected!r} in it"
                )

        # An utterance with nothing stored in it must come back untouched,
        # or ordinary commands would be quietly reworded.
        for spoken in (
            "open netflix",
            "close notepad",
            "what is the capital of France",
            "what time is it",
        ):
            if memory_subjects.correct(spoken) != spoken:
                failures.append(
                    f"{spoken!r} was reworded to "
                    f"{memory_subjects.correct(spoken)!r}"
                )


def _check_correction_guards(failures):
    """The floor and the margin must each be doing real work.

    Uses its own deliberately confusable fixture: "Dune" and "Dunn" are
    close enough that no repair is safe, while "encore" is nearest to
    "Neuromancer" but nowhere near close enough to be a mishearing of it.
    """
    collections = {"novels": ["Dune", "Dunn", "Neuromancer"]}

    with (
        patch.object(
            memory_subjects.memory,
            "facts",
            return_value=[],
        ),
        patch.object(
            memory_subjects.memory_collections,
            "_ensure_data",
            return_value={"collections": collections},
        ),
        patch.object(
            memory_subjects.memory_collections,
            "items",
            side_effect=lambda key: collections.get(key, []),
        ),
    ):
        # Margin: "dunne" is equally close to Dune and Dunn, so guessing
        # between them is worse than leaving it alone.
        if memory_subjects.correct("dunne") != "dunne":
            failures.append(
                "an ambiguous word was repaired to one of two equally "
                "close labels instead of being left alone"
            )

        # Floor: "encore" is closest to Neuromancer by a clear margin, but
        # at 0.47 it is not a mishearing of anything stored.
        if memory_subjects.correct("encore") != "encore":
            failures.append(
                "a word that resembles nothing stored was still repaired"
            )

        # Positive control: an unambiguous mishearing IS repaired, so the
        # two checks above cannot pass by the feature being switched off.
        if "neuromancer" not in memory_subjects.correct("romance"):
            failures.append(
                "a clear mishearing was not repaired, so the guards above "
                "prove nothing"
            )


def _check_layers_independently(failures):
    """Each layer must stand on its own.

    Correction and fuzzy label matching overlap, so a whole-pipeline test
    passes even when one of them is broken. These exercise each directly.
    """
    # Only word correction can rescue this: the whole utterance is nothing
    # like the stored label, so similarity over the label cannot bridge it.
    answer = _answers("BMV fuel economy") or ""

    if "mpg" not in answer:
        failures.append(
            f"a misheard word inside a longer question was not repaired "
            f"before the routes ran: {answer!r}"
        )

    entries = [
        ("BMW 3 Series", "cars"),
        ("Neuromancer", "novels"),
    ]

    # Floor: "remainder" is closest to "Neuromancer" by a clear margin, so
    # only the similarity floor stops it being claimed. Without a case like
    # this the floor can be deleted unnoticed, because the margin check
    # happens to block most unrelated words on its own.
    for unrelated in ("remainder", "announcer", "helicopter"):
        if memory_subjects._closest_label(unrelated, entries) is not None:
            failures.append(
                f"{unrelated!r} was matched to a stored label despite "
                "resembling nothing"
            )

    # Margin: equally close to both, so neither may be chosen.
    ambiguous = [("Dune", "novels"), ("Dunn", "novels")]

    if memory_subjects._closest_label("dunne", ambiguous) is not None:
        failures.append(
            "a label was chosen from two equally close candidates"
        )

    # Positive control, so the two checks above cannot pass by the matcher
    # being disabled outright.
    if memory_subjects._closest_label("Neuromancr", entries) is None:
        failures.append(
            "a clear label mishearing was not matched, so the guards "
            "above prove nothing"
        )

    # A misheard subject word counts as consumed, not as an unanswered
    # attribute. Without this, every misheard name looks like a question
    # about something the subject does not have.
    if memory_subjects._remaining_tokens("toyoda", "toyota"):
        failures.append(
            "a misheard subject word was left over and treated as an "
            "unanswered attribute"
        )

    if not memory_subjects._remaining_tokens("toyota boot space", "toyota"):
        failures.append(
            "a genuine attribute was swallowed as part of the subject"
        )


def _check_declines(failures):
    """Anything it cannot answer well must fall through to the model."""
    cases = (
        # Known subject, but nothing stored answers this.
        "what is the BMW top speed",
        # Not about anything stored.
        "what is the capital of France",
        # Ordinary commands must be untouched.
        "open netflix",
        "close notepad",
        "what is on my calendar",
        "",
    )

    for question in cases:
        answer = _answers(question)

        if answer is not None:
            failures.append(
                f"{question!r} was answered locally as {answer!r}; "
                "it should have fallen through"
            )


def _check_encoder_absent(failures):
    """A missing encoder must degrade, not raise."""
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("encoder unavailable")

    try:
        category = _answers("what type of thing is BMW", encode=unavailable)
    except Exception as error:
        failures.append(f"a missing encoder raised: {error}")
        return

    if not category or "cars" not in category:
        failures.append(
            "category answers need no encoder but stopped working "
            "without one"
        )


def _check_memoised(failures):
    """The routing gate and the answer must not each do the work."""
    calls = []

    def counting_encode(texts):
        calls.append(tuple(texts))
        return _fake_encode(texts)

    facts, data, items, ranker, history = _fixtures(encode=counting_encode)

    with facts, data, items, ranker, history:
        memory_subjects.local_answer("BMW fuel economy")
        first = len(calls)

        memory_subjects.local_answer("BMW fuel economy")
        second = len(calls)

    if second != first:
        failures.append(
            "asking the same question twice ran the encoder twice"
        )


def _check_original_answer_still_runs(failures):
    """When nothing local matches, the existing answer path must be used."""
    class FakeCommands:
        @staticmethod
        def answer(question):
            return f"model answered {question!r}"

    fake = FakeCommands()
    original = memory_subjects._original_answer
    installed = memory_subjects._installed

    memory_subjects._original_answer = fake.answer

    facts, data, items, ranker, history = _fixtures()

    with facts, data, items, ranker, history:
        passed_through = memory_subjects._wrapped_answer(
            "what is the capital of France"
        )

        memory_subjects._last_query = None
        handled = memory_subjects._wrapped_answer("what type of thing is BMW")

    memory_subjects._original_answer = original
    memory_subjects._installed = installed

    if "model answered" not in (passed_through or ""):
        failures.append(
            "a question with no local answer did not reach the original "
            "answer function"
        )

    if "model answered" in (handled or ""):
        failures.append(
            "a question answered locally was still sent onwards"
        )


def main():
    failures = []

    _check_category(failures)
    _check_history(failures)
    _check_misheard(failures)
    _check_correction_guards(failures)
    _check_layers_independently(failures)
    _check_attribute(failures)
    _check_declines(failures)
    _check_encoder_absent(failures)
    _check_memoised(failures)
    _check_original_answer_still_runs(failures)

    memory_subjects._last_query = None
    memory_subjects._last_answer = None

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: subjects resolve locally, one attribute returns one fact, "
        "and anything uncertain still reaches the model."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
