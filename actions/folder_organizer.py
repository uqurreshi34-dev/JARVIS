"""Safe, approval-gated organisation of the JARVIS folder."""

import json
import os
import re
import shutil
from pathlib import Path

import providers

from actions import files, folder_undo, journal


_MAX_FILES = 150
_MAX_GROUPS = 8
_MAX_FILES_PER_GROUP = 100
_MAX_PLAN_TOKENS = 1400

# These files are part of JARVIS's own state or configuration. Moving them
# would silently break the assistant, so they are never offered for sorting.
_PROTECTED_NAMES = frozenset({
    ".env",
    ".gitignore",
    ".jarvis-folder-undo.json",
    "calendar.txt",
    "contacts.txt",
    "jarvis-log.txt",
    "memory.txt",
    # The subject store and its metadata mirror. Both live in this folder
    # and both are JARVIS's own state, so sorting them away silently
    # empties what he knows.
    "subjects.txt",
    "memory.json",
    "patterns.txt",
    "spelling-ignore.txt",
    "tasks.txt",
    ".jarvis-folder-guard.json",
    "jarvis-pending.txt",
    "market-alerts.txt",
    "jarvis-market-marks.txt",
    # Moved into a Notes folder, notes.txt left JARVIS with no notes and
    # every new note at risk of the same.
    "notes.txt",
    # Connected services; moving it silently disconnects them.
    "mcp.json",
    # What those services' programs print, and the no-console check's
    # report (tools/check_services_no_console.py).
    "mcp-servers.log",
    "mcp-check.txt",
    # The Outlook sign-in; moving it signs calendar sync out.
    "outlook_token_cache.json",
    # Caches, cheap to lose but fetched again over the network if moved.
    "places.json",
    "quran-surahs.json",
    "quran-reciters.json",
    "quran-reciter-audio.json",
    # Saved meaning vectors; moving it means encoding everything again.
    "jarvis-vectors.sqlite",
    # Your own questions for tools/rag_eval.py --mine.
    "rag-questions.txt",
})

# Files JARVIS names by date: rotated logs and the monthly token ledger.
_PROTECTED_PATTERNS = (
    re.compile(r"^jarvis-log-.+\.txt$", re.IGNORECASE),
    re.compile(r"^usage-\d{4}-\d{2}\.jsonl$", re.IGNORECASE),
    # Copies of mcp.json kept before an edit ("mcp.json.before-..."). They
    # hold the same keys and tokens, so they stay where they are too.
    re.compile(r"^mcp\.json\..+$", re.IGNORECASE),
)


def is_protected(name):
    """True for a file that is JARVIS's own state and must stay where it is."""
    name = str(name or "").casefold()

    return (
        name in {value.casefold() for value in _PROTECTED_NAMES}
        or any(pattern.match(name) for pattern in _PROTECTED_PATTERNS)
    )

_REQUEST_RE = re.compile(
    r"\b(?:organise|organize)\b.*\b(?:folder|directory)\b",
    re.IGNORECASE,
)

_FOLDER_RE = re.compile(
    r"\b(?:organise|organize)\b.*?\b(?:the\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9 _-]{0,79}?)\s+(?:folder|directory)\b",
    re.IGNORECASE,
)

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": _MAX_FILES_PER_GROUP,
                    },
                },
                "required": ["folder", "files"],
                "additionalProperties": False,
            },
            "maxItems": _MAX_GROUPS,
        },
    },
    "required": ["groups"],
    "additionalProperties": False,
}

_PLAN_SYSTEM = """
You are JARVIS's folder-organisation planner.

Given a list of loose files in one JARVIS folder, propose a small, sensible
set of destination subfolders and assign files to them.

Rules:
- Group by what the files appear to be for, not by arbitrary one-file folders.
- Prefer existing destination folder names when they clearly fit.
- Use simple, human-readable folder names such as Reports, Images, Documents,
  Spreadsheets, Code, Notes, or Media when justified by the actual inventory.
- Do not invent a subject, project, or meaning that the filenames and types do
  not support.
- Every listed file may appear at most once.
- Do not include protected/system files.
- Do not include existing directories; only loose files are being organised.
- Do not delete, rename, merge, or overwrite files. Your output only proposes
  destination folders for moves.
- Leave files unassigned when there is not enough evidence to place them
  confidently.
- Return only the requested JSON.
"""


def _normalise(text):
    return " ".join(str(text or "").casefold().split())


def is_request(text):
    """True when the user explicitly asks to organise a folder."""
    return bool(_REQUEST_RE.search(str(text or "")))


def _target(request):
    """Resolve the requested target to the JARVIS root or one direct child."""
    text = str(request or "")
    lowered = _normalise(text)

    # "this/my/the JARVIS folder" refers to the JARVIS root.
    if re.search(
        r"\b(?:this|my|the)\s+(?:jarvis\s+)?(?:folder|directory)\b",
        lowered,
    ):
        return files.root(), files.FOLDER_NAME

    match = _FOLDER_RE.search(text)

    if not match:
        return None, None

    name = " ".join(match.group(1).split()).strip(" .")

    if not name or name.casefold() == files.FOLDER_NAME.casefold():
        return files.root(), files.FOLDER_NAME

    safe = files.safe_folder(name)

    if not safe:
        return None, None

    base = os.path.abspath(files.root() or "")
    path = os.path.abspath(os.path.join(base, safe))

    if os.path.commonpath([path, base]) != base:
        return None, None

    if not os.path.isdir(path):
        return None, safe

    return path, safe


def _inventory(base):
    """Return a bounded inventory of loose files and existing subfolders."""
    try:
        entries = list(Path(base).iterdir())
    except OSError as error:
        print(f"[JARVIS] could not inspect {base}: {error}")
        return None

    directories = sorted(
        entry.name
        for entry in entries
        if entry.is_dir()
    )

    records = []

    for entry in entries:
        if not entry.is_file():
            continue

        if is_protected(entry.name):
            continue

        try:
            stat = entry.stat()
        except OSError:
            continue

        records.append({
            "name": entry.name,
            "extension": entry.suffix.casefold() or "(none)",
            "bytes": stat.st_size,
        })

    records.sort(key=lambda item: item["name"].casefold())

    return {
        "files": records[:_MAX_FILES],
        "directories": directories,
        "truncated": len(records) > _MAX_FILES,
    }


def _chat(messages):
    """Use JARVIS's central provider chain for planning."""
    return providers.chat(
        messages,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "folder_organisation_plan",
                "schema": _PLAN_SCHEMA,
            },
        },
        temperature=0,
        max_tokens=_MAX_PLAN_TOKENS,
        reasoning_effort="low",
    )


def _parse_plan(content):
    text = str(content or "").strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        print("[JARVIS] folder organiser returned invalid JSON")
        return None


def _plan(base, inventory):
    """Ask the configured model to group the current loose files."""
    lines = []

    if inventory["directories"]:
        lines.append(
            "EXISTING SUBFOLDERS: "
            + ", ".join(inventory["directories"])
        )
    else:
        lines.append("EXISTING SUBFOLDERS: none")

    lines.append("LOOSE FILES:")

    for item in inventory["files"]:
        lines.append(
            f"- {item['name']} | {item['extension']} | {item['bytes']} bytes"
        )

    if inventory["truncated"]:
        lines.append(
            "NOTE: the file list was capped; do not infer anything about files "
            "that were not shown."
        )

    try:
        content = _chat([
            {"role": "system", "content": _PLAN_SYSTEM.strip()},
            {
                "role": "user",
                "content": "\n".join(lines),
            },
        ])
    except Exception as error:
        print(f"[JARVIS] folder planning failed: {error}")
        return None

    plan = _parse_plan(content)

    if not isinstance(plan, dict):
        return None

    return plan


def _validate_plan(base, inventory, plan):
    """Validate the model's plan against the actual inspected files."""
    allowed = {
        item["name"]: item
        for item in inventory["files"]
    }
    protected = {
        name.casefold()
        for name in _PROTECTED_NAMES
    }
    existing = {
        name.casefold()
        for name in inventory["directories"]
    }
    seen = set()
    groups = []

    for raw_group in (plan.get("groups") or [])[:_MAX_GROUPS]:
        if not isinstance(raw_group, dict):
            continue

        folder = files.safe_folder(raw_group.get("folder"))

        if not folder:
            continue

        if folder.casefold() in {".", ".."}:
            continue

        folder_key = folder.casefold()

        if folder_key == os.path.basename(os.path.normpath(base)).casefold():
            continue

        assignments = []

        for raw_name in (raw_group.get("files") or [])[:_MAX_FILES_PER_GROUP]:
            name = str(raw_name or "").strip()

            if name not in allowed:
                continue

            key = name.casefold()

            if key in seen or key in protected:
                continue

            source = os.path.abspath(os.path.join(base, name))

            if os.path.commonpath([source, base]) != os.path.abspath(base):
                continue

            assignments.append(name)
            seen.add(key)

        if assignments:
            groups.append({
                "folder": folder,
                "existing": folder_key in existing,
                "files": assignments,
            })

    return groups


def _describe(groups):
    """Turn the validated plan into the spoken approval question."""
    parts = []

    for group in groups:
        count = len(group["files"])
        noun = "file" if count == 1 else "files"
        parts.append(
            f"{count} {noun} into {group['folder']}"
            + (" folder" if count == 1 else " folder")
        )

    return "; ".join(parts)


def _execute(base, groups):
    """Execute only the approved validated plan, never overwriting files."""
    moved = 0
    skipped = 0
    created = set()
    moves = []
    conflicts = []

    base = os.path.abspath(base)
    protected = {
        name.casefold()
        for name in _PROTECTED_NAMES
    }

    for group in groups:
        folder = files.safe_folder(group["folder"])

        if not folder:
            skipped += len(group["files"])
            continue

        destination_dir = os.path.abspath(os.path.join(base, folder))

        if os.path.commonpath([destination_dir, base]) != base:
            skipped += len(group["files"])
            continue

        was_directory = os.path.isdir(destination_dir)

        try:
            os.makedirs(destination_dir, exist_ok=True)
        except OSError as error:
            print(f"[JARVIS] could not create {destination_dir}: {error}")
            skipped += len(group["files"])
            continue

        if not was_directory:
            created.add(destination_dir)

        for name in group["files"]:
            source = os.path.abspath(os.path.join(base, name))
            destination = os.path.abspath(os.path.join(destination_dir, name))

            if os.path.commonpath([source, base]) != base:
                skipped += 1
                continue

            if os.path.commonpath([destination, base]) != base:
                skipped += 1
                continue

            if not os.path.isfile(source):
                skipped += 1
                continue

            if name.casefold() in protected:
                skipped += 1
                continue

            # Another process or another command may have created the target
            # while the user was answering. Never overwrite it.
            if os.path.exists(destination):
                conflicts.append(name)
                skipped += 1
                continue

            try:
                shutil.move(source, destination)
                moved += 1
                moves.append({
                    "source": source,
                    "destination": destination,
                })
            except OSError as error:
                print(f"[JARVIS] could not move {source}: {error}")
                skipped += 1

    folder_undo.record(base, moves, created)

    journal.action(
        "organise_folder",
        f"organise {os.path.basename(base)}",
        moved > 0 or not groups,
    )

    return {
        "moved": moved,
        "skipped": skipped,
        "created": tuple(sorted(os.path.basename(path) for path in created)),
        "conflicts": tuple(conflicts),
    }


def _success_response(result):
    """Describe the completed organisation without hiding skipped files."""
    result = result or {}
    moved = int(result.get("moved", 0) or 0)
    skipped = int(result.get("skipped", 0) or 0)
    created = tuple(result.get("created") or ())
    conflicts = tuple(result.get("conflicts") or ())

    if moved == 0 and skipped == 0:
        return "Everything already looks organised, sir."

    message = f"Moved {moved} " + \
        ("file" if moved == 1 else "files") + ", sir."

    if created:
        count = len(created)
        message += (
            f" I created {count} new "
            f"folder{'s' if count != 1 else ''}."
        )

    if conflicts:
        count = len(conflicts)
        message += (
            f" I left {count} conflicting file"
            f"{'s' if count != 1 else ''} untouched."
        )
    elif skipped:
        message += (
            f" I left {skipped} "
            f"file{'s' if skipped != 1 else ''} untouched."
        )

    return message


def prepare(request):
    """Inspect, plan and prepare an approval-gated organisation action."""
    base, label = _target(request)

    if not base:
        if label:
            return {
                "status": "error",
                "message": (
                    f"I couldn't find a JARVIS folder called {label}, sir."
                ),
            }

        return {
            "status": "error",
            "message": (
                "Tell me which JARVIS folder you want organised, sir."
            ),
        }

    inventory = _inventory(base)

    if inventory is None:
        return {
            "status": "error",
            "message": f"I couldn't inspect the {label} folder, sir.",
        }

    if not inventory["files"]:
        return {
            "status": "empty",
            "message": f"There are no loose files to organise in {label}, sir.",
        }

    plan = _plan(base, inventory)

    if not plan:
        return {
            "status": "error",
            "message": "I couldn't prepare a safe organisation plan, sir.",
        }

    groups = _validate_plan(base, inventory, plan)

    if not groups:
        return {
            "status": "nothing",
            "message": (
                f"I inspected {label}, sir, but I don't have a confident "
                "organisation to propose."
            ),
        }

    total = sum(len(group["files"]) for group in groups)
    noun = "file" if total == 1 else "files"
    summary = _describe(groups)

    question = (
        f"I found {len(inventory['files'])} loose {noun}. "
        f"I propose: {summary}. Shall I proceed, sir?"
    )

    return {
        "status": "confirm",
        "question": question,
        "action": lambda: _execute(base, groups),
        "success_response": _success_response,
    }
