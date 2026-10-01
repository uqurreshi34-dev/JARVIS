"""Follow-ups to a sensor question: "and humidity?", "what about yesterday?".

Runs in a sandboxed JARVIS folder with a house plan, a board called "room"
in "my room" and a day of its readings. Checked:

- a fragment after a sensor question becomes the whole question: a new
  reading, a new stretch of time, highest or lowest, another room, "now",
  "in here", "the past 3 hours", "show me that again";
- each answer is the ordinary one, from the ordinary route, with no model
  call, and the fragment after it follows the newest question;
- what is not a follow-up is left alone: a word it cannot account for
  ("and the weather", "what about tomorrow", "and play some music"), a
  complete question, a fragment with no sensor question before it, after
  any other command, and after FOLLOW_SECONDS.

    python tools/test_followups.py
"""

import json
import math
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import followups, sensor_history, sensors  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


with open(os.path.join(folder, "house.json"), "w", encoding="utf-8") as handle:
    json.dump({"places": {"home": {"rooms": [
        {"name": "my room", "aliases": ["my bedroom", "room", "room 7"], "x": 0, "y": 0, "w": 3.8, "h": 3.4},
        {"name": "room 4", "aliases": ["room four"], "x": 4, "y": 0, "w": 3, "h": 3},
        {"name": "kitchen", "kind": "kitchen", "x": 0, "y": 4, "w": 3, "h": 3},
    ]}}}, handle)

sensors.reset()
sensor_history.forget()
followups.forget()

now = time.time()
moment = now - 2 * 86400

while moment <= now:
    sensor_history._clock = lambda: moment
    sensor_history.record("room", "", {"temperature": 22 + 3 * math.sin(moment / 9000),
                                       "humidity": 50 + 5 * math.sin(moment / 7000)})
    moment += 300

sensor_history._clock = time.time
sensors.report({"name": "room", "temperature": 22.0, "humidity": 50})

try:
    import commands
except Exception as error:
    print(f"SKIP: could not import commands ({error})")
    sys.exit(0)

commands.set_chart_listener(lambda image, title: None)
real_agent = commands.run_agent
commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))


def ask(text):
    """(intent, what was said) for [text], as said at the desk; (None, None) if nothing local took it."""
    result = commands.handle_command(text, fast_only=True)

    if result is None:
        return None, None

    return result["intent"], (result["action"]() if result.get("kind") == "query" else result.get("response"))


try:
    # ---- following on ------------------------------------------------------------------------

    intent, said = ask("how warm did my room get overnight")
    check(intent == "sensor_history" and said.startswith("Your room got up to"), f"the question ({said!r})")

    check(followups.rewrite("and humidity") == "what was the highest humidity in my room overnight",
          "'and humidity' is the same question, about humidity")
    intent, said = ask("and humidity")
    check(intent == "sensor_history" and said.startswith("Humidity in your room peaked at"), f"and answered so ({said!r})")

    check(followups.rewrite("what about yesterday") == "what was the highest humidity in my room yesterday",
          "'what about yesterday' follows the newest question, humidity")
    ask("what about yesterday")
    check(followups.rewrite("and the lowest") == "what was the lowest humidity in my room yesterday", "'and the lowest'")
    ask("and the lowest")

    intent, said = ask("how about room 4")
    check(said == "Room 4 has no sensor yet, sir.", f"'how about room 4': another room ({said!r})")
    check(followups.rewrite("and now") == "what's the temperature in room 4" or
          followups.rewrite("and now") == "what's the humidity in room 4", "'and now': the reading now")
    check(followups.rewrite("what about my bedroom") == "what was the lowest humidity in my room yesterday",
          "a room by its alias")

    ask("what was the temperature in here today")
    check(followups.rewrite("and the past 3 hours") == "what was the temperature in here in the last 3 hours",
          "'in here' and 'the past 3 hours'")
    check(followups.rewrite("show me that again") == "what was the temperature in here today", "'show me that again'")

    ask("is anyone in my room")
    check(followups.rewrite("and last night") == "was anyone in my room last night", "movement, then 'and last night'")

    # ---- left alone --------------------------------------------------------------------------

    ask("how warm did my room get overnight")

    for spoken in ("and the weather", "what about tomorrow", "and play some music", "and open chrome",
                   "what about the news", "and", "thanks"):
        check(followups.rewrite(spoken) is None, f"not a follow-up: {spoken!r}")

    check(followups.rewrite("what was the humidity in my room today") is None, "a complete question is asked as it is")

    ask("what's the time")
    check(followups.rewrite("and humidity") is None, "any other command in between ends it")

    ask("how warm did my room get overnight")
    followups._context["at"] -= followups.FOLLOW_SECONDS + 1
    check(followups.rewrite("and humidity") is None, "and so does a minute's silence")

    followups.forget()
    check(followups.rewrite("and humidity") is None, "and with no sensor question before it, nothing")

finally:
    commands.run_agent = real_agent
    commands.set_chart_listener(None)
    sensor_history.forget()
    sensors.reset()

sys.exit(1 if failures else 0)
