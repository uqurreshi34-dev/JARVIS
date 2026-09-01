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
import re
import threading
import uuid
from datetime import datetime, timezone

from actions import memory_history


CARDINALITY_SINGLE = "single"
CARDINALITY_COLLECTION = "collection"

_lock = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _normalise_key(key):
    return (key or "").strip().casefold()


def _normalise_item(value):
    return " ".join(
        re.findall(r"\S+", (value or "").strip())
    ).casefold()


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
        return True
