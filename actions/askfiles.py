"""Planning bridge for AskFiles.

JARVIS decides how the contents of one Android folder should be grouped;
AskFiles remains responsible for the actual filesystem changes. The model
never receives permission to execute Android operations or invent absolute
paths. It only returns a bounded organisation plan using direct child folder
names.
"""

import json
from typing import Any

from providers import chat


_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "moves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["file", "destination"],
                "additionalProperties": False,
            },
        },
        "create_folders": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "moves", "create_folders"],
    "additionalProperties": False,
}

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "askfiles_folder_organisation",
        "schema": _SCHEMA,
        "strict": True,
    },
}

_SYSTEM = """You are the planning layer for AskFiles, an Android file manager.

The user asked AskFiles to organise the folder currently open in the app.
Return ONLY a JSON organisation plan matching the supplied schema.

Rules:
- Organise ONLY the files directly listed in the current folder.
- Never invent files that are not in the manifest.
- Never move or classify a directory itself; only files may be moved.
- Destinations must be DIRECT CHILD FOLDER NAMES of the current folder, with no
  slash, backslash, URI, absolute path, or parent traversal.
- Reuse an existing child folder when it is an appropriate destination.
- Put a missing destination folder in create_folders so AskFiles can create it.
- Keep standard Android media directories such as DCIM/Camera or Pictures
  intact when that current folder is itself clearly a camera/media folder;
  prefer no moves there rather than restructuring a system media location.
- Be conservative. Ambiguous files should be left in place.
- Do not delete, overwrite, rename, or move files outside the current folder.
- The summary should describe only the proposed moves.
"""


def _safe_name(value: Any) -> str:
    """Normalise one model-returned file or folder name."""
    text = str(value or "").strip()

    if not text or text in {".", ".."}:
        return ""

    if "/" in text or "\\" in text:
        return ""

    return text


def plan_folder_organisation(
    current_path: str,
    current_folder: str,
    items: list[dict[str, Any]],
    existing_child_folders: list[str],
) -> dict[str, Any]:
    """Ask the configured provider chain for a bounded folder plan."""
    normalized_path = (current_path or "").replace(
        "\\", "/").rstrip("/").casefold()

    # Android's camera tree is already a purposeful media structure. Never
    # ask the model to reorganise it into arbitrary category folders.
    if normalized_path.endswith("/dcim") or normalized_path.endswith("/dcim/camera"):
        return {
            "summary": "This is a standard Android camera folder, so I will leave its structure intact.",
            "moves": [],
            "create_folders": [],
        }

    manifest = []
    valid_names = set()
    directories = set()

    for item in items:
        name = str(item.get("name") or "").strip()

        if not name:
            continue

        entry = {
            "name": name,
            "directory": bool(item.get("isDirectory")),
            "size": int(item.get("size") or 0),
        }
        manifest.append(entry)

        if entry["directory"]:
            directories.add(name)
        else:
            valid_names.add(name)

    prompt = json.dumps(
        {
            "current_folder": current_folder,
            "current_path": current_path,
            "existing_child_folders": existing_child_folders,
            "items": manifest,
        },
        ensure_ascii=False,
    )

    result = chat(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        response_format=_RESPONSE_FORMAT,
        temperature=0,
        max_tokens=1800,
    )

    try:
        plan = json.loads(result)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(
            "AskFiles organisation plan was not valid JSON") from error

    moves = []
    seen = set()

    for move in plan.get("moves") or []:
        if not isinstance(move, dict):
            continue

        source = _safe_name(move.get("file"))
        destination = _safe_name(move.get("destination"))

        if not source or not destination:
            continue

        if source not in valid_names or source in seen:
            continue

        seen.add(source)
        moves.append({"file": source, "destination": destination})

    existing = {
        _safe_name(name)
        for name in existing_child_folders
        if _safe_name(name)
    }

    create_folders = []
    for raw_name in plan.get("create_folders") or []:
        name = _safe_name(raw_name)

        if (
            name
            and name not in existing
            and name not in create_folders
        ):
            create_folders.append(name)

    destinations = {move["destination"] for move in moves}

    # Any destination used by a move must either exist already or be created.
    for destination in sorted(destinations):
        if destination not in existing and destination not in create_folders:
            create_folders.append(destination)

    # Never create a folder whose name collides with a file or existing folder.
    create_folders = [
        name
        for name in create_folders
        if name not in valid_names and name not in directories
    ]

    return {
        "summary": str(plan.get("summary") or "No changes proposed.").strip(),
        "moves": moves,
        "create_folders": create_folders,
    }
