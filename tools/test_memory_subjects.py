"""Offline tests for local subject resolution.

No microphone, no provider, no network. Memory and collections are replaced
with fixtures so the result does not depend on what happens to be stored on
this machine today.
"""

import sys
from pathlib import Path
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


def _fake_rank(attribute, documents):
    """Stand in for the ONNX encoder with predictable scores.

    The real encoder links "fuel economy" to "55 mpg". This fakes that
    relationship so the test asserts the module's behaviour rather than
    the quality of the embedding model.
    """
    topics = {
        "mpg": ("fuel", "economy", "mpg", "diesel"),
        "boot": ("boot", "space", "litres"),
        "seats": ("seat", "seats", "passengers", "adults"),
        "herbert": ("wrote", "author", "published", "who"),
        "hugo": ("award", "awards", "won", "prize"),
    }

    words = set(attribute.casefold().split())
    scored = []

    for index, (_key, value, _display) in enumerate(documents):
        lowered = value.casefold()

        # A real encoder scores an unrelated fact weakly, not at zero. Using
        # zero here would let every unrelated fact be filtered out by
        # semantic_memory's own 0.38 floor, so the confidence check above it
        # would never be exercised and could be deleted unnoticed.
        score = 0.45

        for marker, cues in topics.items():
            if marker in lowered and words & set(cues):
                score = 0.90
                break

        scored.append((index, score))

    scored.sort(key=lambda item: item[1], reverse=True)

    return [
        (index, score)
        for index, score in scored
        if score >= 0.38
    ]


def _fixtures(rank=_fake_rank):
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
            "_semantic_rank",
            side_effect=rank,
        ),
        patch.object(
            memory_history,
            "_load",
            return_value={"history": _HISTORY},
        ),
    )


def _answers(question, rank=_fake_rank):
    facts, data, items, ranker, history = _fixtures(rank)

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
        category = _answers("what type of thing is BMW", rank=unavailable)
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

    def counting_rank(attribute, documents):
        calls.append(attribute)
        return _fake_rank(attribute, documents)

    facts, data, items, ranker, history = _fixtures(rank=counting_rank)

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
