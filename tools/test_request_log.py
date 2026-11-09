"""One record per request (request_log.py), and the report read back from it (actions/request_report.py).

Nothing is fetched and no model is asked. Checked:

- a request records how it was heard, which way it went, its AI calls and their times, what was spoken,
  what failed over, how long it took, and which parts went online;
- no words are kept in the record;
- something heard that was not for JARVIS, and a failover outside a request, get their own lines;
- a second request during the first is recorded too, marked overlapped;
- the spoken report, the status line and the table say what the records say.

    python tools/test_request_log.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

import request_log  # noqa: E402
from actions import request_report  # noqa: E402

failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


check(request_log._folder() == sandbox.directory(), "in tests the record is kept in the sandbox")
check(request_report.report(7).startswith("I have no requests on record"), "no records: said plainly")
check(request_report.line_for_status() is None, "and the status report says nothing of it")

# A desk request: heard by Groq, understood by the AI, answered by Groq after Gemini failed, spoken by Google.
request_log.heard("groq-whisper", True, ms=412.6, audio_seconds=1.234, how="wake model")
desk = request_log.begin("desk")
request_log.route("question", free_path=False)
request_log.route("time", free_path=True)
request_log.model_call("gemini", "gemini-3.6-flash", "classify", {"in": 300, "out": 20})
request_log.model_time("gemini", 900)
request_log.fallback("gemini>groq")
request_log.model_call("groq", "openai/gpt-oss-20b", "answer", {"in": 500, "out": 80})
request_log.model_time("groq", 650)
request_log.voice("google", True, 42, ms=380.4)
record = request_log.end(desk, "answered", to_first_word=1.75, decide=0.9, respond=0.8)

check(record["heard"] == {"engine": "groq-whisper", "online": True, "ms": 412, "audio_s": 1.23,
                          "how": "wake model", "fell_back": False}, "how it was heard is kept")
check(record["intent"] == "question" and record["route"] == "model", "the first way it was understood is kept")
check([call["ms"] for call in record["brain"]] == [900, 650], "each AI call has its own time")
check(record["fallbacks"] == ["gemini>groq"], "the failover is kept with the request")
check(record["online"] == ["hearing", "ai", "voice"], "hearing, the AI and the voice all went online")
check(record["ms"]["to_first_word"] == 1750 and "total" in record["ms"], "how long it took is kept")
check(request_log.end(desk) is None, "ending it twice writes nothing more")

# A desk request that stayed on the PC: heard locally, a phrase JARVIS knows, the Kokoro voice.
request_log.heard("whisper", False, ms=600, audio_seconds=1.0, how="follow-up")
local = request_log.begin("desk")
request_log.route("time", free_path=True)
request_log.voice("kokoro", False, 20, ms=250)
request_log.end(local, "answered", to_first_word=0.9)

# A transcription from long ago is not the next request's.
request_log.heard("whisper", False)
request_log._state["heard"]["at"] -= request_log._HEARD_FRESH_SECONDS + 1
stale = request_log.begin("desk")
check(stale["heard"] is None, "an old transcription is not taken for a new request")

# The phone during a desk request.
phone = request_log.begin("phone")
check(phone.get("overlapped") and phone["heard"] is None, "a phone request during a desk one is marked overlapped")
request_log.end(phone, "answered")
request_log.end(stale, "not understood")

request_log.ignored("no wake word", engine="wake model", online=False, audio_seconds=3.0)
request_log.ignored("written out, no name", engine="groq-whisper", online=True, audio_seconds=2.0)
request_log.fallback("groq-whisper>local")

with open(request_log._ledger(datetime.now()), encoding="utf-8") as handle:
    text = handle.read()

lines = [json.loads(line) for line in text.splitlines()]
check([line["kind"] for line in lines] == ["request"] * 4 + ["ignored", "ignored", "fallback"],
      "one line per request, ignored sound and lone failover")
check(all("at" in line for line in lines) and all("started" not in line for line in lines), "each dated")
check("what time" not in text.casefold() and "text" not in lines[0], "no words are kept")

numbers = request_report.summary(7)
check(numbers["requests"] == 4 and numbers["by_source"] == {"desk": 3, "phone": 1}, "requests counted by source")
check(numbers["online"] == 1 and numbers["online_ai"] == 1 and numbers["online_voice"] == 1, "one went online")
check(numbers["understood_locally"] == 1, "one was understood with no AI")
check(numbers["tokens"] == 900 and numbers["ai_by"] == {"gemini": 1, "groq": 1}, "AI calls and tokens counted")
check(numbers["fallbacks"] == {"gemini>groq": 1, "groq-whisper>local": 1}, "failovers counted, in or out of a request")
check(numbers["kept_off_line"] == 1 and numbers["sent_then_ignored"] == 1, "ignored sound counted both ways")
check(numbers["voice_chars_online"] == 42, "characters sent to the online voice counted")

said = request_report.report(7)
print("   ", said)
check("You asked me 4 things in the last 7 days" in said, "the report counts the requests")
check("1 of them, 25 percent, needed the internet: the AI for 1, hearing you for 1, my voice for 1." in said,
      "says how many needed the internet and for what")
check("3 stayed entirely on this PC" in said, "and how many stayed on the PC")
check("1.8 seconds typically" in said or "1.3 seconds typically" in said, "how long to the first word")
check("The wake word kept 1 thing said in the room on this PC" in said, "what the wake word kept here")
check("Parts failed over 2 times" in said and "gemini to groq 1 time" in said, "and what failed over")
check(request_report.line_for_status() == "1 of today's 4 requests needed the internet.", "the status report line")
check("needed the internet" in request_report.table(7) and "AI tokens" in request_report.table(7), "the table")

check("1 thing was sent to be written out and was not for me" in said, "and what was sent and not for him")
check(sandbox.assert_clean(), "the real JARVIS folder was not touched")
sys.exit(1 if failures else 0)
