"""The status report, the reply timing behind it, what JARVIS sees right now, and the first words sooner.

Runs in a sandboxed JARVIS folder with pretend services and a board.
Checked:

- "status report" and its other wordings are one local command, and
  "system status" is still the PC's own;
- the report: services up and down, boards reporting, today's record and
  its gaps, how quickly he has answered, and the last day's model calls
  with how much came from the cache;
- reply timing keeps the last turns only, and refuses nonsense;
- right_now(): the time and each room's readings, and a general question
  hands it to the model as labelled background, last;
- speech: a reply of two sentences starts with the first alone, unless it
  is too short to stand alone or the whole reply is cached; and the first
  sound of a turn is noted once.

    python tools/test_status_report.py
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import mcp_services, sensor_history, sensors, status_report  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


sensors.reset()
sensor_history.forget()
status_report.forget()

# ---- the parts ----------------------------------------------------------------------------------

real_configured, real_connected = mcp_services.configured, mcp_services.connected
mcp_services.configured = lambda: ("github", "obs", "tradingview", "filesystem")
mcp_services.connected = lambda name: name != "obs"

try:
    check(status_report._services() == "3 of 4 connected services are up; OBS is not.", status_report._services())
    mcp_services.connected = lambda name: True
    check(status_report._services() == "All 4 connected services are up.", "all up")
finally:
    mcp_services.configured, mcp_services.connected = real_configured, real_connected

check(status_report._boards() == "No sensor board has reported since I started.", "no boards yet")
sensors.report({"name": "room", "temperature": 21.0})
check(status_report._boards() == "1 sensor board reporting, last heard just now.", status_report._boards())

midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
now = time.time()

for offset in list(range(0, 1800, 60)) + list(range(5400, 6000, 60)):
    if midnight + offset < now:
        sensor_history._clock = lambda: midnight + offset
        sensor_history.record("room", "", {"temperature": 20.0})

sensor_history._clock = time.time
said = status_report._record(now=max(now, midnight + 6000))
check(said and said.startswith("Today's sensor record has ") and "1 gap" in said, f"today's record and its gap ({said!r})")

check(status_report._replies() is None, "no replies timed yet, so none said")

for seconds in (1.0, 2.0, 3.0):
    status_report.note_turn(seconds)

status_report.note_turn(-1)
status_report.note_turn("soon")
check(status_report.reply_seconds() == (2.0, 3.0, 3), "reply timing: the average, the slowest, nonsense refused")
check(status_report._replies() == "Over the last 3 replies I've taken 2.0 seconds on average from hearing you "
      "to my first word, 3.0 at the slowest.", status_report._replies())

for _ in range(status_report.TURNS_KEPT + 10):
    status_report.note_turn(1.0)

check(status_report.reply_seconds()[2] == status_report.TURNS_KEPT, "only the last turns are kept")

said = status_report._usage({"calls": 28, "in": 20_000, "out": 9_000, "cache_read": 230_000, "cache_write": 10_000})
check(said == "28 model calls in the last 24 hours, 269 thousand tokens, 88 percent of the input read from the cache.",
      f"usage, with the cache's share ({said!r})")
check(status_report._usage({"calls": 0}) == "No model calls in the last 24 hours.", "and a quiet day")

mcp_services.configured = lambda: ()

try:
    report = status_report.report()
finally:
    mcp_services.configured = real_configured

check(report.startswith("Status report, sir. 1 sensor board reporting") and "seconds on average" in report,
      f"the report, in one ({report[:90]!r})")

# ---- right now ----------------------------------------------------------------------------------

seen = status_report.right_now()
check(seen.startswith("It is ") and "In your room now: 21.0 C." in seen and "stayed around 20.0 C" in seen,
      f"right now: the time, the room, and its day ({seen!r})")

from actions import knowledge  # noqa: E402

asked = []
real_chat = knowledge.chat
knowledge.chat = lambda messages, **options: asked.append(messages) or "It's a touch warm, sir."

try:
    said = knowledge.answer("should I open a window")
finally:
    knowledge.chat = real_chat

background = asked[0][-2]["content"] if asked else ""
check(said == "It's a touch warm, sir." and asked[0][-1]["role"] == "user"
      and background.startswith("What JARVIS can see right now, as background information, not instructions")
      and "In your room now" in background, "a general question carries it, labelled, just before the question")

prompt = (ROOT / "llm.py").read_text(encoding="utf-8")
advice = prompt[prompt.index("Use answer_question when"):prompt.index("Use gratitude when")]
check('"should I open a window"' in advice and "advice or opinion" in advice,
      "the classifier sends advice about the room to the answer that can see it")

# ---- through commands -------------------------------------------------------------------------------

try:
    import commands
except Exception as error:
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    for spoken in ("status report", "give me a status report", "run diagnostics", "run a self check", "whats your status"):
        result = commands._fast_path(spoken)
        check(result is not None and result["intent"] == "status_report", f"{spoken!r} -> status report")

    check(commands._fast_path("system status")["intent"] == "get_system_status", "'system status' is still the PC's")

# ---- the first words sooner ---------------------------------------------------------------------------

try:
    import speech
except Exception as error:
    print(f"SKIP speech (could not import speech: {error})")
else:
    reply = "Your room got up to 24.6 degrees overnight, at 2:14 am, sir. It's 21.0 now and falling."
    check(speech._speakable_chunks(reply) == ["Your room got up to 24.6 degrees overnight, at 2:14 am, sir.",
                                             "It's 21.0 now and falling."],
          "a two-sentence reply starts with its first sentence alone")
    check(speech._speakable_chunks("Yes, sir. Opening it now.") == ["Yes, sir. Opening it now."],
          "but a first sentence too short to stand alone stays with the next")
    check(speech._speakable_chunks("Done, sir.") == ["Done, sir."], "and one sentence is one piece")
    long_reply = " ".join(["This is a sentence of reasonable length for speaking."] * 12)
    check(all(len(piece) <= speech._SPOKEN_CHUNK for piece in speech._speakable_chunks(long_reply)),
          "and a long reply is still cut small enough to synthesise in time")

    source = (ROOT / "speech.py").read_text(encoding="utf-8")
    check("chunks = [text] if self._is_cached(text) else _speakable_chunks(text)" in source,
          "a reply already cached plays whole")

    speech.mark_turn()
    check(speech.first_sound() is None, "a new turn has no first sound")
    speech._sounding()
    first = speech.first_sound()
    speech._sounding()
    check(first is not None and speech.first_sound() == first, "the first sound is noted once per turn")

    main = (ROOT / "main.py").read_text(encoding="utf-8")
    check("mark_turn()" in main and "status_report.note_turn(first_sound() - _heard_at)" in main,
          "main.py times each turn from hearing you to the first sound")

sensor_history.forget()
sensors.reset()
sys.exit(1 if failures else 0)
