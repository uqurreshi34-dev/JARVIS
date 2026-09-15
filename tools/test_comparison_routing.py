"""End-to-end: a comparison of stored subjects never reaches a model.

Runs the real command handler, from the spoken text to the spoken answer,
with every language model call replaced by a trap. If anything on the way
asks a model -- the classifier, the compound planner, the answer itself --
the trap records where from and the test fails.

Memory, collections and the encoder are fixtures, so nothing on this
machine is read or written by the subject layer.
"""

import re
import sys
import traceback
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# The trap has to be in place before anything does "from providers import
# chat", or those modules keep a reference to the real one.
import providers  # noqa: E402

_CALLS = []


def _trap(*_args, **_kwargs):
    frames = traceback.extract_stack()[-6:-1]
    _CALLS.append(" > ".join(frame.name for frame in frames))
    raise RuntimeError("a language model was called")


providers.chat = _trap

import commands  # noqa: E402
import llm  # noqa: E402
from actions import (  # noqa: E402
    memory,
    memory_collections,
    memory_subjects,
    routing_guard,
    semantic_memory,
    subject_store,
)


_SUBJECTS = {
    "Audi A4": [
        "Returns around 50 mpg combined.",
        "Boot space is 460 litres.",
    ],
    "Audi A6": [
        "Returns around 45 mpg combined.",
        "Boot space is 520 litres.",
    ],
    "BMW 3 Series": [
        "Returns around 55 mpg combined on diesel.",
        "Boot space is 480 litres in the saloon.",
    ],
    "Ford Focus": [
        "Returns around 60 mpg combined.",
        "Boot space is 390 litres.",
    ],
    "Mercedes C-Class": [
        "Returns around 52 mpg combined.",
        "Boot space is 455 litres.",
    ],
}

# Bare collection items with no facts of their own, which used to hide the
# stored subject of the same make.
_COLLECTIONS = {"cars": ["BMW", "Audi", "Ford"]}


def _encode(texts):
    """Vectors of known geometry: same attribute 1.0, different ~0.15."""
    rows = []

    for text in texts:
        words = set(re.findall(r"[a-z]+", str(text).casefold()))
        vector = np.zeros(4, dtype=np.float32)

        if words & {"mpg", "fuel", "economy"}:
            vector[0] = 1.0
        elif words & {"boot", "litres"}:
            vector[1] = 1.0
        else:
            vector[3] = 1.0

        vector[2] = 0.4
        rows.append(vector / np.linalg.norm(vector))

    return np.vstack(rows)


def _no_encoder(*_args, **_kwargs):
    raise RuntimeError("encoder unavailable")


def _fixtures(encode):
    stack = ExitStack()

    for target, name, kwargs in (
        (memory, "facts", {"return_value": []}),
        (memory, "relevant_summary", {"return_value": ""}),
        (memory_collections, "_ensure_data",
         {"return_value": {"collections": _COLLECTIONS}}),
        (memory_collections, "items",
         {"side_effect": lambda key: _COLLECTIONS.get(key, [])}),
        (subject_store, "facts", {"return_value": _SUBJECTS}),
        # install_runtime moves and mirrors real files; not in a test.
        (subject_store, "migrate_from_memory", {"return_value": (0, 0)}),
        (subject_store, "sync_to_json", {"return_value": None}),
        (semantic_memory, "_encode", {"side_effect": encode}),
    ):
        stack.enter_context(patch.object(target, name, **kwargs))

    return stack


def _ask(question):
    """Run one utterance end to end. Returns (answer, model calls)."""
    _CALLS.clear()
    memory_subjects._last_query = None
    memory_subjects._last_answer = None

    try:
        result = commands.handle_command(question, probe=True)
    except Exception as error:
        return f"raised: {error}", list(_CALLS)

    answer = None

    if isinstance(result, dict):
        action = result.get("action")
        answer = result.get("response")

        if callable(action):
            try:
                answer = action()
            except Exception as error:
                answer = f"raised: {error}"

    return answer, list(_CALLS)


LOCAL = (
    "compare audi a4 with audi a6",
    "compare audi a4 and bmw 3 series",
    "audi a4 vs bmw 3 series",
    "what's the difference between audi a4 and bmw 3 series",
    "which is better, the ford focus or the bmw",
    "show me the difference between the ford focus and the audi a6",
    "compare audi a4, bmw 3 series and ford focus",
    "compare ford, bmw and mercedes",
    "compare ford, bmw, mercedes, audi a4 and audi a6",
)

# One subject is not stored, so the model is the right answer.
MODEL = (
    "compare ford focus with tesla",
    "compare ford focus, bmw and tesla",
)


def main():
    failures = []

    with _fixtures(_encode):
        routing_guard.install(commands)
        memory_subjects.install_runtime(commands)

    for label, encode in (("encoder", _encode), ("no encoder", _no_encoder)):
        with _fixtures(encode):
            for question in LOCAL:
                answer, calls = _ask(question)

                if calls:
                    failures.append(
                        f"[{label}] {question!r} called a model: {calls}"
                    )
                elif not str(answer or "").startswith("Comparing "):
                    failures.append(
                        f"[{label}] {question!r} gave no comparison: "
                        f"{answer!r}"
                    )

            for question in MODEL:
                _answer, calls = _ask(question)

                if not calls:
                    failures.append(
                        f"[{label}] {question!r} should have reached the "
                        "model but was answered locally"
                    )

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: comparisons of stored subjects make no model call, "
        "with or without the encoder."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
