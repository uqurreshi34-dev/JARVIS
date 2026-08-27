"""In-memory working set for documents dropped onto JARVIS.

Documents are read from their original locations and are never copied into
JARVIS. Extraction and lightweight classification are entirely local; the
language model is only called later, when the user asks a question.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from actions import safety


try:
    from docx import Document
    _DOCX_AVAILABLE = True
except ImportError:
    Document = None
    _DOCX_AVAILABLE = False

try:
    from pypdf import PdfReader
    _PDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    _PDF_AVAILABLE = False


MAX_CONTEXT_CHARS = 60_000
MAX_DOCUMENTS = 8
_GETTING_FULL_AT = 0.75

# Every document is capped to this individually, unconditionally, before
# anything checks whether it fits alongside the rest of the working set.
# Kept well below MAX_CONTEXT_CHARS so a fresh, empty set can always take
# one document after this cap is applied — which is what makes "too long
# even on its own" structurally impossible, and "too long to add to the
# others" the only size-based refusal that can actually happen. Roughly
# 15-16 pages of ordinary prose.
MAX_CHARS_PER_DOCUMENT = 24_000

SUPPORTED_SUFFIXES = frozenset({
    ".txt", ".md", ".csv", ".log", ".json", ".docx", ".pdf",
})


@dataclass(frozen=True)
class DocumentEntry:
    path: str
    filename: str
    kind: str | None
    text: str
    original_chars: int
    truncated: bool = False


_LOCK = threading.RLock()
_DOCUMENTS: list[DocumentEntry] = []
_LISTENER = None


# The classifier deliberately uses both distinctive words and structural
# phrases. It is local, deterministic, and uses no API call.
_CLASSIFIERS = (
    (
        "invoice",
        (
            ("invoice", 4), ("tax invoice", 5), ("invoice number", 5),
            ("invoice no", 5), ("amount due", 4), ("subtotal", 2),
            ("vat", 2), ("due date", 2), ("bill to", 2),
            ("payment due", 3),
        ),
    ),
    (
        "contract",
        (
            ("agreement", 3), ("this agreement", 5), ("parties", 2),
            ("hereby", 3), ("whereas", 3), ("shall", 1),
            ("effective date", 3), ("termination", 3),
            ("terms and conditions", 4), ("governing law", 3),
        ),
    ),
    (
        "recipe",
        (
            ("ingredients", 5), ("method", 3), ("directions", 3),
            ("servings", 3), ("preheat", 2), ("teaspoon", 2),
            ("tablespoon", 2), ("bake", 1), ("oven", 1),
        ),
    ),
    (
        "report",
        (
            ("executive summary", 5), ("findings", 3),
            ("recommendations", 3), ("conclusion", 3),
            ("methodology", 2), ("introduction", 1),
            ("appendix", 1), ("report", 3),
        ),
    ),
    (
        "letter",
        (
            ("dear sir", 4), ("dear madam", 4), ("dear ", 2),
            ("yours sincerely", 4), ("yours faithfully", 4),
            ("kind regards", 3), ("subject:", 2),
        ),
    ),
    (
        "CV or résumé",
        (
            ("curriculum vitae", 6), ("professional experience", 4),
            ("employment history", 4), ("education", 2),
            ("work experience", 3), ("qualifications", 3),
            ("skills", 1), ("references", 1),
        ),
    ),
)


_ARTICLE = {
    "invoice": "an invoice",
    "contract": "a contract",
    "recipe": "a recipe",
    "report": "a report",
    "letter": "a letter",
    "CV or résumé": "a CV",
}


def set_listener(listener):
    global _LISTENER
    _LISTENER = listener


def _notify():
    listener = _LISTENER
    if listener:
        try:
            listener()
        except Exception as error:
            print(f"[JARVIS] document listener error: {error}")


def count() -> int:
    with _LOCK:
        return len(_DOCUMENTS)


def active() -> bool:
    return count() > 0


def entries() -> list[DocumentEntry]:
    with _LOCK:
        return list(_DOCUMENTS)


def clear() -> str:
    with _LOCK:
        removed = len(_DOCUMENTS)
        _DOCUMENTS.clear()

    _notify()

    if removed == 0:
        return "There are no documents in my working set, sir."

    word = "document" if removed == 1 else "documents"
    return f"Cleared {removed} {word} from my working set, sir."


def _normalise_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read_text_file(path: Path) -> str:
    suffix = path.suffix.casefold()

    if suffix == ".json":
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            return json.dumps(value, ensure_ascii=False, indent=2)
        except OSError:
            return ""

    if suffix == ".csv":
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            output = []
            reader = csv.reader(io.StringIO(raw))
            for row in reader:
                if row:
                    output.append(" | ".join(cell.strip() for cell in row))
            return "\n".join(output)
        except OSError:
            return ""

    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _read_docx(path: Path) -> str:
    if not _DOCX_AVAILABLE:
        raise RuntimeError("python-docx is not installed")

    document = Document(str(path))
    parts = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)

    for table in document.tables:
        for row in table.rows:
            cells = []
            for cell in row.cells:
                value = " ".join(
                    paragraph.text.strip()
                    for paragraph in cell.paragraphs
                    if paragraph.text.strip()
                )
                cells.append(value)
            if any(cells):
                parts.append(" | ".join(cells))

    return "\n".join(parts)


def _read_pdf(path: Path) -> str:
    if not _PDF_AVAILABLE:
        raise RuntimeError("pypdf is not installed")

    reader = PdfReader(str(path))
    parts = []

    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)

    return "\n\n".join(parts)


def _extract(path: Path) -> str:
    suffix = path.suffix.casefold()

    if suffix == ".docx":
        return _read_docx(path)

    if suffix == ".pdf":
        return _read_pdf(path)

    return _read_text_file(path)


def _classify(text: str) -> str | None:
    sample = text.casefold()
    scored = []

    for kind, signals in _CLASSIFIERS:
        score = 0
        for signal, weight in signals:
            if signal in sample:
                score += weight
        scored.append((score, kind))

    scored.sort(reverse=True)
    best_score, best_kind = scored[0]

    if best_score < 4:
        return None

    # Avoid calling generic documents a report simply because they contain a
    # conclusion or introduction. A stronger score wins; ties remain unknown.
    if len(scored) > 1 and best_score == scored[1][0]:
        return None

    return best_kind


def _truncate_for_context(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False

    head = max(1, int(limit * 0.75))
    tail = max(1, limit - head)
    marker = "\n\n[Middle of document omitted to stay within JARVIS's working-set budget.]\n\n"

    available = max(0, limit - len(marker))
    head = int(available * 0.75)
    tail = available - head

    return text[:head] + marker + text[-tail:], True


def _article(kind: str | None) -> str:
    return _ARTICLE.get(kind, "a document")


def _display_name(entry: DocumentEntry) -> str:
    return entry.filename


def _read_one(path_string: str):
    """Read and prepare one dropped file.

    Returns (entry, None) on success — already appended to the working
    set — or (None, reason) on failure, where reason is one of
    "unsupported", "unreadable", "duplicate", "full", or "overflow".
    Explicit, rather than an empty-text sentinel a caller has to re-lock
    and re-derive the reason for later, which is what the previous
    version did.
    """
    try:
        path = Path(path_string).expanduser().resolve()
    except (OSError, RuntimeError):
        return None, "unreadable"

    if not path.is_file():
        return None, "unreadable"

    if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
        return None, "unsupported"

    try:
        raw = _normalise_text(_extract(path))
    except Exception as error:
        print(f"[JARVIS] could not read {path}: {error}")
        return None, "unreadable"

    if not raw:
        return None, "unreadable"

    kind = _classify(raw)

    # Capped unconditionally now, not only when this happens to be the
    # first document added — a large file dropped second or third used
    # to be refused outright instead of getting the same useful
    # truncated read a lone large file already got.
    if len(raw) > MAX_CHARS_PER_DOCUMENT:
        capped_text, doc_truncated = _truncate_for_context(
            raw, MAX_CHARS_PER_DOCUMENT
        )
    else:
        capped_text, doc_truncated = raw, False

    with _LOCK:
        if any(item.path == str(path) for item in _DOCUMENTS):
            return None, "duplicate"

        if len(_DOCUMENTS) >= MAX_DOCUMENTS:
            return None, "full"

        remaining = MAX_CONTEXT_CHARS - sum(
            len(item.text) for item in _DOCUMENTS
        )

        if len(capped_text) > remaining:
            # MAX_CHARS_PER_DOCUMENT is always <= MAX_CONTEXT_CHARS, so
            # this can only mean it doesn't fit alongside what's already
            # loaded — it would always have fit fine on its own.
            return None, "overflow"

        entry = DocumentEntry(
            path=str(path), filename=path.name, kind=kind,
            text=capped_text, original_chars=len(raw),
            truncated=doc_truncated,
        )

        _DOCUMENTS.append(entry)

    _notify()

    return entry, None


def add_paths(paths) -> str:
    cleaned = []
    seen = set()

    for value in paths or ():
        try:
            path = str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        if path not in seen:
            seen.add(path)
            cleaned.append(path)

    if not cleaned:
        return "I couldn't read those documents, sir."

    added = []
    unsupported = []
    unreadable = []
    duplicate = []
    full = []
    overflow = []

    for path in cleaned:
        entry, reason = _read_one(path)

        if entry is not None:
            added.append(entry)
            continue

        name = Path(path).name

        if reason == "unsupported":
            unsupported.append(name)
        elif reason == "duplicate":
            duplicate.append(name)
        elif reason == "full":
            full.append(name)
        elif reason == "overflow":
            overflow.append(name)
        else:
            unreadable.append(name)

    return _spoken_drop_result(
        added, unsupported, unreadable, duplicate, full, overflow
    )


def _spoken_drop_result(
    added, unsupported, unreadable, duplicate, full, overflow
) -> str:
    added_count = len(added)
    total = count()

    pieces = []

    if added_count:
        if added_count == 1:
            entry = added[0]
            text = (
                f"One document, sir: {_display_name(entry)}. "
                f"You now have {total} documents loaded."
            )
            if entry.truncated:
                text += (
                    " It's larger than my per-document limit, so I kept "
                    "the most useful beginning and end."
                )
            pieces.append(text)
        else:
            labels = ", ".join(_display_name(entry) for entry in added[:4])
            if added_count > 4:
                labels += " and more"
            pieces.append(
                f"Added {added_count} documents, sir: {labels}. "
                f"You now have {total} documents loaded."
            )

    if overflow:
        if len(overflow) == 1:
            pieces.append(
                f"{overflow[0]} is too long to add to the others, sir. "
                "On its own I could manage it."
            )
        else:
            names = ", ".join(overflow)
            pieces.append(
                f"{names} are too long to add to the others, sir. "
                "On their own I could manage them."
            )

    if full:
        if len(full) == 1:
            pieces.append(
                "I'm already holding as many as I can manage, sir — "
                f"{full[0]} will have to wait."
            )
        else:
            pieces.append(
                "I'm already holding as many as I can manage, sir — "
                f"{len(full)} more will have to wait."
            )

    if duplicate:
        if len(duplicate) == 1:
            pieces.append(f"{duplicate[0]} was already in the working set.")
        else:
            pieces.append(
                f"{len(duplicate)} of those were already in the working set."
            )

    if unsupported:
        pieces.append(
            "I currently support text, Markdown, CSV, JSON, Word "
            "documents, and PDFs."
        )

    if unreadable:
        if len(unreadable) == 1:
            pieces.append(f"I couldn't read {unreadable[0]}, sir.")
        else:
            pieces.append(f"I couldn't read {len(unreadable)} of them, sir.")

    if not pieces:
        return "I couldn't add those documents to the working set, sir."

    if total and sum(
        len(item.text) for item in entries()
    ) >= int(MAX_CONTEXT_CHARS * _GETTING_FULL_AT):
        pieces.append("My working set is getting full.")

    return " ".join(pieces)


def context() -> str:
    with _LOCK:
        items = list(_DOCUMENTS)

    sections = []
    for index, item in enumerate(items, 1):
        label = _article(item.kind)
        header = (
            f"DOCUMENT {index}\n"
            f"Type identified from content: {label}\n"
            f"Original filename (not authoritative): {item.filename}"
        )

        # The document's own text is untrusted content — it can be
        # written to look like an instruction to the model, exactly the
        # concern safety.py already exists to guard against everywhere
        # else outside content reaches a prompt (browser pages, memory
        # facts, application names). This is the first place several
        # such documents go into one prompt together, so this matters
        # more here than anywhere else in the project.
        #
        # limit is explicit and set to the text's own length: quote()
        # defaults to a 2000-character cap of its own, which would
        # silently re-truncate a document already correctly sized down
        # to MAX_CHARS_PER_DOCUMENT to a fraction of that if left unset.
        wrapped = safety.quote(
            f"document {index} content", item.text, limit=len(item.text)
        )

        sections.append(f"{header}\n{wrapped}")

    return "\n\n==============================\n\n".join(sections)


def status() -> str:
    with _LOCK:
        items = list(_DOCUMENTS)

    if not items:
        return "There are no documents in my working set, sir."

    labels = ", ".join(item.filename for item in items[:4])
    if len(items) > 4:
        labels += " and more"

    used = sum(len(item.text) for item in items)
    word = "document" if len(items) == 1 else "documents"

    return (
        f"I have {len(items)} {word} loaded, sir: {labels}. "
        f"{used} characters are in the working set."
    )
