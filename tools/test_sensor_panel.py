"""The sensor panel docked on top of the HUD, and the simulator that feeds it.

Drawn off-screen, on a clock the test turns by hand. Checked:

- nothing shows until a board reports; then it slides up out of the HUD
  (part way after a frame, not all at once) and sits on top of it, the
  same width, touching it, and follows it when the HUD is dragged;
- a HUD dragged to the top of the screen has the panel hang below;
- a click on its title strip folds it and another opens it; a click among
  the rows never does;
- a row per board in a steady order, three showing, the wheel scrolling
  through the rest, and so do dragging the rows and the scroll bar's
  arrows (a laptop has no wheel);
- "Welcome back" is said once for the house, not for every room walked into;
- readings go on the trend line, motion reports do not;
- a board that misses readings dims, one long silent is offline, and with
  every board silent it slides away; a board coming back opens it again,
  unfolded, and it takes the HUD's colour;
- words shorten to fit rather than being cut mid-word;
- sensors.py tells the panel of each report, and a panel failing never
  stops an announcement;
- the simulator, and the real sketch, send what sensors.py reads.

    python tools/test_sensor_panel.py
"""

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, qInstallMessageHandler  # noqa: E402
from PyQt6.QtGui import QFont, QFontMetrics, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import hud  # noqa: E402
import sensor_panel  # noqa: E402
from actions import sensors  # noqa: E402


failures = 0


def quiet(mode, context, message):
    # Drawn off-screen, Qt notes each window call it has no screen for
    # ("This plugin does not support raise()"). Harmless, and not ours.
    if "This plugin does not support" not in message:
        print(message)


qInstallMessageHandler(quiet)


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


clock = [1000.0]
sensor_panel._now = lambda: clock[0]
sensors._now = lambda: clock[0]


def later(seconds):
    clock[0] += seconds


app = QApplication.instance() or QApplication([])
widget = hud.Hud()
widget.show()
screen = widget.screen().availableGeometry()
widget.move(screen.left() + 300, screen.bottom() - hud._HEIGHT - 24)

panel = sensor_panel.SensorPanel()
panel.set_anchor(widget)
sensors.reset()
sensors.set_listener(panel.updated.emit)


def settle(frames=120):
    for _ in range(frames):
        app.processEvents()
        panel._tick()


def lit(image):
    return sum(1 for x in range(0, image.width(), 4) for y in range(0, image.height(), 4)
               if image.pixelColor(x, y).alpha() > 0)


def frame():
    image = QImage(panel.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    panel.render(image, QPoint(0, 0))
    return image


app.processEvents()
check(not panel.isVisible(), "nothing shows before a board reports")

said = sensors.report({"name": "room", "event": "online"})
app.processEvents()
check(said == "Room sensor online, sir.", "the announcement is made as before")
check(panel.isVisible(), "a board reporting brings the panel up")

panel._tick()
check(0 < panel._reveal < panel.height(), f"it slides up rather than appearing at once ({panel._reveal:.0f} of {panel.height()})")

settle()
check(panel._reveal == panel.height(), "and comes all the way out")

hud_frame, own = widget.frameGeometry(), panel.frameGeometry()
check(own.width() == hud_frame.width() and own.left() == hud_frame.left(), "the same width as the HUD, and in line with it")
touching = hud_frame.top() - (own.bottom() + 1)
check(-5 <= touching <= 0, f"sitting on top of the HUD, touching it ({touching} px)")

widget.move(widget.x() - 120, widget.y() - 40)
app.processEvents()
check(panel.frameGeometry().left() == widget.frameGeometry().left()
      and panel.frameGeometry().bottom() < widget.frameGeometry().top() + 5, "it follows the HUD when dragged")

widget.move(widget.x(), screen.top() + 10)
app.processEvents()
check(panel._below and panel.frameGeometry().top() >= widget.frameGeometry().bottom() - 5,
      "a HUD at the top of the screen has it hang below instead")
widget.move(screen.left() + 300, screen.bottom() - hud._HEIGHT - 24)
app.processEvents()
check(not panel._below, "and back on top when there is room")

# Readings and the trend line.
for step in range(5):
    later(30)
    sensors.report({"name": "room", "temperature": 21.0 + step * 0.2, "humidity": 48})

sensors.report({"name": "room", "event": "motion"})
board = panel._boards["room"]
check(list(board["history"]) == [21.0, 21.2, 21.4, 21.6, 21.8],
      f"readings go on the trend line, a motion report does not ({list(board['history'])})")
check(panel.presence(board) == "PRESENT", "movement shows as someone present")

settle()
check(lit(frame()) > 200, "the panel is drawn")

# Clicks and drags, as a touchpad or mouse makes them.
from PyQt6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402


def mouse(kind, x, y):
    point = QPointF(x, y)
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    return QMouseEvent(kind, point, panel.mapToGlobal(point), button, held, Qt.KeyboardModifier.NoModifier)


def click(x, y):
    panel.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, x, y))
    panel.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, x, y))
    settle()


def drag(x, from_y, to_y):
    panel.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, x, from_y))
    for step in range(1, 11):
        panel.mouseMoveEvent(mouse(QEvent.Type.MouseMove, x, from_y + (to_y - from_y) * step / 10))
    panel.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, x, to_y))
    settle()


panel._folded = False
click(200, sensor_panel._HEADER + 40)
check(not panel._folded, "a click among the rows does not fold it")
click(200, sensor_panel._HEADER / 2)
check(panel._folded and panel._reveal == sensor_panel._HEADER, "a click on the title strip folds it")
check("1 ONLINE" in panel.header_text(), "which says how many boards are online")
strip = panel.header_rect()
check(strip.bottom() >= panel.height() - 1, "folded, the strip is where it shows, against the HUD")
click(200, strip.center().y())
check(not panel._folded and panel._reveal == panel.height(), "a click on the strip opens it again")

# Rows.
one_row = panel.height()
check(one_row == sensor_panel._HEADER + sensor_panel._ROW + sensor_panel._FOOT, "one board, one row")

for name in ("kitchen", "hall", "garage"):
    later(1)
    sensors.report({"name": name, "temperature": 19.5, "humidity": 55})

settle()
check(panel.height() == sensor_panel._HEADER + 3 * sensor_panel._ROW + sensor_panel._FOOT,
      "three rows at most, the HUD's own height")
check(panel.height() <= hud._HEIGHT, "no taller than the HUD")
check("4 ONLINE" in panel.header_text(), f"all are counted ({panel.header_text()!r})")
check([name for name, _ in panel._listed()] == ["garage", "hall", "kitchen", "room"],
      "in a steady order, by name, so a scrolled list does not reshuffle")


class Wheel:
    def __init__(self, notches):
        self._y = int(notches * 120)
        self.accepted = None

    def angleDelta(self):
        return QPoint(0, self._y)

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


panel.wheelEvent(Wheel(-1))
settle()
check(panel._scroll == 1 and panel._scroll_px == sensor_panel._ROW, "the wheel scrolls down a row")
panel.wheelEvent(Wheel(-5))
settle()
check(panel._scroll == panel._max_scroll() == 1, "no further than the last row")
panel.wheelEvent(Wheel(3))
settle()
check(panel._scroll == 0, "and back up to the first")

for index in range(6):
    later(1)
    sensors.report({"name": f"board {index}", "temperature": 20.0, "humidity": 50})

settle()
check(panel.height() == sensor_panel._HEADER + 3 * sensor_panel._ROW + sensor_panel._FOOT,
      "ten boards still take three rows' room")
panel.wheelEvent(Wheel(-20))
settle()
check(panel._scroll == 7, "and scroll through the other seven")
drag(200, 200, 200 + 2 * sensor_panel._ROW)
check(panel._scroll == 5 and not panel._folded, "dragging the rows down scrolls up, two rows, without folding")
drag(200, 200, 200 - 1.4 * sensor_panel._ROW)
check(panel._scroll == 6, "dragging up scrolls down, settling on a whole row")
arrow_x = panel.width() - 22
click(arrow_x, sensor_panel._HEADER + 10)
check(panel._scroll == 5, "the arrow at the top of the scroll bar goes up a row")
click(arrow_x, panel.height() - sensor_panel._FOOT - 10)
check(panel._scroll == 6, "the one at the bottom goes down a row")
panel.wheelEvent(Wheel(-20))
settle()
image = frame()
bar_x = panel.width() - 22
bar = [image.pixelColor(bar_x, y) for y in range(sensor_panel._HEADER, panel.height() - sensor_panel._FOOT)]
check(any(c.alpha() > 150 and c.blue() > 120 for c in bar), "with a scroll bar showing where")
panel.wheelEvent(Wheel(20))
settle()
for index in range(6):
    panel._boards.pop(f"board {index}")
sensors.reset()
for name in ("room", "kitchen", "hall", "garage"):
    sensors.report({"name": name, "temperature": 19.5, "humidity": 55})
settle()

# Going quiet.
later(80)
check(panel.row_state(panel._boards["garage"]) == "QUIET", "a board that has missed readings is shown as quiet")
later(60)
check(panel.row_state(panel._boards["garage"]) == "OFFLINE", "one silent for longer is offline")

panel._folded = True
settle()
check(not panel.isVisible() and panel._reveal == 0, "with every board silent, it slides away")

widget.state_changed.emit(hud.THINKING)
app.processEvents()
check(panel._accent == hud._PALETTE[hud.THINKING], "it takes the HUD's colour")

later(5)
sensors.report({"name": "room", "temperature": 22.0, "humidity": 47})
app.processEvents()
check(panel.isVisible() and not panel._folded, "a board coming back opens it again, unfolded")

# Words shorten to fit.
metrics = QFontMetrics(QFont("Consolas", 7, QFont.Weight.Bold))
options = ["SEEN 12s AGO", "SEEN 12s", "12s"]
check(sensor_panel.fitting(options, metrics, 1000) == "SEEN 12s AGO", "with room, the full words")
narrow = metrics.horizontalAdvance("SEEN 12s") + 1
check(sensor_panel.fitting(options, metrics, narrow) == "SEEN 12s", "short of room, the shorter form, not a cut word")
check(sensor_panel.ago(12) == "12s" and sensor_panel.ago(125) == "2m" and sensor_panel.ago(7300) == "2h", "ages read short")

# "Welcome back" is for coming back, not for walking between rooms.
sensors.reset()
for name in ("hall", "kitchen"):
    sensors.report({"name": name, "event": "online"})
later(5)
check(sensors.report({"name": "hall", "event": "motion"}) == "Welcome back, sir.", "movement in an empty house greets you")
later(5)
check(sensors.report({"name": "kitchen", "event": "motion"}) is None, "walking on into another room does not, again")
later(sensors.PRESENT_SECONDS + 5)
check(sensors.report({"name": "kitchen", "event": "motion"}) is None,
      "nor does coming back soon after, however long the house was still")
later(sensors.GREET_AGAIN_SECONDS)
check(sensors.report({"name": "kitchen", "event": "motion"}) == "Welcome back, sir.",
      "a long while later, it greets you again")

main_source = (ROOT / "main.py").read_text(encoding="utf-8")
check("sensors.set_listener(None)" in main_source and "phone_server.set_sensor_handler(None)" in main_source,
      "shutting down, boards still reporting are heard and ignored")
sensors.report({"name": "room", "temperature": 21.0, "humidity": 50})
sensors.report({"name": "room", "event": "motion"})

# sensors.py and its listener.
known = sensors.known()["room"]
check("moved_ago" in known and "read_ago" in known, "known() says when a board last moved and last read")


def broken(_known):
    raise RuntimeError("display failed")


sensors.set_listener(broken)
sensors.reset()
check(sensors.report({"name": "den", "event": "online"}) == "Den sensor online, sir.",
      "a panel failing never stops the announcement")
sensors.set_listener(panel.updated.emit)

# What the simulator and the sketch send, sensors.py reads.
from tools import sensor_simulator  # noqa: E402

pretend = sensor_simulator.Board("test", 0, 25.0)
reading = pretend.readings()
check(set(reading) == {"name", "temperature", "humidity"}, "the simulator sends a board's readings")
sensors.reset()
sensors.report({"name": "test", "event": "online"})
sensors.report(reading)
check(sensors.known()["test"]["readings"].get("temperature") == reading["temperature"], "which sensors.py reads")

sketch = (ROOT / "arduino" / "jarvis_sensor" / "jarvis_sensor.ino").read_text(encoding="utf-8")
check(all(word in sketch for word in ('\\"temperature\\"', '\\"humidity\\"', '\\"event\\"', '"online"', '"motion"')),
      "and so does the real sketch")

minutes = sensor_panel._GONE_SECONDS / 60
said = (ROOT / "tools" / "sensor_simulator.py").read_text(encoding="utf-8")
check(minutes == 2 and "quiet for two minutes" in said, "the simulator tells the truth about when the panel goes")
check(re.search(r"ProxyHandler\(\{\}\)", said) is not None, "it goes straight to JARVIS, never through a proxy")

panel.hide_requested.emit()
app.processEvents()
check(not panel.isVisible(), "shutting down hides it")

widget.close()
sys.exit(1 if failures else 0)
