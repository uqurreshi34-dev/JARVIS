"""Persistent undo for the most recent folder-organisation transaction."""

import json
import os
import shutil
from datetime import datetime, timezone

from actions import files


_STATE_NAME = ".jarvis-folder-undo.json"
_VERSION = 1


def _root():
    return os.path.abspath(files.root() or "")


def _state_path():
    root = _root()

    if not root:
        return None

    return os.path.join(root, _STATE_NAME)


def _safe_absolute(relative):
    """Resolve a recorded relative path while keeping it inside JARVIS."""
    root = _root()

    if not root:
        return None

    relative = str(relative or "")

    if os.path.isabs(relative):
        return None

    path = os.path.abspath(os.path.join(root, relative))

    if os.path.commonpath([path, root]) != root:
        return None

    return path


def _relative(path):
    """Store paths relative to the JARVIS root."""
    root = _root()
    path = os.path.abspath(path)

    if os.path.commonpath([path, root]) != root:
        return None

    return os.path.relpath(path, root)


def _load():
    """Load and minimally validate the persisted undo transaction."""
    path = _state_path()

    if not path or not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None

    if not isinstance(state, dict) or state.get("version") != _VERSION:
        return None

    moves = state.get("moves")
    created_dirs = state.get("created_dirs")

    if not isinstance(moves, list) or not isinstance(created_dirs, list):
        return None

    return {
        "version": _VERSION,
        "created_at": str(state.get("created_at") or ""),
        "moves": [
            move
            for move in moves
            if isinstance(move, dict)
            and isinstance(move.get("from"), str)
            and isinstance(move.get("to"), str)
        ],
        "created_dirs": [
            value
            for value in created_dirs
            if isinstance(value, str)
        ],
    }


def _write(state):
    """Persist the transaction atomically."""
    path = _state_path()

    if not path:
        return False

    temporary = f"{path}.tmp"

    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)

        os.replace(temporary, path)
        return True

    except OSError as error:
        print(f"[JARVIS] could not persist folder undo state: {error}")

        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass

        return False


def _clear():
    """Remove the persisted transaction after a complete undo."""
    path = _state_path()

    if not path:
        return

    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as error:
        print(f"[JARVIS] could not clear folder undo state: {error}")


def record(base, moves, created_dirs):
    """Record one completed organisation transaction."""
    root = _root()
    base = os.path.abspath(base or "")

    if not root or os.path.commonpath([base, root]) != root:
        return False

    safe_moves = []

    for move in moves or []:
        source = _relative(move.get("source"))
        destination = _relative(move.get("destination"))

        if source and destination:
            safe_moves.append({
                "from": source,
                "to": destination,
            })

    safe_dirs = []

    for directory in created_dirs or ():
        relative = _relative(os.path.abspath(directory))

        if relative and relative not in safe_dirs:
            safe_dirs.append(relative)

    if not safe_moves and not safe_dirs:
        return False

    state = {
        "version": _VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "moves": safe_moves,
        "created_dirs": safe_dirs,
    }

    return _write(state)


def can_undo():
    """True when a persisted folder-organisation transaction is available."""
    state = _load()
    return bool(state and (state["moves"] or state["created_dirs"]))


def undo():
    """Reverse the most recent recorded organisation transaction safely."""
    state = _load()

    if not state:
        return None

    restored = 0
    skipped = 0
    remaining_moves = []

    # Reverse the transaction in the opposite order to execution.
    for move in reversed(state["moves"]):
        source = _safe_absolute(move.get("to"))
        destination = _safe_absolute(move.get("from"))

        if not source or not destination:
            skipped += 1
            remaining_moves.append(move)
            continue

        if not os.path.isfile(source):
            skipped += 1
            remaining_moves.append(move)
            continue

        # Never overwrite something that appeared at the original location
        # after the organisation ran.
        if os.path.exists(destination):
            skipped += 1
            remaining_moves.append(move)
            continue

        try:
            shutil.move(source, destination)
            restored += 1
        except OSError as error:
            print(f"[JARVIS] could not undo {source}: {error}")
            skipped += 1
            remaining_moves.append(move)

    remaining_dirs = []
    removed_dirs = 0

    for relative in reversed(state["created_dirs"]):
        directory = _safe_absolute(relative)

        if not directory:
            continue

        if not os.path.isdir(directory):
            continue

        try:
            os.rmdir(directory)
            removed_dirs += 1
        except OSError:
            # The folder may now contain a file the user added after
            # organisation. Leave it intact rather than deleting new data.
            remaining_dirs.append(relative)

    if remaining_moves or remaining_dirs:
        _write({
            "version": _VERSION,
            "created_at": state["created_at"],
            "moves": list(reversed(remaining_moves)),
            "created_dirs": list(reversed(remaining_dirs)),
        })
    else:
        _clear()

    return {
        "restored": restored,
        "skipped": skipped,
        "removed_dirs": removed_dirs,
        "remaining": len(remaining_moves) + len(remaining_dirs),
    }


def success_response(result):
    """Describe the undo result for JARVIS's spoken response."""
    result = result or {}
    restored = int(result.get("restored", 0) or 0)
    removed_dirs = int(result.get("removed_dirs", 0) or 0)
    skipped = int(result.get("skipped", 0) or 0)
    remaining = int(result.get("remaining", 0) or 0)

    if restored == 0 and removed_dirs == 0 and skipped == 0:
        return "There was nothing to undo, sir."

    parts = []

    if restored:
        parts.append(
            f"restored {restored} "
            f"file{'s' if restored != 1 else ''}"
        )

    if removed_dirs:
        parts.append(
            f"removed {removed_dirs} "
            f"folder{'s' if removed_dirs != 1 else ''} I created"
        )

    message = "Undo complete, sir. " + " and ".join(parts) + "."

    if skipped:
        message += (
            f" I left {skipped} item"
            f"{'s' if skipped != 1 else ''} untouched"
        )

    if remaining:
        message += " because something at its original location had changed."

    return message
