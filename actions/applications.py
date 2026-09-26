from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import os
import re
import subprocess
import time

import psutil
import win32api
import win32con
import win32gui
import win32process

try:
    import win32com.client as win32com_client
except ImportError:
    win32com_client = None


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_DRIVE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")
_TOKEN = re.compile(r"[a-z0-9]+")

_STOP_TOKENS = frozenset({
    "open", "close", "launch", "start", "run", "quit", "exit",
    "kill", "shut", "down", "the", "and", "app", "application",
    "please", "for", "exe", "lnk", "url",
})

_MIN_TOKEN_LENGTH = 3


_CLOSE_TIMEOUT = 5.0
_TERMINATE_TIMEOUT = 3.0
_POLL_INTERVAL = 0.2


def _significant_tokens(text):
    return frozenset(
        token
        for token in _TOKEN.findall((text or "").casefold())
        if len(token) >= _MIN_TOKEN_LENGTH and token not in _STOP_TOKENS
    )


def _classify(app_id):
    value = (app_id or "").strip()

    if _URL_SCHEME.match(value):
        return "url", value

    if _DRIVE_PATH.match(value) or value.startswith("\\\\"):
        lowered = value.casefold()
        if lowered.endswith(".url"):
            return "internet_shortcut", value
        if lowered.endswith(".lnk"):
            return "shortcut", value
        return "path", value

    return "aumid", value


@dataclass(frozen=True)
class Application:
    name: str
    app_id: str
    kind: str
    target: str
    tokens: frozenset


class ApplicationManager:
    def __init__(self):
        self._applications = self._load_applications()

    @property
    def applications(self):
        return tuple(application.name for application in self._applications)

    def _load_applications(self):
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            check=True,
            # No console window flashing up: the installed JARVIS.exe has
            # none of its own, so PowerShell would open one each time.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        payload = result.stdout.strip()

        if not payload:
            return ()

        data = json.loads(payload)

        if isinstance(data, dict):
            data = [data]

        applications = []

        for entry in data:
            name = (entry.get("Name") or "").strip()
            app_id = (entry.get("AppID") or "").strip()

            if not name or not app_id:
                continue

            kind, target = _classify(app_id)

            applications.append(
                Application(
                    name=name,
                    app_id=app_id,
                    kind=kind,
                    target=target,
                    tokens=_significant_tokens(name),
                )
            )

        return tuple(applications)

    def find(self, name):
        query = (name or "").casefold().strip()

        if not query:
            return None

        for app in self._applications:
            if app.name.casefold() == query:
                return app

        for app in self._applications:
            if query in app.name.casefold():
                return app

        return None

    def candidates(self, query, limit=5):
        raw = (query or "").casefold().strip()

        cleaned = " ".join(
            token
            for token in _TOKEN.findall(raw)
            if token not in _STOP_TOKENS
        ) or raw

        query_tokens = _significant_tokens(raw)

        scored = []

        for application in self._applications:
            name = application.name.casefold()

            ratio = max(
                SequenceMatcher(None, cleaned, name).ratio(),
                SequenceMatcher(None, raw, name).ratio(),
            )

            overlap = self._token_overlap(query_tokens, application.tokens)

            scored.append((ratio + overlap, application.name))

        scored.sort(key=lambda item: item[0], reverse=True)

        seen = set()
        ordered = []

        for _, name in scored:
            if name in seen:
                continue

            seen.add(name)
            ordered.append(name)

            if len(ordered) == limit:
                break

        return tuple(ordered)

    @staticmethod
    def _token_overlap(query_tokens, app_tokens):
        if not query_tokens or not app_tokens:
            return 0.0

        hits = 0

        for token in app_tokens:
            if any(
                token == other or token in other or other in token
                for other in query_tokens
            ):
                hits += 1

        return float(hits)

    def launch(self, name):
        app = self.find(name)

        if not app:
            return False

        try:
            self._activate(app)
        except OSError as error:
            print(f"[JARVIS] launch failed for {app.name!r}: {error}")
            return False

        return True

    def _activate(self, app):
        if app.kind == "url":
            os.startfile(app.target)
            return

        if app.kind == "internet_shortcut":
            url = self._read_internet_shortcut(app.target)
            os.startfile(url or app.target)
            return

        if app.kind in ("shortcut", "path"):
            os.startfile(app.target)
            return

        subprocess.Popen(
            [
                "explorer.exe",
                f"shell:AppsFolder\\{app.target}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def is_running(self, name):
        """Whether the program has a window open now."""
        app = self.find(name)
        return bool(app and self._match_processes(app))

    def close(self, name):
        app = self.find(name)

        if not app:
            return False

        matches = self._match_processes(app)

        if not matches:
            return False

        targets = [
            hwnd for record in matches.values() for hwnd in record["windows"]
        ]

        for hwnd in targets:
            self._post_close(hwnd)

        # Success is the window going away, not the process exiting. Packaged
        # apps run inside a shared host that outlives them, so waiting for
        # the process would report failure on every UWP application.
        gone = self._wait_for_windows(targets, _CLOSE_TIMEOUT)

        if gone:
            return True

        # The windows are still there, so fall back to ending the processes
        # that matched on identity rather than on a window title.
        escalated = False

        for pid, record in matches.items():
            if record["strong"] and psutil.pid_exists(pid):
                self._terminate_tree(pid)
                escalated = True

        if not escalated:
            return False

        return self._wait_for_windows(targets, _TERMINATE_TIMEOUT)

    @staticmethod
    def _wait_for_windows(handles, timeout):
        """True once every window has closed, or False on timeout."""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if not any(win32gui.IsWindow(hwnd) for hwnd in handles):
                return True

            time.sleep(_POLL_INTERVAL)

        return not any(win32gui.IsWindow(hwnd) for hwnd in handles)

    def _match_processes(self, app):
        windows_by_pid = {}
        titles_by_pid = {}

        for hwnd, pid, title in self._enumerate_windows():
            windows_by_pid.setdefault(pid, []).append((hwnd, title))

            if title:
                titles_by_pid.setdefault(pid, []).append(title)

        matches = {}

        for pid, entries in windows_by_pid.items():
            titles = titles_by_pid.get(pid, [])
            identity = self._identity_match(app, pid)

            if identity:
                titled = [hwnd for hwnd, title in entries if title]

                matches[pid] = {
                    "windows": titled or [hwnd for hwnd, _ in entries],
                    "strong": True,
                }

                continue

            # No identity match, so fall back to window titles. Only the
            # windows that actually match are closed, never the whole
            # process, which is what makes this safe for a browser hosting
            # an installed web app alongside ordinary tabs.
            titled = [
                hwnd for hwnd, title in entries
                if title and self._title_matches(app, title)
            ]

            if titled:
                matches[pid] = {"windows": titled, "strong": False}

        # An identity match is better evidence than a title, so if any exists
        # the title matches are discarded.
        strong = {
            pid: record for pid, record in matches.items() if record["strong"]
        }

        return strong or matches

    def _title_matches(self, app, title):
        lowered = title.casefold()

        return all(
            re.search(r"\b%s\b" % re.escape(token), lowered)
            for token in app.tokens
        )

    def _identity_match(self, app, pid):
        """True when the process itself is this application.

        Judged on the executable name, its path, or its version metadata --
        never on a window title, which says nothing reliable about identity.
        """
        try:
            process = psutil.Process(pid)
            process_name = process.name()

            try:
                executable = process.exe()
            except (psutil.AccessDenied, psutil.Error):
                executable = ""

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

        stem = os.path.splitext(process_name)[0]

        if app.tokens <= _significant_tokens(stem):
            return True

        target_executable = self._target_executable(app)

        if target_executable and executable and self._same_file(
            target_executable, executable
        ):
            return True

        signature = self._product_signature(executable)

        if signature:
            signature_tokens = _significant_tokens(signature)

            if app.tokens <= signature_tokens:
                return True

        return False

    def _match_strength(self, app, pid, titles):
        """Diagnostics only: "strong", "weak", or None."""
        if self._identity_match(app, pid):
            return "strong"

        for title in titles:
            if self._title_matches(app, title):
                return "weak"

        return None

    def _target_executable(self, app):
        if app.kind == "path" and app.target.casefold().endswith(".exe"):
            return app.target

        if app.kind == "shortcut":
            return self._resolve_shortcut_target(app.target)

        return None

    @staticmethod
    def _same_file(first, second):
        try:
            return (
                os.path.normcase(os.path.realpath(first))
                == os.path.normcase(os.path.realpath(second))
            )
        except OSError:
            return False

    @staticmethod
    def _product_signature(path):
        if not path:
            return ""

        try:
            translations = win32api.GetFileVersionInfo(
                path,
                "\\VarFileInfo\\Translation",
            )
        except Exception:
            return ""

        if not translations:
            return ""

        language, codepage = translations[0]
        parts = []

        for field_name in ("ProductName", "FileDescription", "OriginalFilename"):
            key = "\\StringFileInfo\\%04X%04X\\%s" % (
                language, codepage, field_name)

            try:
                value = win32api.GetFileVersionInfo(path, key)
            except Exception:
                value = None

            if value:
                parts.append(value)

        return " ".join(parts).casefold()

    @staticmethod
    def _resolve_shortcut_target(path):
        if not win32com_client:
            return None

        try:
            shell = win32com_client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(path)
            return shortcut.TargetPath or None
        except Exception:
            return None

    @staticmethod
    def _read_internet_shortcut(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.lower().startswith("url="):
                        return line.partition("=")[2].strip()
        except OSError:
            return None

        return None

    @staticmethod
    def _enumerate_windows():
        collected = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)

            collected.append((hwnd, pid, title))

        win32gui.EnumWindows(callback, None)

        return collected

    @staticmethod
    def _post_close(hwnd):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass

    @staticmethod
    def _wait_for_exit(pids, timeout):
        alive = {pid for pid in pids if psutil.pid_exists(pid)}
        deadline = time.monotonic() + timeout

        while alive and time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL)
            alive = {pid for pid in alive if psutil.pid_exists(pid)}

        return alive

    @staticmethod
    def _terminate_tree(pid):
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return

        try:
            processes = parent.children(recursive=True)
        except psutil.Error:
            processes = []

        processes.append(parent)

        for process in processes:
            try:
                process.terminate()
            except psutil.Error:
                pass

        _, alive = psutil.wait_procs(processes, timeout=_TERMINATE_TIMEOUT)

        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass


if __name__ == "__main__":
    import sys

    manager = ApplicationManager()
    needle = " ".join(sys.argv[1:]).casefold().strip()

    for application in manager._applications:
        if needle and needle not in application.name.casefold():
            continue

        print(f"{application.name!r}")
        print(f"  app_id: {application.app_id!r}")
        print(f"  kind:   {application.kind}")
        print(f"  target: {application.target!r}")
        print()
