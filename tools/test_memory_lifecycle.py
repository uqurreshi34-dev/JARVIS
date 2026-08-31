"""Regression checks for Memory Lifecycle v1.

Run from the repository root with:
    python tools/test_memory_lifecycle.py

The test uses a temporary memory folder and mocks semantic matching/model
classification. It never touches the user's real memory or provider quota.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from actions import files, memory, memory_lifecycle, semantic_memory  # noqa: E402


def _reset():
    memory_lifecycle._installed = False
    memory_lifecycle._original_remember = None
    memory_lifecycle._original_set_fact = None
    memory_lifecycle._original_forget = None
    memory_lifecycle._original_relevant_summary = None


def _write_memory(root, lines):
    with open(os.path.join(root, "memory.txt"), "w", encoding="utf-8") as handle:
        handle.write("# test memory\n")
        for line in lines:
            handle.write(f"{line}\n")


def _load_json(root):
    with open(os.path.join(root, "memory.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    _reset()

    with tempfile.TemporaryDirectory() as root:
        original_root = files.root
        original_rank = semantic_memory._semantic_rank
        original_chat = memory_lifecycle.chat
        original_best_related = memory_lifecycle._best_related

        files.root = lambda: root
        _write_memory(
            root,
            [
                "gym days: Sunday, Tuesday, Thursday and Saturday",
                "project: JARVIS",
            ],
        )

        try:
            memory_lifecycle.install()

            store = _load_json(root)
            assert len(store["memories"]) == 2, "existing memories were not migrated"

            # "my gym days are now ..." updates locally and strips the generic
            # "now" filler instead of storing it as part of the value.
            assert memory.remember(
                "my gym days are now Monday, Wednesday and Friday"
            )
            assert memory.get("gym days") == "Monday, Wednesday and Friday"

            store = _load_json(root)
            assert any(
                item["value"] == "Sunday, Tuesday, Thursday and Saturday"
                for item in store["history"]
            ), "old gym value was not archived"

            # "my new gym routine is ..." is semantically related to the
            # existing gym fact and uses the same local replacement path.
            memory_lifecycle._best_related = lambda text: (
                "gym days", "Monday, Wednesday and Friday", 0.91
            )
            assert memory.remember("my new gym routine is Tuesday and Saturday")
            assert memory.get("gym days") == "Tuesday and Saturday"

            # Historical questions use archived values, not the current one.
            semantic_memory._semantic_rank = lambda query, documents: (
                [(0, 0.91)] if documents else []
            )
            historical = memory.relevant_summary("what were my old gym days?")
            assert "Sunday, Tuesday, Thursday and Saturday" in historical

            historical_routine = memory.relevant_summary("what was my old gym routine?")
            assert "Monday, Wednesday and Friday" in historical_routine

            # A project activity is additive and does not replace the default project.
            assert memory.remember("I'm working on a Python project")
            assert memory.get("project") == "JARVIS"
            assert any(
                record.get("value") == "I'm working on a Python project"
                for record in _load_json(root)["memories"]
                if not record.get("key")
            ), "project activity was not added alongside default project"

            # An ambiguous semantic relation uses one small model decision.
            memory_lifecycle._best_related = lambda text: (
                "project", "JARVIS", 0.55
            )
            model_calls = []

            def fake_chat(**kwargs):
                model_calls.append(kwargs)
                return '{"relationship":"add","value":null}'

            memory_lifecycle.chat = fake_chat
            assert memory.remember("I am also experimenting with a Python side project")
            assert len(model_calls) == 1, "ambiguous memory relation should use one model call"
            assert memory.get("project") == "JARVIS"

            # Provider failure must preserve the old fact and add the new one.
            def failing_chat(**kwargs):
                raise RuntimeError("simulated provider outage")

            memory_lifecycle.chat = failing_chat
            assert memory.remember("I am exploring a second Python project")
            assert memory.get("project") == "JARVIS"

        finally:
            files.root = original_root
            semantic_memory._semantic_rank = original_rank
            memory_lifecycle.chat = original_chat
            memory_lifecycle._best_related = original_best_related
            _reset()

    print("PASSED: memory lifecycle migration, replacement, history, coexistence, and outage-safe behaviour.")


if __name__ == "__main__":
    main()
