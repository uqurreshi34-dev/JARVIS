"""A small local vector database: each text is encoded once, ever.

Meaning-based search compares a question's vector with the vector of every
text it might match. Encoding is the slow part -- about 80 short sentences a
second on a modest CPU -- so a command log of a few thousand lines cannot be
encoded when the question is asked. Here each vector is worked out once,
saved in the JARVIS folder, and read back instantly from then on, across
restarts.

Stored in SQLite (part of Python, nothing to install), keyed by a hash of
the model and the text. The text itself is not stored: the database only
answers "have I encoded this before?". A different model gets different
keys, so changing the model can never mix incompatible vectors.

Everything here is best-effort. If the database cannot be opened, vectors
are still encoded and kept in memory for the session; nothing fails.
"""

import hashlib
import os
import sqlite3
import threading

import numpy as np


FILENAME = "jarvis-vectors.sqlite"

# Texts are encoded in batches this size. One batch pads every text to the
# longest in it, so thousands at once would need gigabytes; this keeps it
# to a few megabytes with no measurable loss of speed.
BATCH = 64

# Recently used vectors kept in memory in front of the database.
MEMORY_LIMIT = 20000

_lock = threading.RLock()
_connection = None
_connection_path = None
_memory = {}
_warned = False


def _path():
    from actions import files

    base = files.root()

    return os.path.join(base, FILENAME) if base else None


def _database():
    """The open database, or None when it cannot be used."""
    global _connection, _connection_path, _warned

    path = _path()

    if not path:
        return None

    if _connection is not None and _connection_path == path:
        return _connection

    try:
        connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            " key TEXT PRIMARY KEY,"
            " dimensions INTEGER NOT NULL,"
            " vector BLOB NOT NULL)"
        )
        connection.commit()
    except sqlite3.Error as error:
        if not _warned:
            print(f"[JARVIS] vector store unavailable, keeping vectors in memory only: {error}")
            _warned = True
        return None

    _connection = connection
    _connection_path = path

    return connection


def _key(model, text):
    return hashlib.sha256(f"{model}\n{text}".encode("utf-8")).hexdigest()


def vectors(texts, encode, model):
    """Vectors for [texts], encoding only those never seen before.

    [encode] turns a list of texts into an array of normalised vectors, or
    None when the model is unavailable, in which case so is this.
    """
    texts = [str(text or "") for text in texts]

    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    keys = [_key(model, text) for text in texts]

    with _lock:
        found = {key: _memory[key] for key in keys if key in _memory}
        wanted = [key for key in dict.fromkeys(keys) if key not in found]

        if wanted:
            found.update(_read(wanted))

        missing = {}

        for key, text in zip(keys, texts):
            if key not in found:
                missing.setdefault(key, text)

    if missing:
        fresh = {}
        pending = list(missing.items())

        for start in range(0, len(pending), BATCH):
            batch = pending[start:start + BATCH]
            encoded = encode([text for _key_, text in batch])

            if encoded is None:
                return None

            for (key, _text), vector in zip(batch, encoded):
                fresh[key] = np.asarray(vector, dtype=np.float32)

        with _lock:
            _write(fresh)
            found.update(fresh)

    with _lock:
        for key in keys:
            _memory[key] = found[key]

        while len(_memory) > MEMORY_LIMIT:
            _memory.pop(next(iter(_memory)))

    return np.stack([found[key] for key in keys])


def _read(keys):
    database = _database()

    if database is None or not keys:
        return {}

    found = {}

    try:
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = database.execute(
                f"SELECT key, dimensions, vector FROM vectors WHERE key IN ({marks})",
                chunk,
            ).fetchall()

            for key, dimensions, blob in rows:
                vector = np.frombuffer(blob, dtype=np.float32)

                if vector.shape == (dimensions,):
                    found[key] = vector
    except sqlite3.Error as error:
        print(f"[JARVIS] could not read the vector store: {error}")

    return found


def _write(fresh):
    database = _database()

    if database is None or not fresh:
        return

    try:
        database.executemany(
            "INSERT OR REPLACE INTO vectors (key, dimensions, vector) VALUES (?, ?, ?)",
            [
                (key, int(vector.shape[0]), np.ascontiguousarray(vector, dtype=np.float32).tobytes())
                for key, vector in fresh.items()
            ],
        )
        database.commit()
    except sqlite3.Error as error:
        print(f"[JARVIS] could not save to the vector store: {error}")


def count():
    """How many vectors are saved, or 0 without a database."""
    with _lock:
        database = _database()

        if database is None:
            return 0

        try:
            return int(database.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        except sqlite3.Error:
            return 0


def close():
    """Close the database and forget the in-memory copies (tests, shutdown)."""
    global _connection, _connection_path

    with _lock:
        if _connection is not None:
            try:
                _connection.close()
            except sqlite3.Error:
                pass

        _connection = None
        _connection_path = None
        _memory.clear()
