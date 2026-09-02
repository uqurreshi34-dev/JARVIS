"""Offline regression tests for Chunk 2 connected memory."""

import copy
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import memory_relations


def main():
    failures = []
    data = {
        "version": 1,
        "memories": [
            {"key": "default project", "value": "C-sharp"},
        ],
        "history": [],
        "collections": {
            "projects": [
                {"value": "react project"},
                {"value": "C-sharp"},
            ],
        },
        "schemas": {"projects": {"cardinality": "collection"}},
        "relations": [],
    }

    def load():
        return data

    def save(updated):
        # Simulate persistence without mutating the same object that the
        # implementation just passed to us. The real JSON writer serialises
        # the object; it does not clear it in place.
        saved = copy.deepcopy(updated)
        data.clear()
        data.update(saved)
        return True

    try:
        with patch.object(memory_relations.memory_history, "_load", side_effect=load), \
             patch.object(memory_relations.memory_history, "_save", side_effect=save):
            added = memory_relations.sync_memberships()

            expected_to = memory_relations.collection_entity("projects", "C-sharp")
            expected_from = memory_relations.memory_entity("default project")

            if added != 1:
                failures.append(f"expected one inferred relation, got {added}")

            if not memory_relations.has(expected_from, "member_of", expected_to):
                failures.append("inferred membership relation was not stored")

            results = memory_relations.traverse(
                expected_from,
                relation="member_of",
                direction="out",
                max_hops=1,
            )

            if not results or results[0]["entity"] != expected_to:
                failures.append("local traversal did not reach collection item")

            if memory_relations.sync_memberships() != 0:
                failures.append("membership inference was not idempotent")

            if len(data["relations"]) != 1:
                failures.append("unexpected relationship count")

    except Exception as error:
        failures.append(f"unexpected exception: {error}")

    if failures:
        print("FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASSED: connected memory storage, inference, and traversal are offline-safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
