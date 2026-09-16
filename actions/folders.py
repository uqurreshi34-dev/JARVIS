"""Open a folder inside the JARVIS folder, or say why it cannot be.

JARVIS already writes into subfolders of the JARVIS folder -- the folder
organiser sorts files into Documents, Images, Reports and whatever else it
decides on. Until now there was no way to ask him to open one.

Two rules shape everything here:

  - A folder is never created. "open the reports folder" is a request to
    look at something, and creating an empty folder in answer to it is a
    silent lie. files.folder_path() creates on demand, which is right for
    writing and wrong here, so this module does its own lookup.

  - Nothing outside the JARVIS folder is reachable. The name is matched
    against the real listing rather than joined onto a path, so "..\\..\\"
    matches no folder and opens nothing.

Matching is against whatever is really on disk, so "the technology folder"
finds Technologies without anything here knowing that word.
"""

import os
import subprocess
import sys
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse

import phrases
from actions import files

try:
    import win32com.client as win32com_client
except ImportError:
    win32com_client = None


# A spoken name and a folder name rarely agree exactly: "technology" for
# Technologies, "report" for Reports, "doc" for Documents. This is the
# same bar memory_subjects uses for a subject label, for the same reason
# -- high enough that two real folders are not confused for each other.
_MIN_SIMILARITY = 0.72

# The winner must be this far clear of the runner-up. With Documents and
# Downloads both present, "do" names neither, and opening a guess is worse
# than asking.
_MIN_MARGIN = 0.12

# Words that describe the request rather than the folder.
_NOISE = frozenset({
    "the", "a", "an", "my", "our", "your", "please", "up",
    "folder", "folders", "directory", "directories", "dir",
})


def _tokens(text):
    cleaned = "".join(
        character if character.isalnum() else " "
        for character in str(text or "").casefold()
    )

    return [word for word in cleaned.split() if word]


def _identity(text):
    return " ".join(token for token in _tokens(text) if token not in _NOISE)


def folders():
    """Every folder directly inside the JARVIS folder, sorted by name."""
    base = files.root()

    if not base:
        return []

    try:
        return sorted(
            entry.name
            for entry in os.scandir(base)
            if entry.is_dir() and not entry.name.startswith(".")
        )

    except OSError as error:
        print(f"[JARVIS] could not list folders: {error}")
        return []


def listing():
    """Folder names, newest first.

    Same ordering as files.listing(), so "what folders do I have" reads
    the way "what files do I have" already does.
    """
    base = files.root()

    if not base:
        return []

    names = folders()

    try:
        names.sort(
            key=lambda name: os.path.getmtime(os.path.join(base, name)),
            reverse=True,
        )
    except OSError:
        return sorted(names)

    return names


def describe_listing(limit=4, names=True):
    """Spoken summary of the folders, mirroring files.describe_listing.

    The true total is always given; only the most recent few are named,
    because reading twenty folder names aloud would be unbearable.
    """
    entries = listing()

    if not entries:
        return "You have no folders in your JARVIS folder, sir."

    if not names:
        if len(entries) == 1:
            return "One folder, sir."

        return f"You have {phrases.number(len(entries))} folders, sir."

    if len(entries) == 1:
        return f"One folder, sir: {entries[0]}."

    shown = entries[:limit]
    listed = ", ".join(shown[:-1]) + f", and {shown[-1]}"

    if len(entries) <= limit:
        return (
            f"You have {phrases.number(len(entries))} folders, sir: "
            f"{listed}."
        )

    return (
        f"You have {phrases.number(len(entries))} folders, sir. I won't "
        f"list them all, but your {phrases.number(len(shown))} most "
        f"recent are {listed}."
    )


def describe_listing_count():
    """Just the number, for "how many folders do I have"."""
    return describe_listing(names=False)


def find(name):
    """The real folder a spoken name refers to, or None.

    Matched against the listing, never joined onto a path, so nothing
    outside the JARVIS folder is reachable however the name is spelled.
    """
    wanted = _identity(name)

    if not wanted:
        return None

    existing = folders()

    if not existing:
        return None

    scored = []

    for folder in existing:
        candidate = _identity(folder)

        if not candidate:
            continue

        if candidate == wanted:
            return folder

        # A spoken word inside the folder name counts as a match:
        # "technology" for "Technologies", "report" for "Old Reports".
        contained = (
            wanted in candidate
            or candidate in wanted
        )

        score = SequenceMatcher(None, wanted, candidate).ratio()

        if contained:
            score = max(score, _MIN_SIMILARITY)

        scored.append((score, folder))

    scored.sort(reverse=True)

    if not scored or scored[0][0] < _MIN_SIMILARITY:
        return None

    if len(scored) > 1 and scored[0][0] - scored[1][0] < _MIN_MARGIN:
        # Two folders are equally close. Guessing between them opens the
        # wrong one silently, so decline and let the caller say what
        # exists.
        return None

    return scored[0][1]


def describe_available():
    """What folders there are, for when the asked-for one is not among them."""
    existing = folders()

    if not existing:
        return "There are no folders in there yet."

    if len(existing) == 1:
        return f"The only one in there is {existing[0]}."

    names = ", ".join(existing[:-1])

    return f"You have {names} and {existing[-1]}."


def open_folder(name=None):
    """Open a folder in the file browser. Returns a spoken reply.

    With no name, opens the JARVIS folder itself.
    """
    base = files.root()

    if not base:
        return "I can't reach your JARVIS folder, sir."

    if name is None or not _identity(name) or _identity(name) in {
        "jarvis", "jarvis root", "root",
    }:
        target = base
        spoken = "your JARVIS folder"
    else:
        folder = find(name)

        if not folder:
            # The spoken name with "folder" and "the" already stripped, so
            # the reply does not come out as "no music folder folder".
            asked = _identity(name) or str(name).strip()

            return (
                f"There's no {asked} folder in your JARVIS folder, sir. "
                f"{describe_available()}"
            )

        target = os.path.join(base, folder)
        spoken = f"the {folder} folder"

    if not os.path.isdir(target):
        return (
            f"That folder has gone from your JARVIS folder, sir. "
            f"{describe_available()}"
        )

    if not _reveal(target):
        return f"I couldn't open {spoken}, sir."

    return f"Opening {spoken}, sir."


def _open_explorer_windows():
    """Open Explorer windows as {folder path: window}, newest last.

    Uses the Shell COM object, the same interface applications.py already
    reaches for. Without pywin32 -- or on anything but Windows -- this is
    empty, and closing a folder reports that nothing is open rather than
    failing.
    """
    if win32com_client is None:
        return {}

    found = {}

    try:
        shell = win32com_client.Dispatch("Shell.Application")

        for window in shell.Windows():
            try:
                location = window.LocationURL
            except Exception:
                continue

            if not location:
                continue

            path = unquote(urlparse(location).path).lstrip("/")
            path = os.path.normpath(path.replace("/", os.sep))

            if path:
                found[os.path.normcase(path)] = window

    except Exception as error:
        print(f"[JARVIS] could not list open folders: {error}")
        return {}

    return found


def close_folder(name=None):
    """Close an open Explorer window. Returns a spoken reply.

    With no name, closes the JARVIS folder itself.
    """
    base = files.root()

    if not base:
        return "I can't reach your JARVIS folder, sir."

    if name is None or not _identity(name) or _identity(name) in {
        "jarvis", "jarvis root", "root",
    }:
        target = base
        spoken = "your JARVIS folder"
    else:
        folder = find(name)

        if not folder:
            asked = _identity(name) or str(name).strip()

            return (
                f"There's no {asked} folder in your JARVIS folder, sir. "
                f"{describe_available()}"
            )

        target = os.path.join(base, folder)
        spoken = f"the {folder} folder"

    windows = _open_explorer_windows()
    window = windows.get(os.path.normcase(os.path.normpath(target)))

    if window is None:
        return f"{spoken[0].upper()}{spoken[1:]} isn't open, sir."

    try:
        window.Quit()
    except Exception as error:
        print(f"[JARVIS] could not close {target}: {error}")
        return f"I couldn't close {spoken}, sir."

    return f"Closing {spoken}, sir."


def _reveal(path):
    """Show a folder in the desktop file browser. True when it opened."""
    try:
        # Looked up rather than called directly: os.startfile only exists on
        # Windows, so naming it outright fails a linter run anywhere else.
        start = getattr(os, "startfile", None)

        if start is not None:
            start(path)
            return True

        opener = "open" if sys.platform == "darwin" else "xdg-open"

        subprocess.Popen(
            [opener, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return True

    except OSError as error:
        print(f"[JARVIS] could not open {path}: {error}")
        return False
