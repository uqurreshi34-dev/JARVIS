import os
from datetime import datetime


# Notes live alongside the screenshots, in a JARVIS folder in the user's home.
_FOLDER_NAME = "JARVIS"
_FILENAME = "notes.txt"

# Reading every note aloud gets tedious, so only the most recent are spoken.
_SPEAK_LIMIT = 5


def _notes_path():
    folder = os.path.join(os.path.expanduser("~"), _FOLDER_NAME)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not create {folder}: {error}")
        return None

    return os.path.join(folder, _FILENAME)


def add(text):
    """Append a note with a timestamp. Returns True on success."""
    note = " ".join((text or "").split())

    if not note:
        return False

    path = _notes_path()

    if not path:
        return False

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {note}\n")

    except OSError as error:
        print(f"[JARVIS] could not save the note: {error}")
        return False

    print(f"[JARVIS] note saved to {path}")

    return True


def _read_lines():
    path = _notes_path()

    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return [line.strip() for line in handle if line.strip()]

    except OSError as error:
        print(f"[JARVIS] could not read your notes: {error}")
        return []


def _without_stamp(line):
    """Drop the leading timestamp so it is not read aloud."""
    if line.startswith("[") and "]" in line:
        return line.split("]", 1)[1].strip()

    return line


def count():
    return len(_read_lines())


def describe():
    """Spoken summary of the most recent notes."""
    lines = _read_lines()

    if not lines:
        return "You have no notes, sir."

    recent = [_without_stamp(line) for line in lines[-_SPEAK_LIMIT:]]

    if len(lines) == 1:
        return f"One note, sir: {recent[0]}."

    if len(lines) <= _SPEAK_LIMIT:
        listed = "; ".join(recent)
        return f"You have {len(lines)} notes, sir: {listed}."

    listed = "; ".join(recent)

    return (
        f"You have {len(lines)} notes, sir. The most recent {len(recent)}: "
        f"{listed}."
    )


def clear():
    """Delete every note. Returns how many were removed."""
    lines = _read_lines()

    if not lines:
        return 0

    path = _notes_path()

    if not path:
        return 0

    try:
        os.remove(path)
    except OSError as error:
        print(f"[JARVIS] could not clear your notes: {error}")
        return 0

    return len(lines)
