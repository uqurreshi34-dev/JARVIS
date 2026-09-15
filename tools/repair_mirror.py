"""Reconcile memory.json against subjects.txt, once, by hand.

The mirror is normally maintained on every write to subjects.txt. If that
file was restored from a backup, or edited while JARVIS was not running,
the two can drift apart. This brings them back in line.

Run it with JARVIS closed:

    python tools\\repair_mirror.py

A fact that already has a record keeps that record, so the date it was
learned survives. A fact with no record gets one. A record whose fact is
no longer in subjects.txt is removed, which is what deleting a line by
hand is supposed to mean.
"""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import files, memory_history, subject_store  # noqa: E402


def _counts():
    data = memory_history._load() or {"memories": []}
    records = [r for r in data.get("memories", []) if isinstance(r, dict)]

    return (
        len(records),
        sum(1 for r in records if r.get("key")),
        sum(1 for r in records if memory_history._is_subject_record(r)),
        sum(
            1 for r in records
            if not r.get("key")
            and not memory_history._is_subject_record(r)
        ),
    )


def main():
    root = files.root()
    path = os.path.join(root, subject_store.FILENAME)

    if not os.path.exists(path):
        print(f"No {subject_store.FILENAME} in {root}. Nothing to reconcile.")
        return 1

    subject_store._invalidate()
    facts = subject_store.line_count()

    total, keyed, mirrored, loose = _counts()

    print(f"subjects.txt      {facts} facts across "
          f"{len(subject_store.subjects())} subject(s)")
    print(f"memory.json       {total} records "
          f"({keyed} about you, {mirrored} about subjects, "
          f"{loose} unclaimed)")

    if mirrored == facts and not loose:
        print("\nAlready in step. Nothing to do.")
        return 0

    print("\nReconciling...")

    result = subject_store.sync_to_json()

    if result is None:
        print("Could not reach memory.json.")
        return 1

    total, keyed, mirrored, loose = _counts()

    print(f"memory.json       {total} records "
          f"({keyed} about you, {mirrored} about subjects, "
          f"{loose} unclaimed)")

    if mirrored == facts:
        print(f"\nOK: all {facts} facts in subjects.txt now have a record.")
        print("Start JARVIS, then run check_migration.py to confirm.")
        return 0

    print(f"\nStill out of step: {facts} facts, {mirrored} records.")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
