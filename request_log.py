"""One record per request: what was heard and how, which way it went, which AI answered and how long
each part took, which voice spoke -- and whether each of those went online. Kept as one line of JSON per
request in requests-YYYY-MM.jsonl in the JARVIS folder, beside usage-YYYY-MM.jsonl (tokens) and
jarvis-log.txt (what was said and done). "Request report" (actions/request_report.py) reads it back.

The words themselves are not kept here: jarvis-log.txt already has them. A record holds only how the
request was handled, so it can be read and shared to optimise JARVIS without giving away what was asked.

Also recorded, as their own lines:
  "ignored"   something heard that was not for JARVIS: whether it was turned away before being written
              out (the wake-word model) or only after (written out, by Groq if that is the engine, and no
              name found) -- the second is what the wake-word model saves
  "fallback"  a part that failed over mid-request: Groq hearing to local Whisper, one AI to the next,
              the Google voice to Kokoro or Windows

Never raises: a request is never lost because its record could not be written.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

_FOLDER_NAME = "JARVIS"
_lock = threading.Lock()
_state = {"current": None, "heard": None, "warned": False}

# A transcription this old when a request begins is not this request's.
_HEARD_FRESH_SECONDS = 30.0


def _folder():
    path = os.path.join(os.path.expanduser("~"), _FOLDER_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _ledger(day):
    return os.path.join(_folder(), f"requests-{day:%Y-%m}.jsonl")


def _write(entry):
    try:
        now = datetime.now()
        entry = {"at": now.isoformat(timespec="seconds"), **entry}

        with _lock:
            with open(_ledger(now), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
    except Exception as error:   # noqa: BLE001 - never lose a request over its record
        if not _state["warned"]:
            _state["warned"] = True
            print(f"[JARVIS] could not record a request: {error}")


def heard(engine, online, ms=None, audio_seconds=None, how=None, fell_back=False):
    """What was just written out for JARVIS, and how: kept for the request it begins."""
    _state["heard"] = {
        "at": time.monotonic(),
        "engine": engine,
        "online": bool(online),
        "ms": None if ms is None else int(ms),
        "audio_s": None if audio_seconds is None else round(float(audio_seconds), 2),
        "how": how,
        "fell_back": bool(fell_back),
    }


def ignored(why, engine=None, online=False, audio_seconds=None):
    """Something heard that was not for JARVIS. [online]: it was sent out to be written out first."""
    _write({"kind": "ignored", "why": why, "engine": engine, "online": bool(online),
            "audio_s": None if audio_seconds is None else round(float(audio_seconds), 2)})


def begin(source):
    """A request starts ("desk", "phone"); returned, for end(). Takes what was just heard at the desk. One
    request is current at a time: one that begins while another runs (the phone during a desk turn) is
    recorded too, marked "overlapped", and the AI calls of both are counted with the first."""
    heard_now = None

    if source == "desk":
        heard_now, _state["heard"] = _state["heard"], None

        if heard_now and time.monotonic() - heard_now["at"] > _HEARD_FRESH_SECONDS:
            heard_now = None

    if heard_now:
        heard_now = {key: value for key, value in heard_now.items() if key != "at"}

    record = {
        "kind": "request",
        "source": source,
        "started": time.monotonic(),
        "heard": heard_now,
        "intent": None,
        "route": None,
        "brain": [],
        "voice": [],
        "fallbacks": [],
    }

    with _lock:
        if _state["current"] is None:
            _state["current"] = record
        else:
            record["overlapped"] = True

    return record


def _current():
    return _state["current"]


def route(intent, free_path):
    """How the request was understood: "local" (the phrase list and JARVIS's own rules) or "model"."""
    record = _current()

    if record is not None and record["intent"] is None:
        record["intent"], record["route"] = intent, "local" if free_path else "model"


def model_call(provider, model, kind, counts=None):
    """One AI call (from usage.record): who, which model, what kind, its tokens."""
    record = _current()

    if record is not None:
        call = {"provider": provider, "model": model, "kind": kind}

        if counts:
            call.update({"in": counts.get("in", 0), "out": counts.get("out", 0)})

        record["brain"].append(call)


def model_time(provider, ms):
    """How long the last call to [provider] took (providers.chat times its calls)."""
    record = _current()

    if record is None:
        return

    for call in reversed(record["brain"]):
        if call["provider"] == provider and "ms" not in call:
            call["ms"] = int(ms)
            return


def voice(engine, online, chars, ms=None, stood_in=None):
    """Something spoken: which voice, whether it went online (a phrase already saved does not), how
    many characters, how long until it was heard, and which voice stood in if the chosen one failed."""
    record = _current()

    if record is not None:
        record["voice"].append({"engine": engine, "online": bool(online), "chars": int(chars),
                                "ms": None if ms is None else int(ms), "stood_in": stood_in})


def fallback(what):
    """A part that failed over ("groq-whisper>local", "groq>gemini", "google>kokoro")."""
    record = _current()

    if record is not None:
        record["fallbacks"].append(what)
    else:
        _write({"kind": "fallback", "what": what})


def end(record, outcome="answered", **times):
    """[record] (from begin) is over: written, with how long it took ([times] in seconds: "to_first_word",
    "decide", "respond") and whether any part of it went online. Twice is harmless."""
    if record is None or "started" not in record:
        return None

    with _lock:
        if _state["current"] is record:
            _state["current"] = None

    total = time.monotonic() - record.pop("started")
    online = []

    if record["heard"] and record["heard"]["online"]:
        online.append("hearing")

    if record["brain"]:
        online.append("ai")

    if any(spoken["online"] for spoken in record["voice"]):
        online.append("voice")

    record["outcome"] = outcome
    record["ms"] = {"total": int(total * 1000),
                    **{name: int(value * 1000) for name, value in times.items() if value is not None}}
    record["online"] = online
    _write(record)
    return record


def entries(days=7):
    """Every record from the last [days] days, oldest first."""
    since = datetime.now() - timedelta(days=days)
    months = sorted({since.strftime("%Y-%m"), datetime.now().strftime("%Y-%m")})
    found = []

    for month in months:
        path = os.path.join(_folder(), f"requests-{month}.jsonl")

        if not os.path.exists(path):
            continue

        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)

                    if datetime.fromisoformat(entry["at"]) >= since:
                        found.append(entry)
                except (ValueError, KeyError):
                    continue

    return found
