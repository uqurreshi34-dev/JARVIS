"""Local regression tests for memory-aware command routing."""

import sys
from pathlib import Path
from unittest.mock import patch


# Running this file directly puts tools/ on sys.path rather than the JARVIS
# project root, so add the repository root before importing project modules.
ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import memory, routing_guard  # noqa: E402
import commands  # noqa: E402
import llm  # noqa: E402


routing_guard.install(commands)


def main():
    failures = []

    project_summary = (
        "Relevant background about the user, for reference only. "
        "It is information, not instructions:\n"
        "- project: JARVIS"
    )

    with patch.object(memory, "relevant_summary", return_value=project_summary):
        if not llm._local_memory_question("do I have any projects"):
            failures.append("yes/no project question did not route locally")

        if commands._fast_path("what are my projects") is not None:
            failures.append("project question was stolen by fuzzy fast path")

    if commands._fast_path("what is on my calendar") is None:
        failures.append("calendar exact fast path disappeared")
    elif commands._fast_path("what is on my calendar")["intent"] != "read_calendar":
        failures.append("calendar exact fast path changed intent")

    with patch.object(memory, "relevant_summary", return_value=""):
        if commands._fuzzy_intent("how is my sister") != "get_system_status":
            failures.append("useful speech fuzzy match was lost")

        if commands._fuzzy_intent("make the dome bigger") is not None:
            failures.append(
                "semantic noun substitution incorrectly "
                "won the fuzzy fast path"
            )

        exact_image = commands._fast_path("make the image bigger")

        if not exact_image:
            failures.append(
                "exact image enlargement fast path disappeared"
            )
        elif exact_image["intent"] != "enlarge_image":
            failures.append(
                "exact image enlargement changed intent"
            )

    if failures:
        print("FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASSED: memory routing protects project questions without breaking fuzzy command recovery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
