"""The file hologram: your JARVIS folder, projected beside the HUD, by number.

"Jarvis, show me my files" projects the JARVIS folder as numbered cards --
folders first, then files, newest first -- so nothing has to be said by
name, which speech recognition mangles. Then, while it is showing:

    what's file three            what it is: a folder of twelve items, a PDF of 4 MB
    summarise file three         its first three lines, on the hologram and aloud
    what's in / what is file three about   the same
    open three                   a folder: step inside; a file: open it
    go back                      up a folder
    next page / previous page    ten at a time
    close the files              put it away

Numbers may be said as words or digits, "number three", "the third": they
stay with a file across pages, so file eleven is always file eleven.

Free throughout: listing and the first lines are read from disk, with no
model call. Nothing outside the JARVIS folder is shown, JARVIS's own files
are not listed, and a file holding keys or a sign-in is never listed or
read. Programs and scripts are listed but never run from here.

Nothing here touches a window. It works out what to show and hands it to
the panel (files_panel.py) through a listener, as aircraft.py does for the
radar.
"""

import math
import os
import re
import threading
import time
from pathlib import Path

import phrases
from actions import files


PAGE_SIZE = 10
PREVIEW_LINES = 3
PREVIEW_CHARS = 90

# Read from a text file for its first lines; enough for any opening.
_HEAD_BYTES = 16 * 1024

# Never listed and never read: keys, tokens and sign-ins.
_SECRET = re.compile(r"^(?:mcp\.json(?:\..+)?|outlook_token_cache\.json|\.env(?:\..+)?)$", re.IGNORECASE)

# Listed, but opening one would run it rather than show it.
_RUNS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".msi", ".msp", ".scr", ".pif", ".lnk", ".url", ".reg", ".hta",
    ".py", ".pyw", ".jar", ".cpl", ".appref-ms",
})

_KINDS = {
    "document": (".docx", ".doc", ".odt", ".rtf"),
    "PDF": (".pdf",),
    "text file": (".txt", ".md", ".log"),
    "spreadsheet": (".csv", ".xlsx", ".xls", ".ods"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"),
    "video": (".mp4", ".mov", ".mkv", ".avi", ".webm"),
    "sound": (".mp3", ".wav", ".m4a", ".flac", ".ogg"),
    "data file": (".json", ".xml", ".yaml", ".yml", ".sqlite", ".db"),
    "archive": (".zip", ".7z", ".rar", ".tar", ".gz"),
    "code": (".py", ".js", ".ts", ".html", ".css", ".java", ".cs", ".cpp", ".c", ".ino"),
    "3D model": (".blend", ".glb", ".gltf", ".obj", ".fbx", ".stl"),
}

_TEXT = (".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts",
         ".html", ".css", ".java", ".cs", ".cpp", ".c", ".ino", ".ini", ".cfg")


def kind_of(name):
    suffix = os.path.splitext(name)[1].casefold()

    for kind, suffixes in _KINDS.items():
        if suffix in suffixes:
            return kind

    return f"{suffix.lstrip('.').upper()} file" if suffix else "file"


# ---- numbers, as they are said ----------------------------------------------------

_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50,
}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}


# How speech recognition sometimes writes a number. Believed only straight
# after "file" or "number". Not "for": "what is the file for" is a question
# about purpose, and taking it for file four would answer the wrong thing.
_SOUNDS_LIKE = {"won": 1, "to": 2, "too": 2, "tree": 3, "ate": 8}

_BEFORE_A_NUMBER = ("file", "number", "item", "folder", "card")


def number_in(words):
    """The number said in [words] (a list), or None: "3", "three", "twenty one", "third"."""
    for index, word in enumerate(words):
        if word in _SOUNDS_LIKE and index and words[index - 1] in _BEFORE_A_NUMBER:
            return _SOUNDS_LIKE[word]

        if word.isdigit():
            return int(word)

        if word in _ORDINALS:
            return _ORDINALS[word]

        if word in _WORDS:
            value = _WORDS[word]

            # "twenty one": a tens word followed by a unit.
            if value >= 20 and index + 1 < len(words) and words[index + 1] in _WORDS and _WORDS[words[index + 1]] < 10:
                value += _WORDS[words[index + 1]]

            return value

    return None


def spoken_number(value):
    """A number as a word, so the voice says "three", not "3"."""
    return phrases.number(value)


# ---- what is showing ----------------------------------------------------------------

_lock = threading.Lock()
_view = None           # {"folder": relative path, "items": [...], "page": int, "focus": int|None}
_show_listener = None
_hide_listener = None


def set_listeners(on_view=None, on_hide=None):
    """Who is told what to show (a dict), and when to put it away."""
    global _show_listener, _hide_listener
    _show_listener = on_view
    _hide_listener = on_hide


def showing():
    with _lock:
        return _view is not None


def _publish():
    with _lock:
        view = _render(_view) if _view is not None else None

    if view is not None and _show_listener:
        try:
            _show_listener(view)
        except Exception as error:
            print(f"[JARVIS] could not show the files: {error}")


def _folder_path(relative):
    """The folder on disk for [relative], never outside the JARVIS folder."""
    base = files.root()

    if not base:
        return None

    path = os.path.realpath(os.path.join(base, relative)) if relative else os.path.realpath(base)
    root = os.path.realpath(base)

    if path != root and not path.startswith(root + os.sep):
        return None

    return path if os.path.isdir(path) else None


def _listed(relative):
    """Every folder and file to show in [relative]: folders by name, then files newest first."""
    path = _folder_path(relative)

    if not path:
        return []

    at_root = not relative

    try:
        from actions import folder_organizer
        protected = folder_organizer.is_protected
    except Exception:
        protected = lambda name: False  # noqa: E731

    folders, found = [], []

    try:
        entries = list(os.scandir(path))
    except OSError as error:
        print(f"[JARVIS] could not list {path}: {error}")
        return []

    for entry in entries:
        name = entry.name

        if name.startswith(".") or _SECRET.match(name):
            continue

        try:
            if entry.is_dir():
                try:
                    count = sum(1 for inner in os.scandir(entry.path) if not inner.name.startswith("."))
                except OSError:
                    count = 0

                folders.append({"kind": "folder", "name": name, "items": count,
                                "modified": entry.stat().st_mtime, "path": entry.path})

            elif entry.is_file():
                # JARVIS's own files live at the top of the folder; they are his, not yours.
                if at_root and protected(name):
                    continue

                stat = entry.stat()
                found.append({"kind": "file", "name": name, "type": kind_of(name), "size": stat.st_size,
                              "modified": stat.st_mtime, "path": entry.path})
        except OSError:
            continue

    folders.sort(key=lambda item: item["name"].casefold())
    found.sort(key=lambda item: item["modified"], reverse=True)

    items = folders + found

    for number, item in enumerate(items, start=1):
        item["number"] = number

    return items


def _size_text(size):
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= step:
            value = size / step
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"

    return f"{size} B"


def _spoken_size(size):
    for unit, step in (("gigabytes", 1 << 30), ("megabytes", 1 << 20), ("kilobytes", 1 << 10)):
        if size >= step:
            value = size / step
            words = f"{value:.1f}" if value < 10 else f"{value:.0f}"
            return f"{words.rstrip('0').rstrip('.') if '.' in words else words} {unit}"

    return f"{size} bytes"


def _age_text(modified, now=None):
    seconds = max(0, (now or time.time()) - modified)

    for unit, step in (("y", 365 * 86400), ("mo", 30 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= step:
            return f"{int(seconds // step)}{unit}"

    return "now"


def _spoken_age(modified, now=None):
    days = int(max(0, (now or time.time()) - modified) // 86400)

    if days == 0:
        return "today"

    if days == 1:
        return "yesterday"

    if days < 60:
        return f"{spoken_number(days)} days ago"

    return time.strftime("in %B %Y", time.localtime(modified))


def _render(view):
    """What the panel draws: the page of cards, and the one in focus."""
    items = view["items"]
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(view["page"], pages - 1)
    shown = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    largest = max([item.get("size", 0) for item in shown if item["kind"] == "file"] or [1])

    cards = []

    for item in shown:
        if item["kind"] == "folder":
            meta = f"FOLDER - {item['items']} ITEM{'S' if item['items'] != 1 else ''}"
            share = 0.0
        else:
            meta = f"{item['type'].upper()} - {_size_text(item['size'])} - {_age_text(item['modified'])}"
            # Bigger files, fuller bars; by order of size, so one huge video
            # does not flatten every other bar to nothing.
            share = math.log1p(item["size"]) / math.log1p(max(largest, 1))

        cards.append({"number": item["number"], "name": item["name"], "kind": item["kind"],
                      "type": item.get("type", "folder"), "meta": meta, "share": share})

    trail = ["JARVIS"] + [part for part in view["folder"].split("/") if part]

    return {
        "trail": trail,
        "cards": cards,
        "page": page,
        "pages": pages,
        "count": len(items),
        "folders": sum(1 for item in items if item["kind"] == "folder"),
        "focus": view.get("focus"),
    }


# ---- what was said ------------------------------------------------------------------------

def _words(text):
    return re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold().replace("'", "")).split()


_SHOW = re.compile(
    r"^(?:(?:jarvis|please|ok|okay)\s+)*(?:show|display|bring\s+up|pull\s+up|open)\s+(?:me\s+)?(?:all\s+)?"
    r"(?:my\s+|the\s+)?(?:jarvis\s+)?(?:files|file\s+hologram|files\s+hologram)(?:\s+please)?$"
)
_SHOW_IN = re.compile(
    r"^(?:(?:jarvis|please|ok|okay)\s+)*(?:show|display|bring\s+up|pull\s+up)\s+(?:me\s+)?(?:all\s+)?"
    r"(?:(?:the\s+)?files\s+in\s+(?:my\s+|the\s+)?(?P<a>.+?)(?:\s+folder)?|(?:my\s+|the\s+)?(?P<b>.+?)\s+(?:folder\s+)?files)(?:\s+please)?$"
)
_CLOSE = re.compile(
    r"^(?:(?:jarvis|please|ok|okay)\s+)*(?:close|hide|put\s+away|dismiss|clear)\s+(?:my\s+|the\s+)?"
    r"(?:files|file\s+hologram|files\s+hologram|hologram)(?:\s+please)?$"
)

_DESCRIBE = ("whats", "what", "which", "tell")
_PREVIEW_WORDS = ("summarise", "summarize", "summary", "sum", "preview", "read", "about", "in", "inside", "contents")
_OPEN_WORDS = ("open", "enter", "go", "into")


def asked(text):
    """What a command asks of the hologram, or None if it is not about it.

    ("show", folder), ("close", None); and only while it is showing:
    ("describe", n), ("preview", n), ("open", n), ("page", +1 or -1), ("back", None).
    """
    said = " ".join(_words(text))

    if not said:
        return None

    if _SHOW.match(said):
        return "show", ""

    shown_in = _SHOW_IN.match(said)

    if shown_in:
        spoken = shown_in.group("a") or shown_in.group("b")

        if spoken and spoken not in ("jarvis", "my", "all"):
            folder = _match_folder(spoken)

            if folder is not None:
                return "show", folder

    if not showing():
        return None

    if _CLOSE.match(said) or said in ("close", "close it", "hide it", "thats all", "done"):
        return "close", None

    words = [word for word in said.split() if word not in ("jarvis", "please", "the", "a", "me", "us")]

    if words in (["next", "page"], ["more", "files"], ["show", "more"], ["next"], ["more"], ["page", "down"]):
        return "page", 1

    if words in (["previous", "page"], ["last", "page"], ["back", "a", "page"], ["go", "back", "a", "page"],
                 ["previous"], ["page", "up"]):
        return "page", -1

    if words in (["back"], ["go", "back"], ["up"], ["go", "up"], ["up", "a", "folder"], ["go", "up", "a", "folder"],
                 ["back", "up"], ["parent", "folder"]):
        return "back", None

    # The rest name a card: "file three", "number three", "the third", "three".
    about_a_card = any(word in ("file", "files", "number", "folder", "item", "card") for word in words) or len(words) <= 3
    value = number_in(words)

    if value is None or not about_a_card:
        return None

    if any(word in _OPEN_WORDS for word in words):
        return "open", value

    if any(word in _PREVIEW_WORDS for word in words):
        return "preview", value

    if words and (words[0] in _DESCRIBE or words[0] in ("is", "whos")):
        return "describe", value

    # A bare number, "three" or "file three", while the files are showing.
    if len(words) <= 2:
        return "preview", value

    return None


def _match_folder(spoken):
    """The JARVIS subfolder a spoken name means, as folders.find matches it."""
    try:
        from actions import folders
        found = folders.find(spoken)
    except Exception:
        return None

    if not found:
        return None

    return found if _folder_path(found) else None


# ---- doing it -------------------------------------------------------------------------------

def _item(value):
    with _lock:
        items = list(_view["items"]) if _view else []

    return next((item for item in items if item["number"] == value), None), len(items)


def _no_such(value, count):
    if not count:
        return "There's nothing here to number, sir."

    return (f"There's no number {spoken_number(value)} here, sir; "
            f"they go up to {spoken_number(count)}.")


def _first_up(text):
    return text[:1].upper() + text[1:]


def _where(relative):
    return "your JARVIS folder" if not relative else f"the {relative.split('/')[-1]} folder"


def show(folder=""):
    """Project a folder of the JARVIS folder. Returns what to say."""
    global _view

    if _folder_path(folder) is None:
        return "I can't find that folder, sir."

    items = _listed(folder)

    with _lock:
        _view = {"folder": folder, "items": items, "page": 0, "focus": None}

    _publish()

    folders_count = sum(1 for item in items if item["kind"] == "folder")
    files_count = len(items) - folders_count

    if not items:
        return f"{_first_up(_where(folder))} is empty, sir."

    parts = []

    if folders_count:
        parts.append(f"{spoken_number(folders_count)} folder{'s' if folders_count != 1 else ''}")

    if files_count:
        parts.append(f"{spoken_number(files_count)} file{'s' if files_count != 1 else ''}")

    said = f"{_first_up(_where(folder))}, sir: {' and '.join(parts)}."

    if len(items) > PAGE_SIZE:
        said += " Say next page for more."

    return said


def close():
    """Put the hologram away. Returns what to say."""
    global _view

    with _lock:
        was = _view is not None
        _view = None

    if _hide_listener:
        try:
            _hide_listener()
        except Exception as error:
            print(f"[JARVIS] could not hide the files: {error}")

    return "Files closed, sir." if was else "The files aren't showing, sir."


def page(step):
    """Turn a page. Returns what to say."""
    with _lock:
        if _view is None:
            return "The files aren't showing, sir."

        pages = max(1, -(-len(_view["items"]) // PAGE_SIZE))
        wanted = _view["page"] + step

        if wanted < 0 or wanted >= pages:
            return "That's the first page, sir." if wanted < 0 else "That's the last page, sir."

        _view["page"] = wanted
        _view["focus"] = None
        first = wanted * PAGE_SIZE + 1
        last = min(len(_view["items"]), first + PAGE_SIZE - 1)

    _publish()
    return f"Page {spoken_number(wanted + 1)}, sir: {spoken_number(first)} to {spoken_number(last)}."


def _turn_to(value):
    """Show the page holding [value]."""
    with _lock:
        if _view is not None:
            _view["page"] = (value - 1) // PAGE_SIZE


def back():
    with _lock:
        folder = _view["folder"] if _view else ""

    if not folder:
        return "This is the top of your JARVIS folder, sir."

    return show("/".join(folder.split("/")[:-1]))


def describe(value):
    """What card [value] is. Returns what to say."""
    item, count = _item(value)

    if item is None:
        return _no_such(value, count)

    _focus(value, _card_detail(item))

    name = files.spoken_name(item["name"]) if item["kind"] == "file" else item["name"]

    if item["kind"] == "folder":
        return (f"Number {spoken_number(value)} is {item['name']}, a folder with "
                f"{spoken_number(item['items'])} item{'s' if item['items'] != 1 else ''}, sir.")

    return (f"Number {spoken_number(value)} is {name}, a {item['type']} of {_spoken_size(item['size'])}, "
            f"changed {_spoken_age(item['modified'])}, sir.")


def preview(value):
    """Card [value]'s first lines, on the hologram and aloud. Returns what to say."""
    item, count = _item(value)

    if item is None:
        return _no_such(value, count)

    if item["kind"] == "folder":
        inside = _listed(_join(item["name"]))
        detail = _card_detail(item)
        detail["lines"] = [entry["name"] for entry in inside[:PREVIEW_LINES]]
        _focus(value, detail)

        if not inside:
            return f"{item['name']} is empty, sir."

        newest = max((entry for entry in inside if entry["kind"] == "file"), key=lambda entry: entry["modified"], default=None)
        said = f"{item['name']} holds {spoken_number(len(inside))} item{'s' if len(inside) != 1 else ''}"
        said += f"; the newest file is {files.spoken_name(newest['name'])}." if newest else "."
        return said + " Say open " + spoken_number(value) + " to step inside."

    detail = _card_detail(item)
    kind = item["type"]
    name = files.spoken_name(item["name"])

    if kind == "image":
        detail["image"] = item["path"]
        _focus(value, detail)
        return f"Number {spoken_number(value)} is a picture, {name}, sir."

    lines = first_lines(item["path"])

    if lines is None:
        _focus(value, detail | {"note": f"NO TEXT TO SHOW IN A {kind.upper()}"})
        return f"Number {spoken_number(value)} is a {kind} of {_spoken_size(item['size'])}, sir; there's no text in it to show."

    if not lines:
        _focus(value, detail | {"note": "THIS FILE IS EMPTY"})
        return f"Number {spoken_number(value)}, {name}, is empty, sir."

    detail["lines"] = lines
    _focus(value, detail)

    return f"{name[0].upper() + name[1:]} begins: " + "; ".join(line.rstrip(".") for line in lines) + "."


def open_card(value):
    """Step into a folder, or open a file with its own program. Returns what to say."""
    item, count = _item(value)

    if item is None:
        return _no_such(value, count)

    if item["kind"] == "folder":
        return show(_join(item["name"]))

    if os.path.splitext(item["name"])[1].casefold() in _RUNS:
        return (f"Number {spoken_number(value)} is a program or a script, sir; "
                "I won't run it from here.")

    try:
        os.startfile(item["path"])   # noqa: S606 -- a document, opened as double-clicking would
    except (AttributeError, OSError) as error:
        print(f"[JARVIS] could not open {item['path']}: {error}")
        return f"I couldn't open number {spoken_number(value)}, sir."

    return f"Opening {files.spoken_name(item['name'])}, sir."


def click(value):
    """A card clicked on the hologram: a folder opens, a file shows its first lines."""
    item, _count = _item(value)

    if item is None:
        return None

    return show(_join(item["name"])) if item["kind"] == "folder" else preview(value)


def answer(text):
    """Do what [text] asks of the hologram. Returns what to say, or None."""
    request = asked(text)

    if request is None:
        return None

    kind, value = request
    actions = {"show": show, "close": lambda _value: close(), "page": page, "back": lambda _value: back(),
               "describe": describe, "preview": preview, "open": open_card}

    return actions[kind](value)


def _join(name):
    with _lock:
        folder = _view["folder"] if _view else ""

    return f"{folder}/{name}" if folder else name


def _card_detail(item):
    if item["kind"] == "folder":
        meta = f"FOLDER - {item['items']} ITEM{'S' if item['items'] != 1 else ''}"
    else:
        age = _age_text(item["modified"])
        meta = f"{item['type'].upper()} - {_size_text(item['size'])} - " + ("CHANGED JUST NOW" if age == "now" else f"CHANGED {age.upper()} AGO")

    return {"number": item["number"], "name": item["name"], "kind": item["kind"], "meta": meta,
            "lines": [], "note": None, "image": None}


def _focus(value, detail):
    _turn_to(value)

    with _lock:
        if _view is not None:
            _view["focus"] = detail

    _publish()


# ---- the first lines of a file ----------------------------------------------------------------

def _clip(line):
    line = " ".join(str(line).split())
    return line if len(line) <= PREVIEW_CHARS else line[:PREVIEW_CHARS - 3].rstrip() + "..."


def first_lines(path, count=PREVIEW_LINES):
    """The first [count] lines with words in them; [] for an empty file, None for one with no text."""
    suffix = Path(path).suffix.casefold()

    try:
        if suffix in _TEXT or suffix == "":
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(_HEAD_BYTES)

        elif suffix == ".docx":
            from docx import Document

            text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs[:60])

        elif suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(path)
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:2])

        else:
            return None

    except Exception as error:
        print(f"[JARVIS] could not read the start of {path}: {error}")
        return None

    lines = [_clip(line) for line in text.splitlines() if line.strip()]
    return lines[:count]
