"""What every model call cost, and who asked for it.

Before anything can be made cheaper, it has to be seen. Every paid
request JARVIS makes goes through providers.py, and until now each one
came back with an exact count of the tokens it used -- which was read
for the text and thrown away. This keeps it.

One line per call, appended to a monthly file in the JARVIS folder:

    {"at": "...", "provider": "claude", "model": "...", "kind": "chat",
     "caller": "actions/research.answer", "in": 1840, "out": 212, ...}

The caller is found automatically by looking up the stack for the
first function outside providers.py, so no feature has to announce
itself and none of the files that ask a model anything had to change.
That is the column that matters: it turns "JARVIS spent a lot today"
into "the research command spent most of it".

Tokens only, deliberately. Prices change, differ by provider and
account, and a hardcoded price table would be wrong within months and
quietly so. Counting what was actually used is never wrong.

This must never break a model call. Every failure here is swallowed
after a single printed line, because an answer the user asked for is
worth more than a record of what it cost.

    python usage.py           today, by caller and by provider
    python usage.py 7         the last seven days
"""

import json
import os
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta


_FOLDER_NAME = "JARVIS"

# Frames from these files are the plumbing, not the caller. Anything
# above them on the stack is the feature that actually wanted an answer.
_PLUMBING = ("providers.py", "usage.py")

_HERE = os.path.dirname(os.path.abspath(__file__))

_lock = threading.Lock()

# One warning per run is enough. A full disk would otherwise print on
# every single request.
_warned = False


def _folder():
    """Where the ledger lives: the same folder as places.json.

    Resolved here rather than through actions.files, because this is
    imported by providers.py, which loads before almost everything.
    """
    path = os.path.join(os.path.expanduser("~"), _FOLDER_NAME)
    os.makedirs(path, exist_ok=True)

    return path


def _ledger(day):
    return os.path.join(_folder(), f"usage-{day:%Y-%m}.jsonl")


def _caller():
    """The first function up the stack that isn't plumbing.

    Returned as 'actions/research.answer' -- the file relative to the
    JARVIS folder, and the function in it -- so it reads the same way
    the code is laid out.
    """
    frame = sys._getframe(1)

    while frame is not None:
        path = frame.f_code.co_filename

        if os.path.basename(path) not in _PLUMBING:
            try:
                where = os.path.relpath(path, _HERE)
            except ValueError:
                # Different drive on Windows; relpath cannot bridge it.
                where = os.path.basename(path)

            if where.endswith(".py"):
                where = where[:-3]

            return f"{where.replace(os.sep, '/')}.{frame.f_code.co_name}"

        frame = frame.f_back

    return "unknown"


def _counts(response):
    """Token counts from either response shape. Missing fields are 0.

    Anthropic says input/output, with cache reads and writes counted
    separately. OpenAI-compatible providers (Groq, Gemini) say
    prompt/completion. Both are normalised to the same four numbers.
    """
    used = getattr(response, "usage", None)

    if used is None:
        return None

    def read(*names):
        for name in names:
            value = getattr(used, name, None)

            if isinstance(value, int):
                return value

        return 0

    return {
        "in": read("input_tokens", "prompt_tokens"),
        "out": read("output_tokens", "completion_tokens"),
        "cache_read": read("cache_read_input_tokens"),
        "cache_write": read("cache_creation_input_tokens"),
    }


def record(provider, model, kind, response):
    """Note what one call used. Never raises."""
    global _warned

    try:
        counts = _counts(response)

        if counts is None:
            return

        now = datetime.now()

        entry = {
            "at": now.isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "kind": kind,
            "caller": _caller(),
            **counts,
        }

        with _lock:
            with open(_ledger(now), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

    except Exception as error:
        if not _warned:
            _warned = True
            print(f"[JARVIS] could not record model usage: {error}")


def entries(days=1):
    """Every recorded call from the last N days, oldest first."""
    since = datetime.now() - timedelta(days=days)
    months = sorted({since.strftime("%Y-%m"), datetime.now().strftime("%Y-%m")})

    found = []

    for month in months:
        path = os.path.join(_folder(), f"usage-{month}.jsonl")

        if not os.path.exists(path):
            continue

        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                    when = datetime.fromisoformat(entry["at"])
                except (ValueError, KeyError):
                    continue

                if when >= since:
                    found.append(entry)

    return found


def summary(days=1):
    """Totals by caller and by provider, biggest first."""
    calls = entries(days)

    fields = ("in", "out", "cache_read", "cache_write")

    by_caller = defaultdict(lambda: dict(calls=0, **{f: 0 for f in fields}))
    by_provider = defaultdict(lambda: dict(calls=0, **{f: 0 for f in fields}))

    for entry in calls:
        for bucket in (by_caller[entry.get("caller", "unknown")],
                       by_provider[entry.get("provider", "unknown")]):
            bucket["calls"] += 1

            for field in fields:
                bucket[field] += entry.get(field, 0)

    def ranked(table):
        return sorted(table.items(),
                      key=lambda item: -(item[1]["in"] + item[1]["out"]))

    return {
        "calls": len(calls),
        "in": sum(e.get("in", 0) for e in calls),
        "out": sum(e.get("out", 0) for e in calls),
        "cache_read": sum(e.get("cache_read", 0) for e in calls),
        "cache_write": sum(e.get("cache_write", 0) for e in calls),
        "by_caller": ranked(by_caller),
        "by_provider": ranked(by_provider),
    }


def _print(days):
    report = summary(days)
    span = "today" if days == 1 else f"in the last {days} days"

    if not report["calls"]:
        print(f"No model calls recorded {span}.")
        return

    total = report["in"] + report["out"]

    print(f"{report['calls']} model calls {span}: "
          f"{report['in']:,} tokens in, {report['out']:,} out.")

    # Prompt caching: a write stores the unchanging start of a request for a few
    # minutes, a read reuses it at a fraction of the price. Reads well above
    # writes means caching is paying for itself.
    print(f"Cached: {report['cache_read']:,} read back, "
          f"{report['cache_write']:,} written.\n")

    for title, rows in (("By caller", report["by_caller"]),
                        ("By provider", report["by_provider"])):
        print(title)

        for name, row in rows:
            share = 100 * (row["in"] + row["out"]) / total if total else 0
            print(f"  {share:5.1f}%  {row['calls']:>4} calls  "
                  f"{row['in']:>9,} in  {row['out']:>7,} out  "
                  f"{row['cache_read']:>9,} read  {row['cache_write']:>8,} written   {name}")

        print()


if __name__ == "__main__":
    _print(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
