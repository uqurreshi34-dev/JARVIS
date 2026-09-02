"""Local relationship storage and traversal for connected JARVIS memory.

Relationships point at existing memory entities instead of copying their
values. This module deliberately knows nothing about books, projects, cars,
or any other domain: relation labels and entity references are data.
"""

import copy
import threading
import uuid
from datetime import datetime, timezone

from actions import memory_history


_lock = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    return " ".join(str(value or "").strip().split())


def _entity(value):
    return _clean(value)


def _blank_relations():
    return []


def _data():
    data = memory_history._load()

    if data is None:
        data = {
            "version": 1,
            "memories": [],
            "history": [],
        }

    relations = data.get("relations")

    if not isinstance(relations, list):
        data["relations"] = _blank_relations()

    return data


def _same(left, right):
    return _entity(left).casefold() == _entity(right).casefold()


def records():
    """Return all relationship records as independent copies."""
    with _lock:
        return copy.deepcopy(_data().get("relations", []))


def add(from_entity, relation, to_entity, confidence=1.0, source="user"):
    """Create one generic relation, idempotently."""
    source_from = _entity(from_entity)
    source_relation = _entity(relation)
    source_to = _entity(to_entity)

    if not source_from or not source_relation or not source_to:
        return False

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0

    confidence = max(0.0, min(1.0, confidence))

    with _lock:
        data = _data()
        relations = data["relations"]

        for record in relations:
            if not isinstance(record, dict):
                continue

            if (
                _same(record.get("from"), source_from)
                and _same(record.get("relation"), source_relation)
                and _same(record.get("to"), source_to)
            ):
                record["confidence"] = max(
                    float(record.get("confidence", 0.0) or 0.0),
                    confidence,
                )
                record["source"] = record.get("source") or source
                record["updated_at"] = _now()
                memory_history._save(data)
                return True

        now = _now()
        relations.append({
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
    """Remove matching relationships; omitted fields act as wildcards."""
    with _lock:
        data = _data()
        relations = data["relations"]
        kept = []
        removed = False

        for record in relations:
            if not isinstance(record, dict):
                kept.append(record)
                continue

            matches = (
                (from_entity is None or _same(record.get("from"), from_entity))
                and (relation is None or _same(record.get("relation"), relation))
                and (to_entity is None or _same(record.get("to"), to_entity))
            )

            if matches:
                removed = True
                continue

            kept.append(record)

        if not removed:
            return False

        data["relations"] = kept
        memory_history._save(data)
        return True


def outgoing(entity, relation=None):
    """Return relations leaving an entity."""
    wanted = _entity(entity)

    if not wanted:
        return []

    with _lock:
        result = []

        for record in _data().get("relations", []):
            if not isinstance(record, dict) or not _same(record.get("from"), wanted):
                continue

            if relation is not None and not _same(record.get("relation"), relation):
                continue

            result.append(copy.deepcopy(record))

        return result


def incoming(entity, relation=None):
    """Return relations arriving at an entity."""
    wanted = _entity(entity)

    if not wanted:
        return []

    with _lock:
        result = []

        for record in _data().get("relations", []):
            if not isinstance(record, dict) or not _same(record.get("to"), wanted):
                continue

            if relation is not None and not _same(record.get("relation"), relation):
                continue

            result.append(copy.deepcopy(record))

        return result


def related(entity, relation=None, direction="both"):
    """Return directly related entities without performing recursive graph search."""
    result = []

    if direction in ("out", "both"):
        for record in outgoing(entity, relation=relation):
            result.append({
                "entity": record["to"],
                "relation": record["relation"],
                "direction": "out",
                "record": record,
            })

    if direction in ("in", "both"):
        for record in incoming(entity, relation=relation):
            result.append({
                "entity": record["from"],
                "relation": record["relation"],
                "direction": "in",
                "record": record,
            })

    return result


def traverse(start, relation=None, direction="out", max_hops=1):
    """Traverse a small local relationship graph, returning reachable entities.

    The default is deliberately one hop. This is connected memory, not a
    reasoning engine: callers must explicitly request deeper traversal.
    """
    current = [_entity(start)] if _entity(start) else []
    visited = set(current)
    found = []

    try:
        max_hops = max(1, min(4, int(max_hops)))
    except (TypeError, ValueError):
        max_hops = 1

    for _ in range(max_hops):
        next_entities = []

        for entity in current:
            for item in related(entity, relation=relation, direction=direction):
                target = item["entity"]
                marker = target.casefold()

                if marker in visited:
                    continue

                visited.add(marker)
                next_entities.append(target)
                found.append(item)

        if not next_entities:
            break

        current = next_entities

    return found


def has(from_entity, relation, to_entity):
    """Return whether an exact relationship exists."""
    return any(
        _same(record.get("from"), from_entity)
        and _same(record.get("relation"), relation)
        and _same(record.get("to"), to_entity)
        for record in records()
    )


__all__ = [
    "add",
    "has",
    "incoming",
    "outgoing",
    "records",
    "related",
    "remove",
    "traverse",
]
