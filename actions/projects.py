import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse


_APPDATA = os.environ.get("APPDATA", "")
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")

# Cursor is VS Code based, so recent workspaces live in its global storage.
_STATE_DB = os.path.join(
    _APPDATA, "Cursor", "User", "globalStorage", "state.vscdb"
)
_STORAGE_JSON = os.path.join(_APPDATA, "Cursor", "storage.json")

_RECENT_KEY = "history.recentlyOpenedPathsList"

# Where the Cursor executable usually lives.
_EXE_CANDIDATES = (
    r"C:\Program Files\cursor\Cursor.exe",
    os.path.join(_LOCALAPPDATA, "Programs", "cursor", "Cursor.exe"),
    os.path.join(_LOCALAPPDATA, "Programs", "Cursor", "Cursor.exe"),
)

_MATCH_RATIO = 0.6


@dataclass(frozen=True)
class Project:
    name: str
    path: str


def _path_from_uri(uri):
    """Turn a file:// URI into a Windows path, or return None."""
    if not uri:
        return None

    if not uri.startswith("file:"):
        return uri if os.path.isabs(uri) else None

    parsed = urlparse(uri)
    path = unquote(parsed.path)

    # "/c:/Users/..." -> "c:/Users/..."
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]

    return os.path.normpath(path)


def _entries_from_recent(payload):
    """Extract folder paths from a recentlyOpenedPathsList structure."""
    paths = []

    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue

        uri = entry.get("folderUri")

        if not uri:
            workspace = entry.get("workspace")

            if isinstance(workspace, dict):
                uri = workspace.get("configPath")

        path = _path_from_uri(uri)

        if path:
            paths.append(path)

    return paths


def _read_state_db(path=_STATE_DB):
    """Read recent folders from Cursor's SQLite state store."""
    if not os.path.exists(path):
        return []

    # Open read-only and immutable so a running Cursor can't block us.
    uri = f"file:{path.replace(os.sep, '/')}?mode=ro&immutable=1"

    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error as error:
        print(f"[JARVIS] could not open Cursor state: {error}")
        return []

    try:
        with connection:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (_RECENT_KEY,),
            ).fetchone()

    except sqlite3.Error as error:
        print(f"[JARVIS] could not read Cursor state: {error}")
        return []

    finally:
        connection.close()

    if not row or not row[0]:
        return []

    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return []

    return _entries_from_recent(payload)


def _read_storage_json(path=_STORAGE_JSON):
    """Fallback for older Cursor builds that used storage.json."""
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []

    paths = []

    recent = payload.get(_RECENT_KEY)

    if isinstance(recent, dict):
        paths.extend(_entries_from_recent(recent))

    backup = payload.get("backupWorkspaces")

    if isinstance(backup, dict):
        for folder in backup.get("folders") or []:
            if isinstance(folder, dict):
                candidate = _path_from_uri(folder.get("folderUri"))

                if candidate:
                    paths.append(candidate)

    return paths


class ProjectManager:
    """Recent Cursor projects, read lazily and cached."""

    def __init__(self):
        self._projects = None

    def projects(self, refresh=False):
        if self._projects is None or refresh:
            self._projects = self._load()

        return self._projects

    def names(self):
        return tuple(project.name for project in self.projects())

    def _load(self):
        paths = _read_state_db() or _read_storage_json()

        projects = []
        seen = set()

        for path in paths:
            key = os.path.normcase(path)

            if key in seen:
                continue

            seen.add(key)

            name = os.path.basename(path.rstrip("\\/"))

            if name:
                projects.append(Project(name=name, path=path))

        return tuple(projects)

    def find(self, name):
        query = (name or "").casefold().strip()

        if not query:
            return None

        projects = self.projects()

        for project in projects:
            if project.name.casefold() == query:
                return project

        for project in projects:
            if query in project.name.casefold():
                return project

        best = None
        best_score = 0.0

        for project in projects:
            score = SequenceMatcher(
                None, query, project.name.casefold()
            ).ratio()

            if score > best_score:
                best = project
                best_score = score

        return best if best_score >= _MATCH_RATIO else None

    def describe(self, limit=5):
        """Spoken list of the most recent projects."""
        projects = self.projects(refresh=True)

        if not projects:
            return None

        names = [project.name for project in projects[:limit]]

        if len(names) == 1:
            return f"Your most recent project is {names[0]}."

        listed = ", ".join(names[:-1]) + f", and {names[-1]}"

        return f"Your recent projects are {listed}."

    def open(self, name):
        project = self.find(name)

        if not project:
            print(f"[JARVIS] no recent project matching {name!r}")
            return None

        if not os.path.isdir(project.path):
            print(f"[JARVIS] project folder missing: {project.path}")
            return None

        executable = _cursor_executable()

        if not executable:
            print("[JARVIS] could not locate the Cursor executable")
            return None

        try:
            subprocess.Popen(
                [executable, project.path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            print(f"[JARVIS] failed to launch Cursor: {error}")
            return None

        return project


def _cursor_executable():
    """Locate Cursor, preferring the real executable over the CLI shim.

    The shim (cursor.CMD) works but routes through cmd.exe, which can flash
    a console window, so it is only a fallback.
    """
    for candidate in _EXE_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate

    return shutil.which("cursor")
