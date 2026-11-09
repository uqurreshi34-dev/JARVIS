"""What JARVIS's requests used, read back from request_log: how many needed the internet and for what,
how long each part took, how often a part failed over, and how much said in the room never left the PC.

    request report / weekly report / how much went online / what went online today
    python -m actions.request_report [days]      the same, as a table (7 days unless told)

Nothing is fetched and no model is asked: it reads requests-YYYY-MM.jsonl.
"""

import statistics
import sys
from collections import Counter

import request_log


def _median(values):
    values = [value for value in values if isinstance(value, (int, float))]
    return statistics.median(values) if values else None


def _slowest(values):
    values = [value for value in values if isinstance(value, (int, float))]
    return max(values) if values else None


def summary(days=7, entries=None):
    """The numbers, as a dict (see the keys below)."""
    entries = request_log.entries(days) if entries is None else entries
    requests = [entry for entry in entries if entry.get("kind") == "request"]
    ignored = [entry for entry in entries if entry.get("kind") == "ignored"]
    heard = [entry["heard"] for entry in requests if entry.get("heard")]
    brain_calls = [call for entry in requests for call in entry.get("brain") or []]
    voices = [spoken for entry in requests for spoken in entry.get("voice") or []]
    fallbacks = Counter(what for entry in requests for what in entry.get("fallbacks") or [])
    fallbacks.update(entry.get("what") for entry in entries if entry.get("kind") == "fallback")

    return {
        "days": days,
        "requests": len(requests),
        "by_source": dict(Counter(entry.get("source") for entry in requests)),
        "online": sum(1 for entry in requests if entry.get("online")),
        "online_hearing": sum(1 for entry in requests if "hearing" in (entry.get("online") or [])),
        "online_ai": sum(1 for entry in requests if "ai" in (entry.get("online") or [])),
        "online_voice": sum(1 for entry in requests if "voice" in (entry.get("online") or [])),
        "understood_locally": sum(1 for entry in requests if entry.get("route") == "local"),
        "outcomes": dict(Counter(entry.get("outcome") for entry in requests)),
        "to_first_word_ms": _median((entry.get("ms") or {}).get("to_first_word") for entry in requests),
        "to_first_word_slowest_ms": _slowest((entry.get("ms") or {}).get("to_first_word") for entry in requests),
        "hearing_ms_online": _median(item.get("ms") for item in heard if item.get("online")),
        "hearing_ms_local": _median(item.get("ms") for item in heard if not item.get("online")),
        "heard_by": dict(Counter(item.get("engine") for item in heard)),
        "ai_calls": len(brain_calls),
        "ai_ms": _median(call.get("ms") for call in brain_calls),
        "ai_by": dict(Counter(call.get("provider") for call in brain_calls)),
        "tokens": sum(call.get("in", 0) + call.get("out", 0) for call in brain_calls),
        "voice_ms": _median(spoken.get("ms") for spoken in voices),
        "voice_by": dict(Counter(spoken.get("engine") for spoken in voices)),
        "voice_chars_online": sum(spoken.get("chars", 0) for spoken in voices if spoken.get("online")),
        "fallbacks": dict(fallbacks),
        "kept_off_line": sum(1 for entry in ignored if not entry.get("online")),
        "sent_then_ignored": sum(1 for entry in ignored if entry.get("online")),
    }


def _seconds(ms):
    return f"{ms / 1000:.1f} seconds"


def report(days=7):
    """The report, as said."""
    numbers = summary(days)
    count = numbers["requests"]
    span = "today" if days == 1 else f"in the last {days} days"

    if not count:
        return f"I have no requests on record {span}, sir. They are recorded from now on."

    online = numbers["online"]
    parts = [f"Request report, sir. You asked me {count} thing{'s' if count != 1 else ''} {span}."]

    if online:
        reasons = [f"{label} for {numbers[key]}" for key, label in
                   (("online_ai", "the AI"), ("online_hearing", "hearing you"), ("online_voice", "my voice"))
                   if numbers[key]]
        parts.append(f"{online} of them, {round(100 * online / count)} percent, needed the internet: "
                     + ", ".join(reasons) + ".")
    else:
        parts.append("None of them needed the internet.")

    if count - online:
        parts.append(f"{count - online} stayed entirely on this PC.")

    if numbers["to_first_word_ms"] is not None:
        parts.append(f"From hearing you to my first word took {_seconds(numbers['to_first_word_ms'])} typically, "
                     f"{_seconds(numbers['to_first_word_slowest_ms'])} at the slowest.")

    if numbers["kept_off_line"]:
        parts.append(f"The wake word kept {numbers['kept_off_line']} thing{'s' if numbers['kept_off_line'] != 1 else ''} "
                     "said in the room on this PC.")

    if numbers["sent_then_ignored"]:
        sent = numbers["sent_then_ignored"]
        parts.append(f"{sent} thing{'s were' if sent != 1 else ' was'} sent to be written out and "
                     f"{'were' if sent != 1 else 'was'} not for me.")

    failed = sum(numbers["fallbacks"].values())

    if failed:
        top = ", ".join(f"{what.replace('>', ' to ')} {times} time{'s' if times != 1 else ''}"
                        for what, times in Counter(numbers["fallbacks"]).most_common(3))
        parts.append(f"Parts failed over {failed} time{'s' if failed != 1 else ''}: {top}.")

    return " ".join(parts)


def line_for_status():
    """One sentence for "status report": today's share that needed the internet, or None."""
    numbers = summary(1)

    if not numbers["requests"]:
        return None

    return (f"{numbers['online']} of today's {numbers['requests']} request{'s' if numbers['requests'] != 1 else ''} "
            "needed the internet.")


def table(days=7):
    """The same, as a table for the console."""
    numbers = summary(days)
    rows = [("requests", numbers["requests"]), ("by source", numbers["by_source"]),
            ("needed the internet", numbers["online"]), ("  for hearing", numbers["online_hearing"]),
            ("  for the AI", numbers["online_ai"]), ("  for the voice", numbers["online_voice"]),
            ("understood with no AI", numbers["understood_locally"]), ("outcomes", numbers["outcomes"]),
            ("to first word, median ms", numbers["to_first_word_ms"]),
            ("to first word, slowest ms", numbers["to_first_word_slowest_ms"]),
            ("hearing ms, online / local", (numbers["hearing_ms_online"], numbers["hearing_ms_local"])),
            ("heard by", numbers["heard_by"]), ("AI calls", numbers["ai_calls"]),
            ("AI ms, median", numbers["ai_ms"]), ("AI by", numbers["ai_by"]), ("AI tokens", numbers["tokens"]),
            ("voice ms to sound, median", numbers["voice_ms"]), ("voice by", numbers["voice_by"]),
            ("voice characters sent online", numbers["voice_chars_online"]),
            ("fallbacks", numbers["fallbacks"]), ("kept on the PC by the wake word", numbers["kept_off_line"]),
            ("sent, then not for JARVIS", numbers["sent_then_ignored"])]
    width = max(len(name) for name, _ in rows)
    return "\n".join(f"{name.ljust(width)}  {value}" for name, value in rows)


if __name__ == "__main__":
    print(table(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
