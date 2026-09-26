"""The house hologram: "show me the house", then the rooms by their own names.

Runs in a sandboxed JARVIS folder. Checked:

- house.json: a starter plan is written the first time the house is asked
  for, and never over one that exists; a room with no size is left off,
  not guessed; both files are JARVIS's own and never sorted away;
- what is said: show, a place by name, close (and "closed", as speech
  recognition writes it), switch; a room only by its house.json name or
  alias, after a verb that asks after a room, so "tell me about the kitchen
  sink", "what's the temperature in the kitchen", "show me my files" and
  the rest go on to their own commands;
- what is said back: rooms, floors and sensors online; a board that is not
  on the plan; a room's readings, movement and camera; a room with no
  sensor; a board that has not reported;
- places: the same board placed by whichever place is in use, and the
  place shown is remembered;
- a report redraws it while it is up; a sensor question lights its room;
- the panel draws the plan, a tap on a room or its row names it, settled
  frames do not redraw the plan, and the SAY: line uses the house's rooms;
- through commands: no model call.

    python tools/test_house.py
"""

import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import folder_organizer, house, sensors  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


config_path = os.path.join(folder, house.CONFIG_NAME)
state_path = os.path.join(folder, house.STATE_NAME)

for name in (house.CONFIG_NAME, house.STATE_NAME):
    if os.path.exists(os.path.join(folder, name)):
        os.remove(os.path.join(folder, name))

sensors.reset()
shown, hidden = [], []
house.set_listeners(on_view=shown.append, on_hide=lambda: hidden.append(True))

# ---- what is said -----------------------------------------------------------------------------

SAID = {
    "show me the house": ("show", None),
    "jarvis show the house please": ("show", None),
    "open the house hologram": ("show", None),
    "bring up the floor plan": ("show", None),
    "close the house": ("close", None),
    "closed the house": ("close", None),
    "hide the floor plan": ("close", None),
    "what's happening in my room": ("room", ("home", "my room")),
    "show me my room": ("room", ("home", "my room")),
    "show me the bedroom": ("room", ("home", "my room")),
    "show me the kitchen": ("room", ("home", "kitchen")),
    "how's the landing": ("room", ("home", "landing")),
    "zoom in on the hall": ("room", ("home", "hall")),
    "what's going on in the living room": ("room", ("home", "front room")),
    "tell me about the landing": ("room", ("home", "landing")),
    "switch the house to home": ("switch", "home"),
}

for spoken, meant in SAID.items():
    check(house.asked(spoken) == meant, f"{spoken!r} -> {house.asked(spoken)}")

for spoken in ("tell me about the kitchen sink", "what's the temperature in the kitchen", "show me my files",
               "set a timer for five minutes", "open chrome", "how are you", "show me the news",
               "what's the weather at home", "close it", "remind me to clean the kitchen", "go home",
               "tell me about the bathroom"):
    check(house.asked(spoken) is None, f"left alone: {spoken!r} -> {house.asked(spoken)}")

check(not os.path.exists(config_path), "asking what was said writes nothing")

# ---- show ----------------------------------------------------------------------------------------

said = house.show()
check(os.path.exists(config_path), "the first show writes a starter house.json")
check(said == "Your house, sir: two rooms, a kitchen and a landing over two floors, no sensors online.",
      f"and says what is there, rooms counted as rooms ({said!r})")
view = shown[-1]
check(len(view["rooms"]) == 5 and view["floors"] == [0, 1] and view["title"] == "HOME", "the view has the plan")
check(view["counts"] == {"room": 2, "kitchen": 1, "landing": 1}, f"a hall is a passage, not a room ({view['counts']})")
check(not any("bathroom" in room["name"] for room in view["rooms"]), "and the starter has no bathroom")
check(view["boards"]["room"]["state"] == "waiting" and view["boards"]["cam"]["camera"],
      "the board and camera it names are waiting to report")
check(folder_organizer.is_protected("house.json") and folder_organizer.is_protected(".jarvis-house.json"),
      "house.json and which place is in use are never sorted away")

with open(config_path, encoding="utf-8") as handle:
    starter = json.load(handle)

starter["places"]["home"]["rooms"][0]["name"] = "my room"
starter["places"]["home"]["rooms"].append({"name": "attic", "floor": 2})      # no size
with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(starter, handle)

house.config(create=True)

with open(config_path, encoding="utf-8") as handle:
    check(any(room["name"] == "attic" for room in json.load(handle)["places"]["home"]["rooms"]),
          "an existing house.json is never written over")

house.show()
check(not any(room["name"] == "attic" for room in shown[-1]["rooms"]), "a room with no size is left off, not guessed")

# ---- a report redraws it ------------------------------------------------------------------------------

sensors.add_listener(house.refresh)
before = len(shown)
sensors.report({"name": "room", "event": "online"})
sensors.report({"name": "room", "temperature": 21.4, "humidity": 48})
sensors.report({"name": "room", "event": "motion"})
check(len(shown) == before + 3, f"each report redraws it ({len(shown) - before})")
check(shown[-1]["boards"]["room"]["state"] == "live" and shown[-1]["boards"]["room"]["readings"]["temperature"] == 21.4,
      "with the board live and its reading")

sensors.report({"name": "garage", "temperature": 12.0})
said = house.show()
check(said.endswith("The garage sensor is not on the plan yet; name it after a room, or add it to house.json.")
      and [item["name"] for item in shown[-1]["unplaced"]] == ["garage"], f"a board not on the plan says so ({said!r})")

# A board named after a room is in it, with nothing added to house.json.
sensors.report({"name": "kitchen", "temperature": 19.5, "humidity": 55})
drawn = house.render("home")
kitchen = next(room for room in drawn["rooms"] if room["name"] == "kitchen")
check([board["name"] for board in kitchen["boards"]] == ["kitchen"] and drawn["boards"]["kitchen"]["room"] == "kitchen",
      "a board called kitchen lights the kitchen, found by its name")
check(house.describe_room("home", "kitchen") == "Kitchen: 19.5 degrees, 55 percent humidity, sir.",
      f"and the kitchen reads it ({house.describe_room('home', 'kitchen')!r})")
sensors.report({"name": "hallway", "temperature": 18.0})
check("hallway" in [item["name"] for item in house.render("home")["unplaced"]],
      "a passage never takes a board: a hallway board is not on the plan")
check(house.spoken_spaces({"room": 7, "kitchen": 1, "landing": 1}) == "seven rooms, a kitchen and a landing",
      "seven rooms, a kitchen and a landing, as it is said")

# ---- rooms ----------------------------------------------------------------------------------------------

said = house.answer("what's happening in my room")
check(said == "My room: 21.4 degrees, 48 percent humidity, someone there, movement just now, "
              "the street camera isn't connected yet, sir.", f"a room's readings, movement and camera ({said!r})")
check(shown[-1]["focus"] == "my room", "and it comes into focus")

said = house.answer("show me the front room")
check(said == "Front room has no sensor yet, sir." and shown[-1]["focus"] == "front room", f"a room with no sensor ({said!r})")

sensors.report({"name": "cam", "event": "online"})
check(house.describe_room("home", "my room").endswith("the street camera is online, sir."), "a camera that reports is online")

house.tapped("landing")
check(shown[-1]["focus"] == "landing", "a tap lights a room")
house.tapped("landing")
check(shown[-1]["focus"] is None, "and a second tap puts it out")

house.follow(["room"])
check(shown[-1]["focus"] == "my room", "a sensor question about the room's board lights the room")

check(house.close() == "House closed, sir." and hidden and not house.showing(), "close the house puts it away")
check(house.asked("close it") is None, "and 'close it' is nobody's again")

said = house.answer("how's the landing")
check(house.showing() and shown[-1]["focus"] == "landing" and said == "Landing has no sensor yet, sir.",
      "asking after a room with the house away shows it, focused")

# Floors named as the house is spoken of.
with open(config_path, encoding="utf-8") as handle:
    data = json.load(handle)

data["places"]["home"]["floor_names"] = {"0": "1st floor", "1": "2nd floor", "bad": "x"}
with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(data, handle)

check(house.render("home")["floor_names"] == {0: "1ST FLOOR", 1: "2ND FLOOR"}, "floor names come from the house")

# ---- places ---------------------------------------------------------------------------------------------

with open(config_path, encoding="utf-8") as handle:
    data = json.load(handle)

data["places"]["mums house"] = {
    "aliases": ["mum's house", "mums"],
    "rooms": [{"name": "lounge", "x": 0, "y": 0, "w": 5, "h": 4, "boards": [{"name": "room"}]},
              {"name": "spare room", "floor": 1, "x": 0, "y": 0, "w": 3, "h": 3}],
}
with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(data, handle)

check(house.asked("show me mum's house") == ("show", "mums house"), "a place by its own name")
said = house.answer("show me mum's house")
check(said.startswith("Mum's house, sir: two rooms over two floors, one sensor online.")
      and shown[-1]["boards"]["room"]["room"] == "lounge", f"the same board, placed by that place ({said!r})")
check(house.asked("what's happening in the lounge") == ("room", ("mums house", "lounge")), "and its rooms are askable")

with open(state_path, encoding="utf-8") as handle:
    check(json.load(handle)["active"] == "mums house", "the place shown is remembered")

house.close()
house.show()
check(shown[-1]["place"] == "mums house", "and 'show me the house' shows it next time")
check(house.answer("switch the house to home") == "The sensors now belong to home, sir." and shown[-1]["place"] == "home",
      "switching back")

# A typo in house.json: said so, and the file is left as it is.
with open(config_path, encoding="utf-8") as handle:
    good = handle.read()

with open(config_path, "w", encoding="utf-8") as handle:
    handle.write(good[:-5])

house.close()
check(house.show() == "I can't read house.json, sir; there may be a typo in it.", "a typo in house.json is said so")

with open(config_path, encoding="utf-8") as handle:
    check(handle.read() == good[:-5], "and the file is not written over")

with open(config_path, "w", encoding="utf-8") as handle:
    handle.write(good)

# ---- the panel -------------------------------------------------------------------------------------------

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QImage, QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import house_panel  # noqa: E402

panel = house_panel.HousePanel()
house.set_listeners(on_view=panel.show_view.emit, on_hide=panel.hide_view.emit)
house.show("home")
app.processEvents()
check(panel.isVisible(), "the hologram appears")
panel._shown_at -= 5

image = QImage(panel.size(), QImage.Format.Format_ARGB32)
image.fill(0)
panel.render(image, QPoint(0, 0))
lit = sum(1 for x in range(0, image.width(), 5) for y in range(0, image.height(), 5) if image.pixelColor(x, y).alpha() > 0)
check(lit > 2500, f"and is drawn ({lit} lit samples)")

room = next(room for room in panel._view["rooms"] if room["name"] == "kitchen")
middle = panel._projection.point(room["x"] + room["w"] / 2, room["y"] + room["h"] / 2, room["floor"])
check(panel.room_at(middle) == "kitchen", f"a point on the plan finds its room ({panel.room_at(middle)})")
check(panel.room_at(panel._row_rects["my room"].center()) == "my room", "and a row in the list does")

tapped = []
panel.room_clicked.connect(tapped.append)


def mouse(kind, point):
    button = Qt.MouseButton.LeftButton
    return QMouseEvent(kind, point, panel.mapToGlobal(point), button,
                       Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button,
                       Qt.KeyboardModifier.NoModifier)


panel.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, middle))
panel.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, middle))
check(tapped == ["kitchen"], f"a tap names the room under it ({tapped})")

panel._view = dict(panel._view, floor_names={})
check(panel.floor_label(0) == "GROUND" and panel.floor_label(1, long=True) == "FLOOR 1",
      "floors are GROUND and FLOOR 1 unless the house names them")
panel._view = dict(panel._view, floor_names={0: "1ST FLOOR"})
check(panel.floor_label(0) == "1ST FLOOR", "and the house's own name when it does")
check(house_panel.hints({"rooms": [{"name": "room 4", "boards": [{"name": "x"}]}, {"name": "kitchen", "boards": []}]})[0]
      == "SAY: WHAT'S HAPPENING IN ROOM 4 - SHOW ME THE KITCHEN - CLOSE THE HOUSE", "'room 4' is said without 'the'")
panel._view = dict(panel._view, floor_names={})
hint = house_panel.hints(panel._view)[0]
check(hint == "SAY: WHAT'S HAPPENING IN MY ROOM - SHOW ME THE LANDING - CLOSE THE HOUSE",
      f"the SAY: line uses this house's rooms ({hint!r})")

composed = []
real_compose = panel._compose
panel._compose = lambda now: (composed.append(now), real_compose(now))
panel.render(image, QPoint(0, 0))
before = len(composed)
panel._composed_at = time.monotonic()

for _ in range(5):
    panel._tick()
    panel.render(image, QPoint(0, 0))

check(len(composed) == before, f"settled frames reuse the drawn plan ({len(composed) - before} redraws in 5 frames)")
panel._compose = real_compose

started = time.perf_counter()

for _ in range(20):
    panel.render(image, QPoint(0, 0))

each = (time.perf_counter() - started) / 20 * 1000
check(each < 12, f"a settled frame is cheap ({each:.1f} ms)")

house.close()
app.processEvents()
check(not panel.isVisible(), "and goes when the house is closed")
house.set_listeners(on_view=shown.append, on_hide=lambda: hidden.append(True))

# ---- through commands -----------------------------------------------------------------------------------

try:
    import commands
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        result = commands.handle_command("show me the house")
        check(result["intent"] == "house" and result["action"]().startswith("Your house, sir:"),
              "commands: 'show me the house' is the hologram, with no model call")
        result = commands.handle_command("what's happening in my room")
        check(result["intent"] == "house" and result["action"]().startswith("My room: 21.4 degrees"),
              "and 'what's happening in my room' reads the room")
        result = commands.handle_command("what's the temperature in the room")
        said = result["action"]()
        check(result["intent"] == "sensor_question" and "21.4" in said and shown[-1]["focus"] == "my room",
              f"a sensor question is still the sensors', and lights its room ({said!r})")
        result = commands.handle_command("closed the house")
        check(result["intent"] == "house" and result["action"]() == "House closed, sir.", "and closes")
    finally:
        commands.run_agent = real_agent

sensors.remove_listener(house.refresh)
sys.exit(1 if failures else 0)
