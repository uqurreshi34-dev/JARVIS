"""Asking the sensors aloud: "what's the temperature in the room".

Answered from what the boards last reported, with no model call. Rooms are
the boards' own names, so nothing is listed in advance. Checked:

- a question naming a board, or "in here", is answered from its readings;
- one naming no board, or a board that has not reported, is left alone --
  "what's the temperature outside" is still the weather;
- a command that merely mentions a sensor is not taken for a question;
- presence, humidity, several rooms, and a sensor gone quiet are said
  plainly, one sentence per room;
- commands.py answers it on the fast path, before any model is asked.

    python tools/test_sensor_questions.py
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import sensors  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


clock = [1000.0]
sensors._now = lambda: clock[0]
sensors.set_listener(None)
sensors.reset()

check(sensors.question("what's the temperature in the room") is None,
      "with no boards heard from, no room can be asked after")

for name, temperature, humidity in (("room", 21.4, 46), ("kitchen", 23.1, 52)):
    sensors.report({"name": name, "event": "online"})
    sensors.report({"name": name, "temperature": temperature, "humidity": humidity})

sensors.report({"name": "hall", "event": "online"})
clock[0] += 65
sensors.report({"name": "room", "event": "motion"})
clock[0] += 120

ASKED = {
    "what's the temperature in the room": ("temperature", ["room"]),
    "Jarvis, how warm is the kitchen?": ("temperature", ["kitchen"]),
    "is it cold in here": ("temperature", ["room", "kitchen", "hall"]),
    "how humid is it in the kitchen": ("humidity", ["kitchen"]),
    "is anyone in the room": ("presence", ["room"]),
    "is anybody in the kitchen": ("presence", ["kitchen"]),
    "how are the sensors": ("everything", ["room", "kitchen", "hall"]),
}

for said, expected in ASKED.items():
    asked = sensors.question(said)
    check(asked is not None and asked[0] == expected[0] and sorted(asked[1]) == sorted(expected[1]),
          f"{said!r} -> {asked}")

LEFT_ALONE = [
    "what's the temperature outside",
    "what's the weather",
    "is anyone there",
    "remind me to check the sensor wiring",
    "write a note about the kitchen temperature",
    "what's the temperature in the garage",     # no such board
    "open the kitchen",
]

for said in LEFT_ALONE:
    check(sensors.question(said) is None, f"left alone: {said!r}")

said = sensors.answer(sensors.question("what's the temperature in the room"))
check(said == "The room is 21.4 degrees, sir.", f"a temperature ({said!r})")

said = sensors.answer(sensors.question("how humid is it in the kitchen"))
check(said == "Humidity in the kitchen is 52 percent, sir.", f"a humidity ({said!r})")

said = sensors.answer(sensors.question("is anyone in the room"))
check(said == "Someone is in the room, movement 2 minutes ago, sir.", f"someone present ({said!r})")

said = sensors.answer(sensors.question("is anyone in the kitchen"))
check(said == "No movement seen in the kitchen yet, sir.", f"no movement yet ({said!r})")

said = sensors.answer(sensors.question("what's the temperature in the hall"))
check(said == "I don't have a temperature from the hall yet, sir.", f"a board with no reading says so ({said!r})")

said = sensors.answer(sensors.question("how are the sensors"))
import re  # noqa: E402

sentences = re.split(r"(?<=\.)\s+", said)
check(len(sentences) == 3 and said.count("sir") == 1 and sentences[0] == "No movement seen in the hall yet, sir."
      and sentences[1].startswith("The kitchen is 23.1 degrees with 52 percent humidity"),
      f"several rooms, one sentence each, 'sir' once ({said!r})")

clock[0] += sensors.PRESENT_SECONDS + 300
said = sensors.answer(sensors.question("is anyone in the room"))
check(said.startswith("The room looks empty, no movement for") and "last reported" in said,
      f"an empty room, and a sensor gone quiet, are said so ({said!r})")

# The fast path, before any model.
try:
    import commands
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    sensors.reset()
    clock[0] = 5000.0
    sensors.report({"name": "room", "temperature": 20.5, "humidity": 44})

    routed = commands._fast_path("what's the temperature in the room")
    check(routed is not None and routed["intent"] == "sensor_question", "commands: asked on the fast path")

    routed = commands._fast_path("what's the weather")
    check(routed is None or routed["intent"] != "sensor_question", "and the weather is still the weather")

    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        result = commands.handle_command("what's the temperature in the room")
        said = result["action"]() if result else None
        check(said == "The room is 20.5 degrees, sir.", f"answered with no model call ({said!r})")
    finally:
        commands.run_agent = real_agent

sys.exit(1 if failures else 0)
