"""The sensor panel docked on top of the HUD, and the simulator that feeds it.

Drawn off-screen, on a clock the test turns by hand. Checked:

- nothing shows until a board reports; then it slides up out of the HUD
  (part way after a frame, not all at once) and sits on top of it, the
  same width, touching it, and follows it when the HUD is dragged;
- a HUD dragged to the top of the screen has the panel hang below;
- a click folds it to its title strip, another opens it;
- a row per board, most recent first, three at most with the rest counted;
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

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtGui import QFont, QFontMetrics, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import hud  # noqa: E402
import sensor_panel  # noqa: E402
from actions import sensors  # noqa: E402


failures = 0


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

# Folding.
panel._folded = False
panel.mouseReleaseEvent(type("E", (), {"button": lambda self: hud.Qt.MouseButton.LeftButton})())
settle()
check(panel._folded and panel._reveal == sensor_panel._HEADER, "a click folds it to its title strip")
check("1 ONLINE" in panel.header_text(), "which says how many boards are online")
panel.mouseReleaseEvent(type("E", (), {"button": lambda self: hud.Qt.MouseButton.LeftButton})())
settle()
check(not panel._folded and panel._reveal == panel.height(), "another click opens it")

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
check("4 ONLINE" in panel.header_text() and "+1" in panel.header_text(), f"the rest are counted ({panel.header_text()!r})")
check([name for name, _ in panel._ordered()][0] == "garage", "the most recently heard first")

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
