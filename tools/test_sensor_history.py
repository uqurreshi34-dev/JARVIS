"""Sensor history: "how warm did my room get overnight", and nudges.

Runs in a sandboxed JARVIS folder with a house plan, a board called "room"
in "my room", and a clock held still at 9:30 am. Checked:

- stretches of time as they are said: overnight and last night (10 pm to
  7 am, and "overnight" at 11 pm is still last night), today, yesterday,
  this morning, since this morning, the last 6 hours, the last half hour,
  a couple of hours, tonight before it starts; sensors.json moves them;
- what is a question for the record and what is left alone: no stretch,
  no room, the future, the weather;
- answers: the highest and when, the lowest and when, the range with the
  average and now, movement and when, a room with no sensor, a sensor that
  was not reporting, a record that starts partway through;
- a chart is drawn for each, and nothing is said for an empty stretch;
- nudges: humidity over 70 for two hours says so once, not before, not
  across a hole in the record, not again for six hours, never in quiet
  hours; sensors.json changes the limits and the words;
- sensors.report keeps each reading and hands back a nudge;
- through commands: no model call, the chart shown, and "what's the
  temperature in my room" still the reading now.

    python tools/test_sensor_history.py
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import folder_organizer, sensor_history, sensors  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def at(day, hour, minute=0):
    """A moment on 1 October 2026 (day 0), or the days either side."""
    return (datetime(2026, 10, 1) + timedelta(days=day, hours=hour, minutes=minute)).timestamp()


NOW = at(0, 9, 30)
clock = {"now": NOW}
sensor_history._clock = lambda: clock["now"]


def hours(stretch):
    return round((stretch["end"] - stretch["start"]) / 3600, 2)


with open(os.path.join(folder, "house.json"), "w", encoding="utf-8") as handle:
    json.dump({"places": {"home": {"rooms": [
        {"name": "my room", "aliases": ["my bedroom", "room"], "x": 0, "y": 0, "w": 3.8, "h": 3.4},
        {"name": "kitchen", "kind": "kitchen", "x": 4, "y": 0, "w": 3, "h": 3},
        {"name": "front room", "x": 0, "y": 4, "w": 4, "h": 3},
    ]}}}, handle)

sensors.reset()
sensor_history.forget()

# ---- stretches of time -------------------------------------------------------------------

stretch = sensor_history.window("how warm did my room get overnight")
check(stretch["start"] == at(-1, 22) and stretch["end"] == at(0, 7) and stretch["spoken"] == "overnight",
      "overnight, asked in the morning: 10 pm last night to 7 am")
check(sensor_history.window("was it cold last night")["spoken"] == "last night", "last night is said back as last night")
check(sensor_history.window("overnight", now=at(0, 23, 15))["start"] == at(-1, 22),
      "overnight at 11 pm is still last night, not the last hour")
check(sensor_history.window("overnight", now=at(0, 3))["end"] == at(0, 3), "and in the small hours, the night so far")
check(sensor_history.window("what about today")["start"] == at(0, 0) and sensor_history.window("today")["end"] == NOW,
      "today: midnight until now")
check((sensor_history.window("yesterday")["start"], sensor_history.window("yesterday")["end"]) == (at(-1, 0), at(0, 0)),
      "yesterday: all of it")
check(sensor_history.window("this morning")["start"] == at(0, 6) and sensor_history.window("this morning")["end"] == NOW,
      "this morning, so far")
check(sensor_history.window("since this morning")["spoken"] == "since this morning", "since this morning")
check(sensor_history.window("yesterday evening")["start"] == at(-1, 18), "yesterday evening")
check(hours(sensor_history.window("over the last 6 hours")) == 6, "the last 6 hours")
check(hours(sensor_history.window("in the last two hours")) == 2, "the last two hours, in words")
check(hours(sensor_history.window("the last couple of hours")) == 2, "a couple of hours")
check(hours(sensor_history.window("the last twenty four hours")) == 24, "twenty four hours")
check(hours(sensor_history.window("the past hour")) == 1 and sensor_history.window("the past hour")["spoken"] == "in the last hour",
      "the past hour, said as the last hour")
check(hours(sensor_history.window("in the last half hour")) == 0.5, "the last half hour")
check(sensor_history.window("in the last 3 days")["spoken"] == "in the last 3 days", "the last 3 days")
check(sensor_history.window("tonight")["start"] is None, "tonight, asked in the morning, has not begun")
check(sensor_history.window("tonight", now=at(0, 21))["start"] == at(0, 18), "tonight at 9 pm is since the evening")
check(sensor_history.window("tonight", now=at(0, 1))["start"] == at(-1, 18), "and at 1 am, the evening before")
check(sensor_history.window("how warm is my room") is None, "no stretch of time, no window")

with open(os.path.join(folder, "sensors.json"), "w", encoding="utf-8") as handle:
    json.dump({"overnight": {"from": "23:30", "to": "06:00"}}, handle)

moved = sensor_history.window("overnight")
check(moved["start"] == at(-1, 23, 30) and moved["end"] == at(0, 6), "sensors.json moves the night")

with open(os.path.join(folder, "sensors.json"), "w", encoding="utf-8") as handle:
    json.dump({"overnight": {"from": "late", "to": "06:00"}}, handle)

check(sensor_history.window("overnight")["start"] == at(-1, 22), "and a time it cannot read falls back to the default")
os.remove(os.path.join(folder, "sensors.json"))

# ---- a night of readings --------------------------------------------------------------------

def feed(start, end, every_minutes, value_at, board="room", field="temperature"):
    clock_now = clock["now"]
    moment = start

    while moment <= end:
        clock["now"] = moment
        sensor_history.record(board, "", {field: value_at(moment)})
        moment += every_minutes * 60

    clock["now"] = clock_now


def night_temperature(moment):
    # 20 at 10 pm, up to 24.6 at 2:14 am, down to 18 by 7 am.
    peak = at(0, 2, 14)
    return 24.6 - abs(moment - peak) / 3600 * (1.0 if moment < peak else 0.95)


feed(at(-1, 21), at(0, 9, 30), 2, night_temperature)
feed(at(0, 2, 14), at(0, 2, 14), 1, lambda _: 24.6)
feed(at(-1, 21), at(0, 9, 30), 5, lambda moment: 55 + (moment - at(-1, 21)) / 3600, field="humidity")

for minute in (0, 1, 2, 40, 41):
    clock["now"] = at(0, 3, minute)
    sensor_history.record("room", "motion", {})

clock["now"] = NOW
sensors.report({"name": "room", "temperature": 21.0, "humidity": 60})

said, data, title, boards = sensor_history.answer("how warm did my room get overnight")
check(said == "Your room got up to 24.6 degrees overnight, at 2:14 am, sir.", f"the highest, and when ({said!r})")
check(bool(data) and data.startswith(b"\x89PNG") and title == "YOUR ROOM \u00b7 TEMPERATURE \u00b7 OVERNIGHT" and boards == ["room"],
      "with a chart of it")

said = sensor_history.answer("what was the coldest my bedroom got last night")[0]
check(said.startswith("Your room got down to ") and "last night, at 7 am" in said, f"the lowest, and when ({said!r})")

said = sensor_history.answer("how cold did it get in my room overnight")[0]
check(said.startswith("Your room got down to"), f"'how cold did it get' is the lowest ({said!r})")

said = sensor_history.answer("what was the temperature in my room today")[0]
check(said.startswith("Your room ranged from ") and "averaging" in said and "It's 17.7 now and falling" in said,
      f"the range, the average, and now ({said!r})")

said = sensor_history.answer("what was the humidity in my room overnight")[0]
check(said.startswith("Humidity in your room ranged from 56 to 65 percent overnight"), f"humidity too ({said!r})")

said = sensor_history.answer("how humid did my room get overnight")[0]
check(said.startswith("Humidity in your room peaked at 65 percent overnight, at 7 am"), f"and its peak ({said!r})")

said, data, _, _ = sensor_history.answer("was anyone in my room overnight")
check(said == "Movement in your room overnight, 2 separate times: the first at 3 am, the last at 3:41 am, sir.",
      f"movement, how often and when ({said!r})")
check(bool(data), "with a chart of when")

said = sensor_history.answer("was anyone in my room yesterday")[0]
check(said == "Movement in your room yesterday, sir." or said.startswith("No movement in your room yesterday"),
      f"none yesterday ({said!r})")

said, data, _, _ = sensor_history.answer("how warm did the kitchen get overnight")
check(said == "The kitchen has no sensor yet, sir." and data is None, f"a room with no sensor says so ({said!r})")

said, data, _, _ = sensor_history.answer("what was the temperature in my room in the last 3 days")
check("I've only been keeping a record since" in said, f"a record that starts partway through says so ({said!r})")

clock["now"] = at(1, 9, 30)
said, data, _, _ = sensor_history.answer("what was the temperature in my room in the last hour")
check(said == "I have no record from your room in the last hour; its sensor wasn't reporting, sir." and data is None,
      f"a sensor that wasn't reporting ({said!r})")
clock["now"] = NOW

said = sensor_history.answer("what was the temperature in here overnight")[0]
check(said.startswith("Your room ranged from"), f"'in here' means the boards there are ({said!r})")

said = sensor_history.answer("how warm did my room get this evening")[0]
check(said == "It isn't this evening yet, sir.", f"a stretch not yet begun ({said!r})")

# ---- what is and is not a question for the record ----------------------------------------------

for spoken in ("how warm did my room get overnight", "jarvis what was the humidity in my room today please",
               "show me the temperature in my room over the last 6 hours", "was anyone in my room last night",
               "graph my room's temperature today"):
    check(sensor_history.question(spoken) is not None, f"asked of the record: {spoken!r}")

for spoken in ("how warm is my room", "how cold did it get last night", "what's the weather today",
               "how warm will my room be tonight", "remind me to check the room in 2 hours",
               "what did I do yesterday", "how cold is it outside today"):
    check(sensor_history.question(spoken) is None, f"left alone: {spoken!r}")

check(sensor_history.question("how warm did the front room get overnight")["where"] == "The front room has no sensor yet, sir.",
      "'front room' is never 'room'")

# ---- nudges ---------------------------------------------------------------------------------------

sensor_history.forget()
said = []


def damp(start_hour, end_hour, value=74, day=0, every=5, gap=None):
    moment = at(day, start_hour)

    while moment <= at(day, end_hour):
        if not (gap and gap[0] <= moment < gap[1]):
            clock["now"] = moment
            spoken = sensor_history.record("room", "", {"humidity": value})

            if spoken:
                said.append((moment, spoken))

        moment += every * 60


damp(10, 11, value=60)
damp(11, 13, value=74)
check(len(said) == 1 and said[0][0] == at(0, 13), f"humidity over 70 for two hours: said once, at two hours ({said and said[0]})")
check(said and said[0][1] == "Humidity in your room has been over 70 percent for 2 hours, sir. "
      "It's 74 percent now; opening a window would help.", f"in its own words ({said and said[0][1]!r})")

damp(13, 18, value=74)
check(len(said) == 1, "and not again within six hours")
damp(18, 20, value=74)
check(len(said) == 2 and said[1][0] == at(0, 19), "but again after six")

sensor_history.forget()
said.clear()
damp(9, 10, value=60)
damp(10, 13, value=74, gap=(at(0, 10, 30), at(0, 11, 10)))
check(not said or said[0][0] >= at(0, 13, 10), f"never across a hole in the record ({said and said[0][0]})")

sensor_history.forget()
said.clear()
damp(22, 23, value=60, day=-1)
damp(23, 23, value=74, day=-1)
moment = at(-1, 23)

while moment <= at(0, 6, 55):
    clock["now"] = moment
    spoken = sensor_history.record("room", "", {"humidity": 74})

    if spoken:
        said.append((moment, spoken))

    moment += 300

check(not said, "never in quiet hours")
clock["now"] = at(0, 7)
spoken = sensor_history.record("room", "", {"humidity": 74})
check(spoken and spoken.startswith("Humidity in your room"), "and said once they end, if it still holds")

sensor_history.forget()

with open(os.path.join(folder, "sensors.json"), "w", encoding="utf-8") as handle:
    json.dump({"quiet_hours": {"from": "00:00", "to": "00:00"},
               "nudges": [{"reading": "temperature", "above": 25, "for_minutes": 30,
                           "say": "{Place} is warm: {value} degrees for {duration}."},
                          {"reading": "humidity", "for_minutes": 30}]}, handle)

said.clear()
feed(at(0, 14), at(0, 14, 25), 5, lambda _: 26.2)
clock["now"] = at(0, 14, 30)
spoken = sensor_history.record("room", "", {"temperature": 26.2})
check(spoken == "Your room is warm: 26.2 degrees for 30 minutes.", f"sensors.json sets the limits and the words ({spoken!r})")
check(len(sensor_history.settings()["nudges"]) == 1, "and a nudge with no limit is left out")
os.remove(os.path.join(folder, "sensors.json"))

# ---- through sensors.report ---------------------------------------------------------------------

sensor_history.forget()
sensors.reset()
sensors.set_recorder(sensor_history.record)

try:
    for minute in range(0, 125, 5):
        clock["now"] = at(0, 12, minute)
        heard = sensors.report({"name": "room", "humidity": 80})

    check(heard and heard.startswith("Humidity in your room has been over 70 percent"),
          f"sensors.report keeps each reading and says the nudge ({heard!r})")
    check(sensor_history._rows("SELECT COUNT(*) FROM readings")[0][0] == 25, "every reading kept")
    check(sensors.report({"name": "room", "event": "online"}) is None
          and sensor_history._rows("SELECT COUNT(*) FROM readings")[0][0] == 25,
          "an online with no readings keeps nothing")
finally:
    sensors.set_recorder(None)

check(folder_organizer.is_protected("sensor-history.sqlite") and folder_organizer.is_protected("sensors.json")
      and folder_organizer.is_protected("sensor-history.sqlite-journal"), "the folder guard never moves the record")

# ---- keeping only so long ------------------------------------------------------------------------

sensor_history.forget()
clock["now"] = at(-40, 12)
sensor_history.record("room", "", {"temperature": 20})
clock["now"] = at(0, 12)
sensor_history._pruned_at = None
sensor_history.record("room", "", {"temperature": 21})
check(sensor_history._rows("SELECT COUNT(*) FROM readings")[0][0] == 1, "readings older than keep_days are cleared out")

# ---- through commands ---------------------------------------------------------------------------

sensor_history.forget()
clock["now"] = NOW
feed(at(-1, 21), at(0, 9, 30), 5, night_temperature)

try:
    import commands
except Exception as error:
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    shown = []
    commands.set_chart_listener(lambda image, title: shown.append(title))
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        result = commands.handle_command("how warm did my room get overnight")
        said = result["action"]()
        check(result["intent"] == "sensor_history" and said.startswith("Your room got up to")
              and shown == ["YOUR ROOM \u00b7 TEMPERATURE \u00b7 OVERNIGHT"], f"commands: answered and charted, no model call ({said!r})")
        now_result = commands._fast_path("what's the temperature in my room")
        check(now_result and now_result["intent"] == "house_question", "and the reading now is still the plan's")
    finally:
        commands.run_agent = real_agent
        commands.set_chart_listener(None)

sensor_history.forget()
sensors.reset()
sys.exit(1 if failures else 0)
