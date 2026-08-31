"""Local regression tests for memory-aware command routing."""

from unittest.mock import patch

from actions import memory, routing_guard
import commands
import llm


routing_guard.install(commands)


def main():
    failures = []

    with patch.object(llm, "_local_memory_question", return_value=True):
        if commands._fast_path("what are my projects") is not None:
            failures.append("project question was stolen by fuzzy fast path")

        if not llm._local_memory_question("do I have any projects"):
            failures.append("yes/no project question did not route locally")

    if commands._fast_path("what is my calendar") is None:
        failures.append("calendar exact fast path disappeared")
    elif commands._fast_path("what is my calendar")["intent"] != "read_calendar":
        failures.append("calendar exact fast path changed intent")

    with patch.object(llm, "_local_memory_question", return_value=False):
        if commands._fuzzy_intent("how is my sister") != "get_system_status":
            failures.append("useful speech fuzzy match was lost")

    if failures:
        print("FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASSED: memory routing protects personal questions without breaking command fuzzing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
