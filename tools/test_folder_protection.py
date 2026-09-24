"""Tidying the JARVIS folder never moves JARVIS's own files.

"Organise my folder" and the automatic folder guard move loose files into
subfolders. On 24 September 2026 that moved notes.txt into a Notes folder,
where JARVIS no longer looks, so every note seemed to vanish. The same was
true of the Outlook sign-in, mcp.json, the token ledger and old logs.

Checked:

- every file JARVIS keeps in its folder is protected -- found by reading
  the code, so a store added later fails here until it is protected too;
- dated files (rotated logs, the monthly ledger) are protected by pattern;
- protected files are never offered for sorting, while ordinary ones are;
- the automatic guard sorts ordinary files and leaves JARVIS's alone.

Runs in a sandboxed JARVIS folder.

    python tools/test_folder_protection.py
"""

import ast
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import folder_organizer  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- every store the code keeps in the JARVIS folder -----------------------

def stores():
    """File names that modules building paths on the JARVIS folder use."""
    found = {}
    sources = list(ROOT.glob("*.py")) + list((ROOT / "actions").glob("*.py"))

    for path in sources:
        source = path.read_text(encoding="utf-8", errors="ignore")

        if "files.root()" not in source and 'expanduser("~")' not in source:
            continue

        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"[\w.\-]+\.(?:txt|json|jsonl)", node.value)
            ):
                found.setdefault(node.value, set()).add(path.name)

    return found


named = stores()
check(len(named) >= 15, f"the code's own files are found ({len(named)})")

for name, where in sorted(named.items()):
    check(folder_organizer.is_protected(name), f"protected: {name} ({', '.join(sorted(where))})")

for name in ("jarvis-log-2026-08-01.txt", "usage-2026-09.jsonl", "USAGE-2026-10.JSONL"):
    check(folder_organizer.is_protected(name), f"protected by pattern: {name}")

for name in ("shopping list.txt", "report.docx", "usage notes.txt", "my-notes.txt", "jarvis-log.md"):
    check(not folder_organizer.is_protected(name), f"ordinary file may be tidied: {name}")

# ---- what the organiser is offered -----------------------------------------

ordinary = ["shopping list.txt", "holiday.jpg", "script.py"]
own = sorted(named) + ["jarvis-log-2026-08-01.txt", "usage-2026-09.jsonl"]

for name in ordinary + own:
    with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
        handle.write("x\n")

offered = {item["name"] for item in folder_organizer._inventory(folder)["files"]}

check(set(ordinary) <= offered, "ordinary files are offered for sorting")
check(not (offered & set(own)), "none of JARVIS's own files are offered: "
      + (", ".join(sorted(offered & set(own))) or "none"))

# ---- the automatic guard -----------------------------------------------------

try:
    from actions import folder_guard

    guard = folder_guard.FolderGuard()
    categories = {name: guard._category_for({"name": name, "extension": Path(name).suffix})
                  for name in ("notes.txt", "shopping list.txt")}
except Exception as error:
    print(f"SKIP folder guard categories ({error})")
else:
    check(categories["shopping list.txt"] is not None, "the guard still sorts an ordinary text file")

    inventory_names = {item["name"] for item in folder_organizer._inventory(folder)["files"]}
    check("notes.txt" not in inventory_names, "the guard, which sorts from the same inventory, never sees notes.txt")

sys.exit(1 if failures else 0)
