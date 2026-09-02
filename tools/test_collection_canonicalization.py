"""Offline tests for generic collection item canonicalisation."""

import copy
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import memory_collections


def main():
    failures = []

    cases = [
        ("projects", "react project", "react"),
        ("projects", "c-sharp project", "c-sharp"),
        ("cars", "Audi car", "Audi"),
        ("books", "the hobbit", "the hobbit"),
        ("books", "the book", "the book"),
    ]

    for key, value, expected in cases:
        actual = memory_collections._canonical_item(key, value)
        if actual != expected:
            failures.append(
                f"{key!r} / {value!r}: expected {expected!r}, got {actual!r}"
            )

    data = {
        "version": 1,
        "memories": [],
        "history": [],
        "collections": {
            "projects": [
                {"id": "one", "value": "react project"},
                {"id": "two", "value": "react"},
                {"id": "three", "value": "c-sharp project"},
            ],
        },
        "schemas": {"projects": {"cardinality": "collection"}},
        "relations": [
            {
                "id": "relation-1",
                "from": "memory:default project",
                "relation": "member_of",
                "to": "collection:projects:c-sharp project",
            }
        ],
    }

    saved = []

    def save(updated):
        saved.append(copy.deepcopy(updated))
        return True

    try:
        with patch.object(memory_collections.memory_history, "_save", side_effect=save), \
             patch.object(memory_collections, "_sync_text", return_value=True):
            changed = memory_collections._migrate_redundant_item_labels(data)

        if not changed:
            failures.append("migration did not report a change")

        values = [
            entry["value"] if isinstance(entry, dict) else entry
            for entry in data["collections"]["projects"]
        ]
        if values != ["react", "c-sharp"]:
            failures.append(f"unexpected migrated project values: {values!r}")

        target = data["relations"][0]["to"]
        if target != "collection:projects:c-sharp":
            failures.append(f"relationship target was not migrated: {target!r}")

        if len(saved) != 1:
            failures.append(f"expected one persistence write, got {len(saved)}")

    except Exception as error:
        failures.append(f"unexpected exception: {error}")

    if failures:
        print("FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASSED: collection labels canonicalise and migrate without domain-specific rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
