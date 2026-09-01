"""Local collection storage for structured JARVIS memory.

This module is the storage substrate for memories that naturally contain more
than one current item. It deliberately does not decide *which* user utterance
means a collection or alter the existing memory/router behaviour; later layers
can learn that from language and call these local operations.

The existing memory.json remains the machine-readable store. Collection data
lives under its own "collections" object and the schema under "schemas", so
single-value memories and existing history remain untouched until a caller
explicitly opts a memory key into collection behaviour.
"""

import copy
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from actions import files, memory_history


CARDINALITY_SINGLE = "single"
CARDINALITY_COLLECTION = "collection"

_COLLECTION_HEADER = (
    "# --- JARVIS collections (managed; structured data lives in memory.json) ---"
)
_COLLECTION_FOOTER = "# --- End JARVIS collections ---"

_lock = threading.RLock()
_installed = False
_original_memory_write = None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _normalise_key(key):
    return (key or "").strip().casefold()


def _normalise_item(value):
    return " ".join(
        re.findall(r"\S+", (value or "").strip())
    ).casefold()


def _memory_path():
    base = files.root()
    return os.path.join(base, "memory.txt") if base else None


def _sync_text(data=None):
    """Mirror current collections into a managed, human-readable text block."""
    path = _memory_path()

    if not path:
        return False

    if data is None:
        data = _ensure_data()

    collections = data.get("collections", {})

    if not isinstance(collections, dict):
        collections = {}

    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                lines = handle.read().splitlines()
        else:
            lines = [
                "# What JARVIS knows about you. One fact per line.",
                "# Edit or delete anything here; it is read at startup.",
            ]

        cleaned = []
        inside = False

        for line in lines:
            stripped = line.strip()

            if stripped == _COLLECTION_HEADER:
                inside = True
                continue

            if stripped == _COLLECTION_FOOTER:
                inside = False
                continue

            if not inside:
                cleaned.append(line)

        while cleaned and not cleaned[-1].strip():
            cleaned.pop()

        block = []

        for key, raw_items in collections.items():
            if not isinstance(raw_items, list):
                continue

            values = []

            for entry in raw_items:
                if isinstance(entry, dict):
                    value = str(entry.get("value") or "").strip()
                else:
                    value = str(entry or "").strip()

                if value:
                    values.append(value)

            if values:
                block.append(f"# {key}: {', '.join(values)}")

        if block:
            if cleaned:
                cleaned.append("")

            cleaned.append(_COLLECTION_HEADER)
            cleaned.extend(block)
            cleaned.append(_COLLECTION_FOOTER)

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(cleaned).rstrip("\n") + "\n")

        return True

    except OSError as error:
        print(f"[JARVIS] could not mirror collections to memory.txt: {error}")
        return False


def _install_memory_writer():
    """Keep collection text in place when the ordinary memory writer runs."""
    global _original_memory_write

    from actions import memory

    if getattr(memory, "_collection_text_sync_installed", False):
        return

    _original_memory_write = memory._write

    def write_with_collections(lines):
        result = _original_memory_write(lines)

        if result:
            _sync_text()

        return result

    memory._write = write_with_collections
    memory._collection_text_sync_installed = True


def install():
    """Install collection-to-memory.txt mirroring exactly once."""
    global _installed

    with _lock:
        if _installed:
            return

        _install_memory_writer()
        _sync_text()
        _installed = True


def _ensure_data():
    """Load memory metadata, creating only the collection containers."""
    data = memory_history._load()

    if data is None:
        data = {
            "version": 1,
            "memories": [],
            "history": [],
        }

    data.setdefault("schemas", {})
    data.setdefault("collections", {})

    if not isinstance(data["schemas"], dict):
        data["schemas"] = {}

    if not isinstance(data["collections"], dict):
        data["collections"] = {}

    return data


def cardinality(key):
    """Return the learned cardinality for a memory key, or None."""
    wanted = _normalise_key(key)

    if not wanted:
        return None

    with _lock:
        data = _ensure_data()
        schema = data["schemas"].get(wanted)

        if not isinstance(schema, dict):
            return None

        value = schema.get("cardinality")

        if value in (CARDINALITY_SINGLE, CARDINALITY_COLLECTION):
            return value

        return None


def set_cardinality(key, value, source="local"):
    """Record a single/collection schema decision locally."""
    wanted = _normalise_key(key)

    if not wanted or value not in (
        CARDINALITY_SINGLE,
        CARDINALITY_COLLECTION,
    ):
        return False

    with _lock:
        data = _ensure_data()
        existing = data["schemas"].get(wanted)

        if (
            isinstance(existing, dict)
            and existing.get("cardinality") == value
        ):
            return True

        now = _now()
        data["schemas"][wanted] = {
            "cardinality": value,
            "updated_at": now,
            "source": source,
        }

        memory_history._save(data)
        _sync_text(data)
        return True


def items(key):
    """Return collection values in their stored order."""
    wanted = _normalise_key(key)

    if not wanted:
        return []

    with _lock:
        data = _ensure_data()
        raw = data["collections"].get(wanted, [])

        if not isinstance(raw, list):
            return []

        values = []

        for entry in raw:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "").strip()
            else:
                value = str(entry or "").strip()

            if value:
                values.append(value)

        return values


def records(key):
    """Return a copy of collection records, including metadata."""
    wanted = _normalise_key(key)

    if not wanted:
        return []

    with _lock:
        data = _ensure_data()
        raw = data["collections"].get(wanted, [])

        if not isinstance(raw, list):
            return []

        return copy.deepcopy(raw)


def add(key, value, source="user"):
    """Add one item to a collection, ignoring case/spacing duplicates."""
    wanted = _normalise_key(key)
    cleaned = " ".join((value or "").strip().split())

    if not wanted or not cleaned:
        return False

    with _lock:
        data = _ensure_data()

        if data["schemas"].get(wanted, {}).get("cardinality") != CARDINALITY_COLLECTION:
            return False

        collection = data["collections"].setdefault(wanted, [])

        if not isinstance(collection, list):
            collection = []
            data["collections"][wanted] = collection

        normalised = _normalise_item(cleaned)

        for entry in collection:
            existing = (
                entry.get("value", "")
                if isinstance(entry, dict)
                else str(entry or "")
            )

            if _normalise_item(existing) == normalised:
                return True

        now = _now()
        collection.append({
            "id": uuid.uuid4().hex,
            "value": cleaned,
            "created_at": now,
            "updated_at": now,
            "confidence": 1.0,
            "source": source,
        })

        memory_history._save(data)
        _sync_text(data)
        return True


def remove(key, value):
    """Remove one matching collection item. Returns True when removed."""
    wanted = _normalise_key(key)
    normalised = _normalise_item(value)

    if not wanted or not normalised:
        return False

    with _lock:
        data = _ensure_data()
        collection = data["collections"].get(wanted, [])

        if not isinstance(collection, list):
            return False

        kept = []
        removed = False

        for entry in collection:
            existing = (
                entry.get("value", "")
                if isinstance(entry, dict)
                else str(entry or "")
            )

            if not removed and _normalise_item(existing) == normalised:
                removed = True
                continue

            kept.append(entry)

        if not removed:
            return False

        data["collections"][wanted] = kept
        memory_history._save(data)
        _sync_text(data)
        return True


def replace(key, values, source="user"):
    """Replace a collection with unique values, preserving input order."""
    wanted = _normalise_key(key)

    if not wanted:
        return False

    unique = []
    seen = set()

    for value in values or ():
        cleaned = " ".join(str(value or "").strip().split())
        normalised = _normalise_item(cleaned)

        if not normalised or normalised in seen:
            continue

        seen.add(normalised)
        unique.append(cleaned)

    with _lock:
        data = _ensure_data()

        if data["schemas"].get(wanted, {}).get("cardinality") != CARDINALITY_COLLECTION:
            return False

        now = _now()
        data["collections"][wanted] = [
            {
                "id": uuid.uuid4().hex,
                "value": value,
                "created_at": now,
                "updated_at": now,
                "confidence": 1.0,
                "source": source,
            }
            for value in unique
        ]

        memory_history._save(data)
        _sync_text(data)
        return True


# The collection module is imported by the memory-intelligence layer during
# normal startup, so install the human-readable mirror without adding another
# command or routing hook.
install()
