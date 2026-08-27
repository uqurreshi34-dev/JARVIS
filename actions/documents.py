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
    return _article(entry.kind)


def _read_one(path_string: str) -> DocumentEntry | None:
    try:
        path = Path(path_string).expanduser().resolve()
    except (OSError, RuntimeError):
        return None

    if not path.is_file() or path.suffix.casefold() not in SUPPORTED_SUFFIXES:
        return None

    try:
        raw = _normalise_text(_extract(path))
    except Exception as error:
        print(f"[JARVIS] could not read {path}: {error}")
        return None

    if not raw:
        return None

    kind = _classify(raw)

    with _LOCK:
        remaining = MAX_CONTEXT_CHARS - \
            sum(len(item.text) for item in _DOCUMENTS)
        current_count = len(_DOCUMENTS)

        if current_count >= MAX_DOCUMENTS:
            return DocumentEntry(
                path=str(path), filename=path.name, kind=kind, text="",
                original_chars=len(raw), truncated=False,
            )

        already = any(item.path == str(path) for item in _DOCUMENTS)
        if already:
            return DocumentEntry(
                path=str(path), filename=path.name, kind=kind, text="",
                original_chars=len(raw), truncated=False,
            )

        if len(raw) <= remaining:
            text = raw
            truncated = False
        elif not _DOCUMENTS and remaining == MAX_CONTEXT_CHARS:
            # One large document is useful on its own, but it must be bounded.
            text, truncated = _truncate_for_context(raw, MAX_CONTEXT_CHARS)
        else:
            # Refuse only the offending document; the existing set stays intact.
            return DocumentEntry(
                path=str(path), filename=path.name, kind=kind, text="",
                original_chars=len(raw), truncated=False,
            )

        return DocumentEntry(
            path=str(path), filename=path.name, kind=kind, text=text,
            original_chars=len(raw), truncated=truncated,
        )


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
    refused = []
    duplicate = []
    unsupported = []

    for path in cleaned:
        candidate = _read_one(path)

        if candidate is None:
            suffix = Path(path).suffix.casefold()
            if suffix not in SUPPORTED_SUFFIXES:
                unsupported.append(Path(path).name)
            else:
                refused.append(Path(path).name)
            continue

        if not candidate.text:
            with _LOCK:
                exists_here = any(
                    item.path == candidate.path for item in _DOCUMENTS)
                full = len(_DOCUMENTS) >= MAX_DOCUMENTS
                remaining = MAX_CONTEXT_CHARS - \
                    sum(len(item.text) for item in _DOCUMENTS)

            if exists_here:
                duplicate.append(candidate.filename)
            elif full:
                refused.append(candidate.filename)
            else:
                refused.append(candidate.filename)
            continue

        with _LOCK:
            _DOCUMENTS.append(candidate)
        added.append(candidate)

        _notify()

    return _spoken_drop_result(added, refused, duplicate, unsupported)


def _spoken_drop_result(added, refused, duplicate, unsupported) -> str:
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
                text += " It is larger than my working-set limit, so I kept the most useful beginning and end."
            pieces.append(text)
        else:
            labels = ", ".join(_display_name(entry) for entry in added[:4])
            if added_count > 4:
                labels += " and more"
            pieces.append(f"Added {added_count} documents, sir: {labels}.")

    if refused:
        if added_count:
            if len(refused) == 1 and total == 0:
                pieces.append(
                    "I couldn't add the other document because it is too large for the working set."
                )
            elif len(refused) == 1:
                pieces.append(
                    "I left the other document out because it would exceed my working-set limit; the documents already loaded are untouched."
                )
            else:
                pieces.append(
                    f"I left {len(refused)} documents out because they would exceed my working-set limit."
                )
        else:
            pieces.append(
                f"I couldn't add those documents to the working set. "
                f"They may be too large, unsupported, or unreadable. "
                f"You currently have {total} documents loaded."
            )

    if duplicate:
        pieces.append(
            f"One or more were already in the working set. "
            f"You currently have {total} documents loaded."
        )

    if unsupported:
        pieces.append(
            "I currently support text, Markdown, CSV, JSON, Word documents, and PDFs.")

    if not pieces:
        return "I couldn't add those documents to the working set, sir."

    if total and sum(len(item.text) for item in entries()) >= int(MAX_CONTEXT_CHARS * _GETTING_FULL_AT):
        pieces.append("My working set is getting full.")

    return " ".join(pieces)


def context() -> str:
    with _LOCK:
        items = list(_DOCUMENTS)

    sections = []
    for index, item in enumerate(items, 1):
        label = _article(item.kind)
        sections.append(
            f"DOCUMENT {index}\n"
            f"Type identified from content: {label}\n"
            f"Original filename (not authoritative): {item.filename}\n"
            f"Content:\n{item.text}"
        )

    return "\n\n==============================\n\n".join(sections)


def status() -> str:
    with _LOCK:
        items = list(_DOCUMENTS)

    if not items:
        return "There are no documents in my working set, sir."

    labels = ", ".join(_article(item.kind) for item in items[:4])
    if len(items) > 4:
        labels += " and more"

    used = sum(len(item.text) for item in items)
    return f"I have {len(items)} documents loaded, sir: {labels}. {used} characters are in the working set."
