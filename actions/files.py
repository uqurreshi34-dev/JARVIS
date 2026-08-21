"""File operations, confined to a single folder.

Everything JARVIS reads or writes lives under ~/JARVIS. A misheard command
should never be able to touch anything else, so every path is resolved and
checked against that root before use.
"""

import os
import re
import shutil

# Optional formats. Missing libraries are reported rather than crashing.
try:
    from docx import Document

    _DOCX = True
except ImportError:
    _DOCX = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    _PDF = True
except ImportError:
    _PDF = False


FOLDER_NAME = "JARVIS"

TEXT_SUFFIXES = (".txt", ".md", ".csv", ".log", ".json")
READABLE_SUFFIXES = TEXT_SUFFIXES + (".docx",)

# Spoken format words mapped to a file extension.
FORMATS = {
    "text": ".txt", "text file": ".txt", "txt": ".txt", "plain text": ".txt",
    "markdown": ".md", "md": ".md",
    "word": ".docx", "word document": ".docx", "doc": ".docx",
    "docx": ".docx", "word doc": ".docx",
    "pdf": ".pdf", "pdf document": ".pdf",
    "csv": ".csv", "spreadsheet": ".csv",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Reading a whole file aloud is unusable, so only this much is spoken.
_SPEAK_LIMIT = 400


def root():
    """The one folder JARVIS may touch. Created if missing."""
    path = os.path.join(os.path.expanduser("~"), FOLDER_NAME)

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not create {path}: {error}")
        return None

    return path


def safe_name(name, default_suffix=".txt"):
    """Turn spoken words into a filename, or None if nothing usable remains."""
    cleaned = _ILLEGAL.sub("", (name or "").strip())
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(". ")

    if not cleaned:
        return None

    stem, suffix = os.path.splitext(cleaned)

    if not suffix:
        suffix = default_suffix

    return f"{stem}{suffix}"


def resolve(name, default_suffix=".txt"):
    """Full path inside the folder, or None if it would escape it."""
    base = root()
    filename = safe_name(name, default_suffix)

    if not base or not filename:
        return None

    path = os.path.abspath(os.path.join(base, filename))

    # The decisive check: anything that resolves outside the root is refused.
    if os.path.commonpath([path, os.path.abspath(base)]) != os.path.abspath(base):
        print(f"[JARVIS] refusing a path outside {base}")
        return None

    return path


def exists(name, default_suffix=".txt"):
    path = resolve(name, default_suffix)

    return bool(path and os.path.exists(path))


def unique_path(path):
    """A path that does not exist yet, adding (1), (2) and so on."""
    if not os.path.exists(path):
        return path

    stem, suffix = os.path.splitext(path)

    for index in range(1, 500):
        candidate = f"{stem} ({index}){suffix}"

        if not os.path.exists(candidate):
            return candidate

    return None


def write(name, content="", default_suffix=".txt", overwrite=False):
    """Create a file with optional content. Returns the path, or None."""
    path = resolve(name, default_suffix)

    if not path:
        return None

    if os.path.exists(path) and not overwrite:
        path = unique_path(path)

        if not path:
            return None

    suffix = os.path.splitext(path)[1].casefold()

    try:
        if suffix == ".docx":
            if not _DOCX:
                print("[JARVIS] python-docx is not installed")
                return None

            document = Document()

            for line in (content or "").split("\n"):
                document.add_paragraph(line)

            document.save(path)

        elif suffix == ".pdf":
            if not _PDF:
                print("[JARVIS] reportlab is not installed")
                return None

            doc = SimpleDocTemplate(path, pagesize=A4)
            styles = getSampleStyleSheet()
            flow = []

            for line in (content or "").split("\n"):
                if line.strip():
                    flow.append(Paragraph(line, styles["Normal"]))
                else:
                    flow.append(Spacer(1, 10))

            doc.build(flow or [Spacer(1, 10)])

        else:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content or "")

    except Exception as error:
        print(f"[JARVIS] could not write {path}: {error}")
        return None

    print(f"[JARVIS] wrote {path}")

    return path


def append(name, content, default_suffix=".txt"):
    """Add a line to an existing text file, creating it if needed."""
    path = resolve(name, default_suffix)

    if not path:
        return None

    if os.path.splitext(path)[1].casefold() not in TEXT_SUFFIXES:
        print("[JARVIS] can only append to plain text files")
        return None

    try:
        # Existing content may not end with a newline, which would run the
        # two lines together.
        needs_break = (
            os.path.exists(path) and os.path.getsize(path) > 0
        )

        if needs_break:
            with open(path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                needs_break = handle.read(1) not in (b"\n", b"\r")

        with open(path, "a", encoding="utf-8") as handle:
            if needs_break:
                handle.write("\n")

            handle.write(f"{content}\n")

    except OSError as error:
        print(f"[JARVIS] could not append to {path}: {error}")
        return None

    return path


def read(name):
    """Return a file's text, or None if it cannot be read."""
    path = resolve(name)

    if not path or not os.path.exists(path):
        return None

    suffix = os.path.splitext(path)[1].casefold()

    try:
        if suffix == ".docx":
            if not _DOCX:
                print("[JARVIS] python-docx is not installed")
                return None

            document = Document(path)

            return "\n".join(p.text for p in document.paragraphs)

        if suffix in TEXT_SUFFIXES:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()

        print(f"[JARVIS] cannot read {suffix} files")
        return None

    except Exception as error:
        print(f"[JARVIS] could not read {path}: {error}")
        return None


def copy(source, destination=None):
    """Copy a file within the folder. Returns the new path, or None."""
    source_path = resolve(source)

    if not source_path or not os.path.exists(source_path):
        print(f"[JARVIS] no file named {source!r}")
        return None

    if destination:
        target = resolve(destination, os.path.splitext(source_path)[1])
    else:
        stem, suffix = os.path.splitext(os.path.basename(source_path))
        target = resolve(f"{stem} copy{suffix}")

    if not target:
        return None

    target = unique_path(target)

    if not target:
        return None

    try:
        shutil.copy2(source_path, target)
    except OSError as error:
        print(f"[JARVIS] could not copy: {error}")
        return None

    print(f"[JARVIS] copied to {target}")

    return target


def listing():
    """Filenames in the folder, newest first."""
    base = root()

    if not base:
        return []

    try:
        entries = [
            entry for entry in os.listdir(base)
            if os.path.isfile(os.path.join(base, entry))
        ]
    except OSError:
        return []

    entries.sort(
        key=lambda name: os.path.getmtime(os.path.join(base, name)),
        reverse=True,
    )

    return entries


_SCREENSHOT = re.compile(r"^jarvis-\d{4}-\d{2}-\d{2}-\d{6}$", re.IGNORECASE)

_TYPE_WORDS = {
    ".pdf": "PDF",
    ".docx": "Word document",
    ".doc": "Word document",
    ".csv": "spreadsheet",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".md": "markdown file",
    ".txt": "",
    ".log": "log",
    ".json": "JSON file",
}


def spoken_name(filename):
    """A filename a voice can say quickly.

    Timestamped names are read digit by digit otherwise, which takes about
    fifteen seconds each.
    """
    stem, suffix = os.path.splitext(filename)
    suffix = suffix.casefold()

    if _SCREENSHOT.match(stem):
        return "a screenshot"

    kind = _TYPE_WORDS.get(suffix, suffix.lstrip(".") or "")

    # Underscores and hyphens are spoken aloud, so replace them.
    spoken = stem.replace("_", " ").replace("-", " ")
    spoken = " ".join(spoken.split())

    return f"{spoken} {kind}".strip() if kind else spoken


def describe_listing(limit=4, names=False):
    """Spoken summary of what is in the folder.

    Terse by default: reading filenames aloud takes several seconds, so the
    count is given and the names only on request.
    """
    entries = listing()

    if not entries:
        return "Your JARVIS folder is empty, sir."

    if not names:
        if len(entries) == 1:
            return f"One file, sir: {spoken_name(entries[0])}."

        return f"You have {len(entries)} files, sir."

    if len(entries) == 1:
        return f"One file, sir: {spoken_name(entries[0])}."

    shown = [spoken_name(entry) for entry in entries[:limit]]
    listed = ", ".join(shown[:-1]) + f", and {shown[-1]}"

    if len(entries) <= limit:
        return f"You have {len(entries)} files, sir: {listed}."

    return (
        f"You have {len(entries)} files, sir. The {len(shown)} most recent "
        f"are {listed}."
    )


def describe_listing_named(limit=4):
    """The full version, with filenames read out."""
    return describe_listing(limit=limit, names=True)


def describe_read(name):
    """Spoken rendering of a file's contents."""
    content = read(name)
    label = spoken_name(safe_name(name) or name)

    if content is None:
        return f"I couldn't find a file called {label}, sir."

    tidied = " ".join(content.split())

    if not tidied:
        return f"{label} is empty, sir."

    if len(tidied) <= _SPEAK_LIMIT:
        return f"{label} says: {tidied}"

    words = len(tidied.split())
    opening = tidied[:_SPEAK_LIMIT].rsplit(" ", 1)[0]

    return f"{label} holds {words} words, sir. It begins: {opening}..."
