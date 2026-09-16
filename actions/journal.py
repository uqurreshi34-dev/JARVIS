"""A plain record of what JARVIS did.

When something surprises you, this is where you find out what happened. It
is deliberately dull: one line per event, human readable, in the JARVIS
folder alongside everything else, so it can be opened in any editor.

Writing is best-effort. A journal that cannot be written must never stop
JARVIS from working, so every failure here is swallowed after one warning.
"""

import os
import re
import threading
from datetime import datetime, timedelta

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


def command(spoken, intent, free_path, detail=None):
    """Record a command as it is understood, including an optional subject."""
    route = "local" if free_path else "model"

    line = f"{spoken!r} -> {intent}"

    if detail:
        line += f" [{detail}]"

    line += f" ({route})"

    write("command", line)


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
    "make_note": "make a note",
    "append_file": "add to {detail}",
    "create_file": "create {detail}",
    "copy_file": "copy {detail}",
    "file_to_clipboard": "copy {detail} to your clipboard",
    "clear_clipboard": "clear your clipboard",
    "copy_to_clipboard": "copy something to your clipboard",
    "save_picture": "save a picture",
    "save_chart": "save a chart",
    "save_image": "save an image to your JARVIS images folder",
    "click_thing": "click {detail}",
    "add_event": "add {detail} to your calendar",
    "market_report": "write {detail}",
    "set_market_alert": "watch {detail}",
    "remove_event": "remove {detail} from your calendar",
    "clear_calendar": "clear your calendar",
    "proofread_fix": "correct the spelling in {detail}",
    "proofread_report": "write a spelling report for {detail}",
    "proofread_copy": "copy a corrected version of {detail}",
    "ignore_word": "add {detail} to the ignore list",
    "page_to_file": "save a web page to your JARVIS folder",
    "type_text": "type something",
    "git_commit": "commit your staged changes: {detail}",
    "open_application": "open {detail}",
    "close_application": "close {detail}",
    "open_folder": "open your {detail} folder",
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
        if "{detail}" not in template:
            return template

        # With no detail, drop the trailing placeholder and any dangling
        # preposition, so "add to {detail}" becomes "add something".
        if not tidy or tidy.replace("_", " ") == intent.replace("_", " "):
            bare = template.replace("{detail}", "").strip()

            if bare.endswith((" to", " from", " in", " on")):
                bare = f"{bare} something"

            return bare or intent.replace("_", " ")

        # The detail sometimes already carries the preposition ("bread to
        # shopping list"), which would otherwise read "add to bread to...".
        for word in (" to", " from", " in", " on"):
            if template.endswith(f"{word} {{detail}}") and (
                f"{word.strip()} " in tidy
            ):
                return f"{template.split(word + ' {detail}')[0]} {tidy}".strip()

        return template.format(detail=tidy)

    # Unknown intent: at least say it as words rather than an identifier.
    words = intent.replace("_", " ")

    return f"{words} {tidy}".strip()


def browser(what, where, outcome=None):
    """Record a browser step: every navigation, every page read.

    Deliberately not action(): browsing is chatty, and a page read is not
    the kind of thing worth answering "what did you do?" with. This writes
    the audit trail without touching _last_spoken, so "what did you do"
    still names the last real change rather than the last page visited.
    """
    write("browser", f"{what}: {where}", outcome)


def refused(reason, detail):
    """Record something JARVIS declined to do."""
    write("refused", f"{reason}: {detail}")


def alert(text):
    """Record something JARVIS said unprompted."""
    write("alert", text)


_COMMAND_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}\s+command\s+"
    r"'.*?' -> (\w+)(?: \[(.*?)\])? \(\w+\)$"
)


def command_history(days=30):
    """(date, hour, minute, intent, detail) for recent logged commands.

    Reads both the live journal and rotated archives. Older log entries
    without a subject simply return None for detail.
    """
    base = files.root()

    if not base:
        return []

    try:
        names = [FILENAME] + sorted(
            name
            for name in os.listdir(base)
            if name.startswith("jarvis-log-") and name.endswith(".txt")
        )
    except OSError:
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    entries = []

    for name in names:
        path = os.path.join(base, name)

        try:
            with open(
                path, "r", encoding="utf-8", errors="ignore"
            ) as handle:
                for line in handle:
                    match = _COMMAND_LINE.match(line.strip())

                    if not match:
                        continue

                    date, hour, minute, intent, detail = match.groups()

                    if date >= cutoff:
                        entries.append(
                            (
                                date,
                                int(hour),
                                int(minute),
                                intent,
                                detail or None,
                            )
                        )
        except OSError:
            continue

    return entries


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


def summary():
    """How much has happened today, without reading it all out."""
    path = _path()

    if not path or not os.path.exists(path):
        return "There's nothing in the log yet, sir."

    today = datetime.now().strftime("%Y-%m-%d")

    commands = 0
    actions = 0
    refusals = 0

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.startswith(today):
                    continue

                if "  command " in line:
                    commands += 1
                elif "  action " in line:
                    actions += 1
                elif "  refused " in line:
                    refusals += 1

    except OSError:
        return "I couldn't read the log, sir."

    if not commands and not actions:
        return "Nothing so far today, sir."

    parts = [f"{commands} commands today, sir"]

    if actions:
        word = "change" if actions == 1 else "changes"
        parts.append(f"{actions} {word}")

    if refusals:
        parts.append(f"{refusals} declined")

    spoken = ", ".join(parts)

    last = _last_spoken.get("text")

    if last:
        return f"{spoken}. The last was to {last}."

    return f"{spoken}."


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
