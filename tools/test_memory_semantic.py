"""Offline smoke test for JARVIS semantic memory retrieval.

The test replaces both provider calls with failures, supplies temporary
in-memory facts, and checks that paraphrased questions still reach the local
memory route and return a factual fallback without any LLM response.

Run from the JARVIS virtual environment with the normal provider keys present:

    python tools/test_memory_semantic.py

The provider functions are replaced before any test question is issued, so
no model API request is made by this test.
"""

import os
import sys
from unittest.mock import patch


# Keep provider construction happy while ensuring every actual model call in
# this test fails immediately. These are process-local overrides only.
os.environ.setdefault("GROQ_API_KEY", "semantic-test-key")
os.environ.setdefault("GEMINI_API_KEY", "semantic-test-key")
os.environ.setdefault("LLM_PROVIDER", "groq")


from actions import memory, semantic_memory  # noqa: E402
from actions import knowledge  # noqa: E402
import llm  # noqa: E402


TEST_FACTS = [
    ("gym days", "Sunday, Tuesday, Thursday and Saturday"),
    ("project", "JARVIS"),
    ("reply length", "short"),
]


QUESTIONS = (
    ("what are my gym days", "gym days"),
    ("when do I train", "gym days"),
    ("what are my training days", "gym days"),
    ("which days do I normally train", "gym days"),
    ("what's my workout schedule", "gym days"),
    ("which project am I working on", "project"),
)


def _failing_chat(*args, **kwargs):
    raise RuntimeError("simulated provider outage")


def main():
    original_facts = memory.facts
    semantic_memory.clear_cache()

    try:
        memory.facts = lambda: list(TEST_FACTS)
        semantic_memory.clear_cache()

        with patch.object(knowledge, "chat", side_effect=_failing_chat), \
             patch.object(llm, "chat", side_effect=_failing_chat):

            failures = []

            for question, expected_key in QUESTIONS:
                route = llm._local_memory_question(question)
                answer = knowledge.answer(question)

                print(f"\nQUESTION: {question}")
                print(f"LOCAL ROUTE: {route}")
                print(f"ANSWER: {answer}")

                if not route:
                    failures.append(f"not routed locally: {question}")
                    continue

                if not answer:
                    failures.append(f"no offline answer: {question}")
                    continue

                if expected_key not in answer.casefold():
                    failures.append(
                        f"wrong memory in answer: {question} -> {answer}"
                    )

            if failures:
                print("\nFAILED")
                for failure in failures:
                    print(f"- {failure}")
                return 1

            print("\nPASSED: semantic memory survived the simulated provider outage.")
            return 0

    finally:
        memory.facts = original_facts
        semantic_memory.clear_cache()


if __name__ == "__main__":
    sys.exit(main())
