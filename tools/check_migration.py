"""Show what is in each memory file, before or after migration.

Reads only. Run it before starting JARVIS and again afterwards, and the
two reports together tell you whether the move did what it should:

    python tools\\check_migration.py

Nothing here changes a file. It is safe to run at any time.
"""

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import files, memory, subject_store  # noqa: E402


def _json_data():
    path = os.path.join(files.root(), "memory.json")

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    except (OSError, ValueError) as error:
        print(f"could not read memory.json: {error}")
        return None


def main():
    root = files.root()

    print(f"JARVIS folder: {root}\n")

    # ---- memory.txt ----
    lines = memory._read()
    keyed = [line for line in lines if memory._is_keyed(line)]
    loose = [line for line in lines if not memory._is_keyed(line)]

    print(f"memory.txt        {len(lines)} lines "
          f"({len(keyed)} about you, {len(loose)} other)")

    for line in keyed:
        print(f"                    {line}")

    if loose:
        print(f"\n  {len(loose)} line(s) not yet moved:")

        for line in loose[:8]:
            print(f"                    {line[:76]}")

        if len(loose) > 8:
            print(f"                    ... and {len(loose) - 8} more")

    # ---- subjects.txt ----
    path = os.path.join(root, subject_store.FILENAME)

    print()

    if not os.path.exists(path):
        print("subjects.txt      does not exist yet "
              "(migration has not run -- start JARVIS)")
    else:
        names = subject_store.subjects()

        print(f"subjects.txt      {subject_store.line_count()} facts "
              f"across {len(names)} subject(s)")

        for name in names[:20]:
            facts = subject_store.entries(name)
            sources = sorted({source or "untagged" for _f, source in facts})
            print(f"                    {name}: {len(facts)} facts "
                  f"[{', '.join(sources)}]")

        if len(names) > 20:
            print(f"                    ... and {len(names) - 20} more")

    # ---- memory.json ----
    data = _json_data()

    print()

    if data is None:
        print("memory.json       not found")
        return 0

    records = [r for r in data.get("memories", []) if isinstance(r, dict)]
    subjects = [
        r for r in records
        if str(r.get("source") or "").startswith("subjects.txt")
    ]
    personal = [r for r in records if r.get("key")]
    orphans = [
        r for r in records
        if not r.get("key")
        and not str(r.get("source") or "").startswith("subjects.txt")
    ]

    print(f"memory.json       {len(records)} records "
          f"({len(personal)} about you, {len(subjects)} about subjects, "
          f"{len(orphans)} not yet moved)")
    print(f"                    {len(data.get('history', []))} archived "
          f"values, {len(data.get('relations', []))} relations, "
          f"{len(data.get('collections', {}))} collections")

    if subjects:
        dates = sorted(r.get("created_at", "") for r in subjects)
        print(f"                    earliest fact learned: {dates[0][:10]}")

    # ---- the check that matters ----
    print()

    if os.path.exists(path):
        stored = {
            f"{name}: {fact}".casefold()
            for name in subject_store.subjects()
            for fact in subject_store.facts_for(name)
        }
        mirrored = {
            str(r.get("value") or "").strip().casefold() for r in subjects
        }

        missing = stored - mirrored
        extra = mirrored - stored

        if not missing and not extra:
            print(f"OK: every one of the {len(stored)} facts in subjects.txt "
                  "has its record in memory.json.")
        else:
            if missing:
                print(f"{len(missing)} fact(s) in subjects.txt have no "
                      "record in memory.json")

            if extra:
                print(f"{len(extra)} record(s) in memory.json have no line "
                      "in subjects.txt")

            print("Start JARVIS once to resynchronise, then run this again.")

    if loose:
        print(f"\n{len(loose)} subject line(s) are still in memory.txt. "
              "Start JARVIS to move them.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
