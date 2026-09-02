"""Generic local relationships between existing JARVIS memory entities.

Relationships are a thin layer over memory.json. They do not own facts or
collection values and they do not define a domain ontology. The first local
inference is exact value membership: a single memory whose value is already
present in a learned collection is linked to that collection item.
"""

import copy
import re
import threading
import uuid
from datetime import datetime, timezone

from actions import memory, memory_collections, memory_history


_lock = threading.RLock()
_installed = False
_original_set_fact = None
_original_remember = None
_original_add = None
_original_remove = None
_original_replace = None
_original_answer = None

_RELATION = "member_of"
_RELATION_CUES = (
    "related", "connected", "associated", "part of", "belong",
    "belongs", "member", "one of", "same group",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    return " ".join(str(value or "").strip().split())


def _normalise(value):
    return _clean(value).casefold()


def _identity(value):
    """Compare labels while ignoring case, spacing and punctuation."""
    return "".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def memory_entity(key):
    key = _clean(key)
    return f"memory:{key}" if key else ""


def collection_entity(key, item=None):
    key = _clean(key)
    if not key:
        return ""
    if item is None:
        return f"collection:{key}"
    item = _clean(item)
    return f"collection:{key}:{item}" if item else f"collection:{key}"


def _data():
    data = memory_history._load()
    if data is None:
        data = {"version": 1, "memories": [], "history": []}
    if not isinstance(data.get("relations"), list):
        data["relations"] = []
    return data


def records():
    """Return all relationship records as independent copies."""
    with _lock:
        return copy.deepcopy(_data()["relations"])


def add(from_entity, relation, to_entity, confidence=1.0, source="user"):
    """Add one idempotent relationship between two entity references."""
    source_from = _clean(from_entity)
    source_relation = _clean(relation)
    source_to = _clean(to_entity)
    if not source_from or not source_relation or not source_to:
        return False

    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 1.0

    with _lock:
        data = _data()
        for record in data["relations"]:
            if not isinstance(record, dict):
                continue
            if (
                _normalise(record.get("from")) == _normalise(source_from)
                and _normalise(record.get("relation")) == _normalise(source_relation)
                and _normalise(record.get("to")) == _normalise(source_to)
            ):
                try:
                    old = float(record.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    old = 0.0
                record["confidence"] = max(old, confidence)
                record["updated_at"] = _now()
                memory_history._save(data)
                return True

        now = _now()
        data["relations"].append({
            "id": uuid.uuid4().hex,
            "from": source_from,
            "relation": source_relation,
            "to": source_to,
            "confidence": confidence,
            "source": source,
            "created_at": now,
            "updated_at": now,
        })
        memory_history._save(data)
        return True


def remove(from_entity=None, relation=None, to_entity=None):
    """Remove relationships matching the supplied fields."""
    with _lock:
        data = _data()
        kept = []
        removed = False
        for record in data["relations"]:
            if not isinstance(record, dict):
                kept.append(record)
                continue
            matches = (
                from_entity is None or _normalise(record.get("from")) == _normalise(from_entity)
            ) and (
                relation is None or _normalise(record.get("relation")) == _normalise(relation)
            ) and (
                to_entity is None or _normalise(record.get("to")) == _normalise(to_entity)
            )
            if matches:
                removed = True
            else:
                kept.append(record)
        if not removed:
            return False
        data["relations"] = kept
        memory_history._save(data)
        return True


def outgoing(entity, relation=None):
    wanted = _normalise(entity)
    if not wanted:
        return []
    with _lock:
        return [
            copy.deepcopy(record)
            for record in _data()["relations"]
            if isinstance(record, dict)
            and _normalise(record.get("from")) == wanted
            and (relation is None or _normalise(record.get("relation")) == _normalise(relation))
        ]


def incoming(entity, relation=None):
    wanted = _normalise(entity)
    if not wanted:
        return []
    with _lock:
        return [
            copy.deepcopy(record)
            for record in _data()["relations"]
            if isinstance(record, dict)
            and _normalise(record.get("to")) == wanted
            and (relation is None or _normalise(record.get("relation")) == _normalise(relation))
        ]


def related(entity, relation=None, direction="both"):
    result = []
    if direction in ("out", "both"):
        for record in outgoing(entity, relation=relation):
            result.append({"entity": record.get("to"), "relation": record.get("relation"), "direction": "out", "record": record})
    if direction in ("in", "both"):
        for record in incoming(entity, relation=relation):
            result.append({"entity": record.get("from"), "relation": record.get("relation"), "direction": "in", "record": record})
    return result


def traverse(start, relation=None, direction="out", max_hops=1):
    """Traverse locally with a deliberately small hop limit."""
    start = _clean(start)
    if not start:
        return []
    try:
        max_hops = max(1, min(4, int(max_hops)))
    except (TypeError, ValueError):
        max_hops = 1

    current = [start]
    visited = {start.casefold()}
    found = []
    for _ in range(max_hops):
        next_entities = []
        for entity in current:
            for item in related(entity, relation=relation, direction=direction):
                target = _clean(item.get("entity"))
                marker = target.casefold()
                if not target or marker in visited:
                    continue
                visited.add(marker)
                next_entities.append(target)
                found.append(item)
        if not next_entities:
            break
        current = next_entities
    return found


def has(from_entity, relation, to_entity):
    return any(
        _normalise(record.get("from")) == _normalise(from_entity)
        and _normalise(record.get("relation")) == _normalise(relation)
        and _normalise(record.get("to")) == _normalise(to_entity)
        for record in records()
        if isinstance(record, dict)
    )


def _value_memories():
    result = []
    for record in _data().get("memories", []):
        if not isinstance(record, dict):
            continue
        key = _clean(record.get("key"))
        value = _clean(record.get("value"))
        if key and value:
            result.append((key, value))
    return result


def sync_memberships():
    """Connect single memories to collection items with the same identity."""
    memories = _value_memories()
    collections = _data().get("collections", {})
    if not memories or not isinstance(collections, dict):
        return 0

    added = 0
    for key, value in memories:
        wanted = _identity(value)
        for collection_key in list(collections):
            for item in memory_collections.items(collection_key):
                if _identity(item) != wanted:
                    continue
                from_entity = memory_entity(key)
                to_entity = collection_entity(collection_key, item)
                if has(from_entity, _RELATION, to_entity):
                    continue
                if add(from_entity, _RELATION, to_entity, source="inferred-local"):
                    added += 1
    return added


def _answer_relation_question(query):
    """Answer a small generic set of relationship questions locally."""
    text = _clean(query)
    folded = text.casefold()
    if not any(cue in folded for cue in _RELATION_CUES):
        return None

    try:
        summary = memory.relevant_summary(text, limit=1)
    except Exception:
        return None
    if not summary:
        return None

    lines = [
        line.strip()[2:]
        for line in summary.splitlines()
        if line.strip().startswith("- ")
    ]
    if not lines:
        return None

    key, separator, value = lines[0].partition(":")
    if not separator:
        return None
    key = _clean(key)
    value = _clean(value)
    if not key or not value:
        return None

    links = outgoing(memory_entity(key), relation=_RELATION)
    targets = []
    for record in links:
        match = re.match(r"^collection:(.+?):(.*)$", _clean(record.get("to")), re.I)
        if match:
            collection_key = _clean(match.group(1))
            item = _clean(match.group(2))
            if collection_key and item:
                targets.append((collection_key, item))
    if not targets:
        return None

    if len(targets) == 1:
        collection_key, item = targets[0]
        return f"Your {key} is {value}, and it is one of your {collection_key}."

    grouped = {}
    for collection_key, item in targets:
        grouped.setdefault(collection_key, []).append(item)
    parts = []
    for collection_key, items in grouped.items():
        if len(items) == 1:
            joined = items[0]
        elif len(items) == 2:
            joined = f"{items[0]} and {items[1]}"
        else:
            joined = f"{', '.join(items[:-1])}, and {items[-1]}"
        parts.append(f"{joined} in your {collection_key}")
    return f"Your {key} is {value}, connected as {', '.join(parts)}."


def _wrapped_answer(question):
    local = _answer_relation_question(question)
    if local:
        return local
    return _original_answer(question)


def _after_memory_write(*args, **kwargs):
    result = _original_set_fact(*args, **kwargs)
    if result:
        sync_memberships()
    return result


def _after_remember(*args, **kwargs):
    result = _original_remember(*args, **kwargs)
    if result:
        sync_memberships()
    return result


def _after_collection_add(*args, **kwargs):
    result = _original_add(*args, **kwargs)
    if result:
        sync_memberships()
    return result


def _after_collection_remove(*args, **kwargs):
    result = _original_remove(*args, **kwargs)
    if result:
        sync_memberships()
    return result


def _after_collection_replace(*args, **kwargs):
    result = _original_replace(*args, **kwargs)
    if result:
        sync_memberships()
    return result


def install_runtime(commands):
    """Install relationship hooks after normal command modules exist."""
    global _installed
    global _original_set_fact, _original_remember
    global _original_add, _original_remove, _original_replace
    global _original_answer

    with _lock:
        if _installed:
            return

        _original_set_fact = memory.set_fact
        _original_remember = memory.remember
        _original_add = memory_collections.add
        _original_remove = memory_collections.remove
        _original_replace = memory_collections.replace
        _original_answer = commands.answer

        memory.set_fact = _after_memory_write
        memory.remember = _after_remember
        memory_collections.add = _after_collection_add
        memory_collections.remove = _after_collection_remove
        memory_collections.replace = _after_collection_replace
        commands.answer = _wrapped_answer

        sync_memberships()
        _installed = True


__all__ = [
    "add", "collection_entity", "has", "incoming", "install_runtime",
    "memory_entity", "outgoing", "records", "related", "remove",
    "sync_memberships", "traverse",
]
