"""A plain record of what JARVIS did.

When something surprises you, this is where you find out what happened. It
is deliberately dull: one line per event, human readable, in the JARVIS
folder alongside everything else, so it can be opened in any editor.

Writing is best-effort. A journal that cannot be written must never stop
JARVIS from working, so every failure here is swallowed after one warning.
"""

import os
import threading
from datetime import datetime

from actions import files


FILENAME = "jarvis-log.txt"

# Once the live log passes this, it is moved aside rather than trimmed, and
# a fresh one started. Nothing is ever discarded: the point of an audit
# trail is that the old entries are still there when you need them.
MAX_LINES = 5000

ARCHIVE_NAME = "jarvis-log-{stamp}.txt"

# How many archives to keep before the oldest is removed.
MAX_ARCHIVES = 10

_lock = threading.Lock()
_warned = False


def _path():
    base = files.root()

    return os.path.join(base, FILENAME) if base else None


def write(kind, detail, outcome=None):
    """Record one event. Never raises."""
    global _warned

    path = _path()

    if not path:
        return

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    line = f"{stamp}  {kind:<16} {detail}"

    if outcome is not None:
        line += f"  -> {outcome}"

    try:
        with _lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")

            _trim(path)

    except OSError as error:
        if not _warned:
            print(f"[JARVIS] could not write the journal: {error}")
            _warned = True


def _trim(path):
    """Move a full log aside and start a fresh one. Nothing is discarded."""
    try:
        if os.path.getsize(path) < 400_000:
            return

        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()

        if len(lines) <= MAX_LINES:
            return

        base = files.root()

        if not base:
            return

        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        archive = os.path.join(base, ARCHIVE_NAME.format(stamp=stamp))

        os.replace(path, archive)

        print(f"[JARVIS] journal archived to {os.path.basename(archive)}")

        _prune_archives(base)

    except OSError:
        pass


def _prune_archives(base):
    """Keep only the most recent archives, so the folder stays tidy."""
    try:
        archives = sorted(
            name for name in os.listdir(base)
            if name.startswith("jarvis-log-") and name.endswith(".txt")
        )

        for name in archives[:-MAX_ARCHIVES]:
            os.remove(os.path.join(base, name))

    except OSError:
        pass


def command(spoken, intent, free_path):
    """Record a command as it is understood."""
    route = "local" if free_path else "model"

    write("command", f"{spoken!r} -> {intent} ({route})")


def action(intent, detail, succeeded, spoken=None):
    """Record something that changed the machine or a file.

    `detail` is for the file, `spoken` is how it should be read aloud. When
    no spoken form is given one is worked out from the intent, so the log is
    never read back as "remove underscore note".
    """
    write("action", f"{intent}: {detail}", "ok" if succeeded else "failed")

    _last_spoken["text"] = spoken or _phrase_for(intent, detail)
    _last_spoken["ok"] = succeeded


# How the most recent action should be said aloud.
_last_spoken = {"text": None, "ok": True}

# Turning an intent name into something a person would say.
_VERBS = {
    "remove_note": "remove {detail} from your notes",
    "remove_line": "remove {detail}",
    "clear_notes": "clear your notes",
    "make_note": "note down {detail}",
    "append_file": "add to {detail}",
    "create_file": "create {detail}",
    "copy_file": "copy {detail}",
    "file_to_clipboard": "copy {detail} to your clipboard",
    "clear_clipboard": "clear your clipboard",
    "copy_to_clipboard": "copy {detail} to your clipboard",
    "save_picture": "save a picture",
    "save_chart": "save the chart",
    "plot_chart": "plot {detail}",
    "hide_chart": "close the chart",
    "take_screenshot": "take a screenshot",
    "click_thing": "click {detail}",
    "type_text": "type {detail}",
    "open_application": "open {detail}",
    "close_application": "close {detail}",
}


def _tidy(detail):
    """Strip the quoting and file wording out of a logged detail."""
    text = str(detail or "").strip()

    text = text.replace("'", "").replace('"', "")

    for noise in (" from notes", " from your notes"):
        text = text.replace(noise, "")

    return text.strip()


def _phrase_for(intent, detail):
    """A spoken phrase for an intent, falling back to plain words."""
    template = _VERBS.get(intent)
    tidy = _tidy(detail)

    if template:
        return template.format(detail=tidy) if "{detail}" in template else template

    # Unknown intent: at least say it as words rather than an identifier.
    words = intent.replace("_", " ")

    return f"{words} {tidy}".strip()


def refused(reason, detail):
    """Record something JARVIS declined to do."""
    write("refused", f"{reason}: {detail}")


def alert(text):
    """Record something JARVIS said unprompted."""
    write("alert", text)


def recent(count=12):
    """The last few lines, for reading back aloud."""
    path = _path()

    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = [line.strip() for line in handle if line.strip()]

    except OSError:
        return []

    return lines[-count:]


def describe(count=5):
    """A spoken summary of the most recent activity."""
    lines = recent(count)

    if not lines:
        return "There's nothing in the log yet, sir."

    spoken = _last_spoken.get("text")

    if not spoken:
        # Nothing recorded this session, so read the file instead.
        actions = [line for line in lines if "  action" in line]

        if not actions:
            return "I haven't changed anything yet, sir."

        body = actions[-1].split("action", 1)[-1].strip()
        intent, _, detail = body.partition(":")
        detail = detail.split("->")[0]
        spoken = _phrase_for(intent.strip(), detail)

    lead = "The last thing I did was" if _last_spoken.get("ok", True) else (
        "The last thing I tried was"
    )

    return (
        f"{lead} {spoken}, sir. The full log is in your JARVIS folder."
    )
