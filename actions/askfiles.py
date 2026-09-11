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
        "summary": {
            "type": "string",
        },
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "match": {
                        "type": "string",
                        "enum": ["extension", "prefix"],
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "destination": {
                        "type": "string",
                    },
                },
                "required": [
                    "match",
                    "values",
                    "destination",
                ],
                "additionalProperties": False,
            },
        },
        "exceptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "destination": {
                        "type": "string",
                    },
                },
                "required": [
                    "indices",
                    "destination",
                ],
                "additionalProperties": False,
            },
        },
        "create_folders": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "summary",
        "rules",
        "exceptions",
        "create_folders",
    ],
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

The user asked AskFiles to organise the files directly inside the current
folder. Return ONLY a JSON organisation plan matching the supplied schema.

The file list is numbered from 1 upward. Those indices are the only file
identifiers you may use.

Rules:
- Organise ONLY the listed files in the current folder.
- Never invent a file.
- Directories are not included in the file list and must never be moved.
- Destinations must be DIRECT CHILD FOLDER NAMES of the current folder.
- Never use a slash, backslash, URI, absolute path, or parent traversal.
- Reuse an existing child folder when appropriate.
- Missing destination folders belong in create_folders.
- Standard Android camera/media folders must remain intact; this is already
  enforced by the caller for recognised camera paths.
- Be conservative. Ambiguous files should remain where they are.

Compact planning:
- Prefer extension rules for common file types.
- Use prefix rules when filenames clearly identify a narrower group, such as
  screenshots.
- Prefix rules override extension rules.
- Use exceptions only for files that need individual treatment.
- Do not create a separate exception for every ordinary file when a rule can
  cover them.
- Use at most 300 exception indices total. If more would be needed, leave the
  ambiguous files unmoved.
- Avoid overlapping rules that would assign the same file to different
  destinations.
- Every extension should normally have at most one destination.
- Every prefix should normally identify one destination.

The planner's output is expanded locally by JARVIS into individual file moves.
Do not return individual filename-based move objects.
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
    index_to_name: dict[int, str] = {}
    index_to_extension: dict[int, str] = {}
    valid_names = set()
    directories = set()

    next_index = 1

    for item in items:
        name = str(item.get("name") or "").strip()

        if not name:
            continue

        if bool(item.get("isDirectory")):
            directories.add(name)
            continue

        dot = name.rfind(".")
        extension = (
            name[dot:].casefold()
            if dot > 0
            else ""
        )

        index_to_name[next_index] = name
        index_to_extension[next_index] = extension
        valid_names.add(name)

        manifest.append([
            next_index,
            name,
            extension,
        ])

        next_index += 1

    prompt = json.dumps(
        {
            "folder": current_folder,
            "existing_folders": existing_child_folders,
            "files": manifest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    result = chat(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        response_format=_RESPONSE_FORMAT,
        temperature=0,
        max_tokens=3072,
        reasoning_effort="low",
    )

    try:
        plan = json.loads(result)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(
            "AskFiles organisation plan was not valid JSON") from error

    assigned: dict[int, str] = {}
    ambiguous: set[int] = set()

    def normalise_extension(value: Any) -> str:
        text = str(value or "").strip().casefold()

        if not text:
            return ""

        if not text.startswith("."):
            text = "." + text

        if "/" in text or "\\" in text:
            return ""

        return text

    extension_destinations: dict[str, str | None] = {}

    for rule in plan.get("rules") or []:
        if not isinstance(rule, dict):
            continue

        if str(rule.get("match") or "").strip().casefold() != "extension":
            continue

        destination = _safe_name(rule.get("destination"))

        if not destination:
            continue

        for raw_value in rule.get("values") or []:
            extension = normalise_extension(raw_value)

            if not extension:
                continue

            existing_destination = extension_destinations.get(extension)

            if existing_destination is None and extension in extension_destinations:
                continue

            if existing_destination and existing_destination != destination:
                extension_destinations[extension] = None
            elif extension not in extension_destinations:
                extension_destinations[extension] = destination

    prefix_rules: list[tuple[str, str]] = []

    for rule in plan.get("rules") or []:
        if not isinstance(rule, dict):
            continue

        if str(rule.get("match") or "").strip().casefold() != "prefix":
            continue

        destination = _safe_name(rule.get("destination"))

        if not destination:
            continue

        for raw_value in rule.get("values") or []:
            prefix = str(raw_value or "").strip()

            if not prefix or "/" in prefix or "\\" in prefix:
                continue

            prefix_rules.append((prefix.casefold(), destination))

    for index, name in index_to_name.items():
        matching_prefix_destinations = {
            destination
            for prefix, destination in prefix_rules
            if name.casefold().startswith(prefix)
        }

        if len(matching_prefix_destinations) == 1:
            assigned[index] = next(iter(matching_prefix_destinations))
            continue

        if len(matching_prefix_destinations) > 1:
            ambiguous.add(index)
            continue

        extension = index_to_extension.get(index, "")
        destination = extension_destinations.get(extension)

        if destination:
            assigned[index] = destination

    exception_count = 0

    for exception in plan.get("exceptions") or []:
        if not isinstance(exception, dict):
            continue

        destination = _safe_name(exception.get("destination"))

        if not destination:
            continue

        for raw_index in exception.get("indices") or []:
            if exception_count >= 300:
                break

            if (
                not isinstance(raw_index, int)
                or isinstance(raw_index, bool)
                or raw_index not in index_to_name
            ):
                continue

            assigned[raw_index] = destination
            ambiguous.discard(raw_index)
            exception_count += 1

    moves = []

    for index, destination in assigned.items():
        if index in ambiguous:
            continue

        source = index_to_name.get(index)

        if not source or not destination:
            continue

        moves.append({
            "file": source,
            "destination": destination,
        })

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
