"""Persistent, local housekeeping for the JARVIS folder.

This module deliberately does not use an LLM. It watches only the JARVIS
root, moves files only when their type/name gives a clear destination, and
reuses folder_organizer's existing safe move and persistent-undo machinery.
"""

from __future__ import annotations

import json
import os
import threading

from actions import files, folder_organizer


_STATE_NAME = ".jarvis-folder-guard.json"
_POLL_SECONDS = 6 * 60 * 60

# Local, high-confidence categories only. Anything not covered here is left
# alone rather than guessed at.
_CATEGORIES = (
    ("Spreadsheets", frozenset({".csv", ".tsv", ".xls", ".xlsx", ".ods"})),
    ("Images", frozenset({
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    })),
    ("Media", frozenset({
        ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wav", ".mp3", ".m4a",
    })),
    ("Documents", frozenset({
        ".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt", ".md",
    })),
    ("Code", frozenset({
        ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".ps1",
        ".bat", ".cmd", ".sh",
    })),
)

# JARVIS keeps these files in its root. The manual organiser already protects
# most of them; these extra names/patterns are important for automatic mode.
_PROTECTED_AUTO_NAMES = frozenset({
    _STATE_NAME,
    "jarvis-pending.txt",
    "market-alerts.txt",
})


class FolderGuard:
    """Maintain a standing, local request to keep JARVIS tidy."""

    def __init__(self):
        self._listener = None
        self._follow_up_listener = None
        self._busy_checker = None
        self._stop = threading.Event()
        self._thread = None

    def set_listener(self, listener):
        """Register the callable used for spoken guard announcements."""
        self._listener = listener

    def set_follow_up_listener(self, listener):
        """Register the callable used to open an immediate follow-up window."""
        self._follow_up_listener = listener

    def set_busy_checker(self, checker):
        """Register a callable returning True while JARVIS is handling a turn."""
        self._busy_checker = checker

    def _root(self):
        return os.path.abspath(files.root() or "")

    def _state_path(self):
        root = self._root()
        return os.path.join(root, _STATE_NAME) if root else None

    def _read_state(self):
        path = self._state_path()

        if not path or not os.path.isfile(path):
            return {}

        try:
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError, TypeError):
            return {}

        return state if isinstance(state, dict) else {}

    def _write_state(self, enabled):
        path = self._state_path()

        if not path:
            return False

        temporary = f"{path}.tmp"

        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"enabled": bool(enabled)}, handle, indent=2)

            os.replace(temporary, path)
            return True

        except OSError as error:
            print(f"[JARVIS] could not save folder guard state: {error}")

            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

            return False

    def enabled(self):
        """True when persistent JARVIS-folder housekeeping is enabled."""
        return bool(self._read_state().get("enabled"))

    def enable(self):
        """Enable persistent housekeeping."""
        return self._write_state(True)

    def disable(self):
        """Disable persistent housekeeping without touching existing files."""
        return self._write_state(False)

    @staticmethod
    def _existing_folder(existing, wanted):
        """Reuse an existing destination folder without changing its casing."""
        wanted_key = wanted.casefold()

        for name in existing:
            if name.casefold() == wanted_key:
                return name

        return wanted

    @staticmethod
    def _category_for(item):
        """Return one high-confidence category, or None."""
        name = item["name"].casefold()
        extension = item["extension"].casefold()

        if name in {value.casefold() for value in _PROTECTED_AUTO_NAMES}:
            return None

        if name.startswith("jarvis-log-") and name.endswith(".txt"):
            return None

        # "report" is deliberately checked as a filename word rather than
        # matching any filename containing "report" (for example, report.py
        # is still a report, but reporting.py should remain Code).
        report_words = {
            token.strip("._- ")
            for token in name.replace(".", " ").replace("_", " ").replace("-", " ").split()
        }

        if "report" in report_words:
            return "Reports"

        for category, extensions in _CATEGORIES:
            if extension in extensions:
                return category

        return None

    def _plan(self, inventory):
        """Build a conservative local move plan from the inspected inventory."""
        groups = {}
        existing = inventory["directories"]
        existing_keys = {name.casefold() for name in existing}

        for item in inventory["files"]:
            category = self._category_for(item)

            if not category:
                continue

            folder = self._existing_folder(existing, category)
            groups.setdefault(folder, []).append(item["name"])

        return [
            {
                "folder": folder,
                "existing": folder.casefold() in existing_keys,
                "files": names,
            }
            for folder, names in groups.items()
            if names
        ]

    @staticmethod
    def _summary(groups, result):
        """Create the spoken result from the already validated local plan."""
        moved = int(result.get("moved", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        created = tuple(result.get("created") or ())

        if moved == 0:
            return "Your JARVIS folder is in good order, sir."

        planned = sum(len(group["files"]) for group in groups)

        if moved == planned:
            destinations = "; ".join(
                f"{len(group['files'])} into {group['folder']}"
                for group in groups
                if group["files"]
            )
            message = f"Checking your JARVIS folder, sir. I moved {destinations}."
        else:
            message = (
                f"Checking your JARVIS folder, sir. I moved {moved} "
                f"file{'s' if moved != 1 else ''}."
            )

        if created:
            count = len(created)
            message += (
                f" I created {count} new "
                f"folder{'s' if count != 1 else ''}."
            )

        if skipped:
            message += (
                f" I left {skipped} item"
                f"{'s' if skipped != 1 else ''} untouched."
            )

        return message

    def check(self, *, startup=False):
        """Inspect and, when safe, organise the JARVIS root locally."""
        if not self.enabled():
            return None

        busy = self._busy_checker

        if busy is not None:
            try:
                if busy():
                    return {"status": "deferred"}
            except Exception as error:
                print(f"[JARVIS] folder guard busy check failed: {error}")
                return None

        root = self._root()

        if not root or not os.path.isdir(root):
            return None

        try:
            inventory = folder_organizer._inventory(root)
        except Exception as error:
            print(f"[JARVIS] folder guard inspection failed: {error}")
            return None

        if not inventory or not inventory["files"]:
            if startup:
                self._announce(
                    "Your JARVIS folder is in good order, sir.",
                    open_follow_up=False,
                )

            return {"status": "clean"}

        groups = self._plan(inventory)

        if not groups:
            if startup:
                self._announce(
                    "Your JARVIS folder is in good order, sir.",
                    open_follow_up=False,
                )

            return {"status": "clean"}

        try:
            result = folder_organizer._execute(root, groups)
        except Exception as error:
            print(f"[JARVIS] folder guard execution failed: {error}")
            return None

        moved = int((result or {}).get("moved", 0) or 0)

        if moved <= 0:
            if startup:
                self._announce(
                    "Your JARVIS folder is in good order, sir.",
                    open_follow_up=False,
                )

            return result

        text = self._summary(groups, result)
        self._announce(text, open_follow_up=True)

        return {
            **result,
            "status": "organised",
            "startup": startup,
        }

    def _announce(self, text, *, open_follow_up):
        listener = self._listener

        if listener is None:
            return

        try:
            listener(text)
        except Exception as error:
            print(f"[JARVIS] folder guard announcement failed: {error}")
            return

        if open_follow_up and self._follow_up_listener is not None:
            try:
                self._follow_up_listener()
            except Exception as error:
                print(
                    f"[JARVIS] folder guard follow-up failed: {error}"
                )

    def start(self):
        """Start periodic checks, including one immediate startup check."""
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()

        if self.enabled():
            self.check(startup=True)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(_POLL_SECONDS)

            if self._stop.is_set() or not self.enabled():
                continue

            result = self.check(startup=False)

            if result and result.get("status") == "deferred":
                continue


folder_guard = FolderGuard()
