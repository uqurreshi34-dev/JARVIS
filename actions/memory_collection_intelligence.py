"""Schema learning and local semantic matching for collection memories.

This module sits above memory_collections.py. It does not change command
routing or existing memory behaviour by itself. A later integration layer can
feed it the structured memory decision already produced by the command-model
call, after which the learned schema and its language examples can be matched
locally without another provider request.
"""

import copy
import re
import threading

from actions import memory_collections, memory_history


_SCHEMA_EXAMPLE_LIMIT = 8
_LOCAL_SCHEMA_SCORE = 0.62

_lock = threading.RLock()


def _key(value):
    return (value or "").strip().casefold()


def _clean_example(value):
    return " ".join((value or "").strip().split())


def _load():
    data = memory_history._load()

    if data is None:
        data = {
            "version": 1,
            "memories": [],
            "history": [],
        }

    data.setdefault("schemas", {})

    if not isinstance(data["schemas"], dict):
        data["schemas"] = {}

    return data


def _save(data):
    return memory_history._save(data)


def schema(key):
    """Return the learned schema for a key, or None."""
    wanted = _key(key)

    if not wanted:
        return None

    with _lock:
        data = _load()
        value = data["schemas"].get(wanted)

        if not isinstance(value, dict):
            return None

        return copy.deepcopy(value)


def remember_schema(
    key,
    cardinality,
    example=None,
    operation="add",
    source="language-model",
):
    """Cache a schema decision and one useful language example locally.

    Repeated examples are ignored. Only the schema metadata is persisted;
    no provider call happens here.
    """
    wanted = _key(key)

    if not wanted or cardinality not in (
        memory_collections.CARDINALITY_SINGLE,
        memory_collections.CARDINALITY_COLLECTION,
    ):
        return False

    cleaned = _clean_example(example)

    with _lock:
        data = _load()
        existing = data["schemas"].get(wanted)

        if not isinstance(existing, dict):
            existing = {}

        existing["cardinality"] = cardinality
        existing["source"] = source
        existing["examples"] = [
            item
            for item in existing.get("examples", [])
            if isinstance(item, str) and item.strip()
        ]
        existing["operations"] = [
            item
            for item in existing.get("operations", [])
            if isinstance(item, str) and item.strip()
        ]

        if cleaned and cleaned.casefold() not in {
            item.casefold() for item in existing["examples"]
        }:
            existing["examples"].append(cleaned)
            existing["examples"] = existing["examples"][-_SCHEMA_EXAMPLE_LIMIT:]

        if operation and operation not in existing["operations"]:
            existing["operations"].append(operation)
            existing["operations"] = existing["operations"][-4:]

        data["schemas"][wanted] = existing
        _save(data)

    return True


def learn(decision, example=None, source="language-model"):
    """Persist a structured memory decision returned by the command model.

    Expected keys are intentionally small and provider-neutral:
    ``key``, ``cardinality`` and ``operation``. The optional ``items`` field
    is not stored here because collection contents belong to
    memory_collections.py.
    """
    if not isinstance(decision, dict):
        return False

    return remember_schema(
        decision.get("key"),
        decision.get("cardinality"),
        example=example,
        operation=decision.get("operation") or "add",
        source=source,
    )


def _schema_documents():
    """Schemas as semantic-search documents."""
    documents = []

    with _lock:
        data = _load()

        for key, value in data["schemas"].items():
            if not isinstance(value, dict):
                continue

            examples = value.get("examples") or []

            for example in examples:
                cleaned = _clean_example(example)

                if cleaned:
                    documents.append(
                        (
                            key,
                            value.get("cardinality"),
                            cleaned,
                        )
                    )

    return documents


def local_match(text):
    """Find a learned memory schema locally by semantic meaning.

    Returns ``(key, cardinality, score)`` or ``(None, None, 0.0)``.
    The semantic encoder is the existing local ONNX engine; this function
    never contacts an LLM provider.
    """
    cleaned = _clean_example(text)

    if not cleaned:
        return None, None, 0.0

    documents = _schema_documents()

    if not documents:
        return None, None, 0.0

    try:
        from actions import semantic_memory

        rankable = [
            (key, cardinality, example)
            for key, cardinality, example in documents
        ]
        ranked = semantic_memory._semantic_rank(
            cleaned,
            [
                (key, cardinality, example)
                for key, cardinality, example in rankable
            ],
        )

    except Exception as error:
        print(f"[JARVIS] local collection schema match failed: {error}")
        return None, None, 0.0

    if not ranked:
        return None, None, 0.0

    index, score = ranked[0]

    if score < _LOCAL_SCHEMA_SCORE:
        return None, None, score

    key, cardinality, _example = rankable[index]

    return key, cardinality, score


def locally_known(text):
    """True when a statement strongly matches a learned collection schema."""
    key, cardinality, score = local_match(text)

    return (
        key is not None
        and cardinality == memory_collections.CARDINALITY_COLLECTION
        and score >= _LOCAL_SCHEMA_SCORE
    )


def operation_for_text(text, key=None):
    """Choose a safe local operation for a known collection statement.

    Destructive removal is explicit. Replacement is reserved for wording that
    clearly says a previous collection is being changed; ordinary activity
    statements remain additions. This is deliberately generic and does not
    name projects, books, gym days, or any other domain.
    """
    folded = _clean_example(text).casefold()

    if re.match(r"^(?:forget|remove|delete|drop|erase|take out)\b", folded):
        return "remove"

    replacement = (
        r"\b(?:now|new|changed|change|updated|update|instead|"
        r"replaced?|switched|no longer|from now|going forward|"
        r"these days)\b"
    )

    if re.search(replacement, folded):
        return "replace"

    # Explicit collection declarations such as "my ... are ..." naturally
    # describe the current set. Do this from grammar, not from a domain list.
    if re.match(r"^my\s+.+?\s+(?:are)\s+", folded):
        return "replace"

    return "add"


__all__ = [
    "learn",
    "learned_schema",
    "locally_known",
    "local_match",
    "operation_for_text",
    "remember_schema",
    "schema",
]

# Backwards-friendly name for callers that prefer a predicate-like API.
learned_schema = schema
