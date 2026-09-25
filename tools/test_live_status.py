"""The HUD's live status, bottom left: OBS's REC light and the like.

Drawn off-screen and read back. Checked:

- nothing is drawn while nothing is live;
- recording, a red light and the time are shown, and the clock runs on
  smoothly between reports rather than jumping every couple of seconds;
- paused says PAUSED; stopped clears it;
- a flash word (CLIP SAVED) shows, then goes;
- it keeps clear of the countdown written on the same line;
- with little room -- the countdown showing, or display scaling at 150% --
  a light shortens ("REC 01:23", then "REC", then the light alone) rather
  than disappearing.

    python tools/test_live_status.py
"""

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtGui import QFont, QFontMetrics, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import hud  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


app = QApplication.instance() or QApplication([])
widget = hud.Hud()

AREA = (20, hud._LIVE_Y - 11, hud._PANEL_X - 10, hud._LIVE_Y + 3)


def frame():
    app.processEvents()
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image, QPoint(0, 0))
    return image


def ink(image, area=AREA):
    """The average colour of the area's brightest pixels, by hue."""
    left, top, right, bottom = area
    lit = [image.pixelColor(x, y) for x in range(left, right) for y in range(top, bottom)]
    lit = [c for c in lit if max(c.red(), c.green(), c.blue()) > 60]
    lit.sort(key=lambda c: max(c.red(), c.green(), c.blue()), reverse=True)
    top_share = lit[:max(1, len(lit) // 5)] if lit else []
    if not top_share:
        return None
    return tuple(sum(getattr(c, part)() for c in top_share) / len(top_share) for part in ("red", "green", "blue"))


def is_red(rgb):
    return rgb is not None and rgb[0] > 120 and rgb[0] > 1.8 * rgb[1] and rgb[0] > 1.8 * rgb[2]


def is_green(rgb):
    return rgb is not None and rgb[1] > 120 and rgb[1] > 1.25 * rgb[0]


check(ink(frame()) is None or not is_red(ink(frame())), "nothing live, nothing drawn")

widget.live_status.emit("obs:REC", "REC", True, False, 83.0, "red")
check(is_red(ink(frame())), "recording: a red light and its time")

item = widget._live["obs:REC"]
now = time.monotonic()
check(widget._live_text(item, now) == "REC 01:23", f"the time is shown as minutes and seconds ({widget._live_text(item, now)!r})")
check(widget._live_text(item, now + 5) == "REC 01:28", "and runs on between reports, rather than jumping")

widget.live_status.emit("obs:REC", "REC", True, False, 3725.0, "red")
check(widget._live_text(widget._live["obs:REC"], time.monotonic()) == "REC 1:02:05", "past an hour, hours are shown")

widget.live_status.emit("obs:REC", "REC", True, True, 120.0, "red")
paused = widget._live["obs:REC"]
check(widget._live_text(paused, time.monotonic() + 30) == "REC PAUSED", "paused says so, and the clock stops")

widget.live_status.emit("obs:REC", "REC", False, False, -1.0, "red")
check(ink(frame()) is None or not is_red(ink(frame())), "stopped, the light goes out")

widget.live_flash.emit("CLIP SAVED")
check(is_green(ink(frame())), "a flash word shows in green")

# The usual case: clipping while recording with the replay buffer on. The
# flash must show, even though three do not fit on the line.
widget.live_status.emit("obs:REC", "REC", True, False, 83.0, "red")
widget.live_status.emit("obs:REPLAY", "REPLAY", True, False, -1.0, "amber")
widget.live_flash.emit("CLIP SAVED")
image = frame()


def first_x(image, test):
    """The leftmost column on the live line holding a pixel that passes [test]."""
    for x in range(20, hud._PANEL_X - 10):
        for y in range(hud._LIVE_Y - 11, hud._LIVE_Y + 3):
            if test(image.pixelColor(x, y)):
                return x
    return None


red_x = first_x(image, lambda c: c.red() > 150 and c.red() > 1.8 * c.green() and c.red() > 1.8 * c.blue())
green_x = first_x(image, lambda c: c.green() > 150 and c.green() > 1.25 * c.red() and c.green() > 1.1 * c.blue())
check(red_x is not None and green_x is not None and red_x < green_x,
      f"with REC and REPLAY both on, CLIP SAVED still shows, beside REC (red at {red_x}, green at {green_x})")
widget.live_status.emit("obs:REC", "REC", False, False, -1.0, "red")
widget.live_status.emit("obs:REPLAY", "REPLAY", False, False, -1.0, "amber")
widget._flash = ("CLIP SAVED", time.monotonic() - hud._FLASH_SECONDS - 0.1)
check(ink(frame()) is None or not is_green(ink(frame())), "then goes")

# With the countdown showing, the live light keeps to the left of it.
widget.live_status.emit("obs:REC", "REC", True, False, 83.0, "red")
widget.live_status.emit("obs:REPLAY", "REPLAY", True, False, -1.0, "amber")
widget.confirmation_changed.emit(120.0, "")
image = frame()
words, countdown_font = widget._countdown_text(widget._confirm_until - time.monotonic())
countdown_left = int(hud._PANEL_X - 14 - QFontMetrics(countdown_font).horizontalAdvance(words))
countdown_area = (countdown_left - 4, hud._LIVE_Y - 11, hud._PANEL_X - 10, hud._LIVE_Y + 3)
reds = [image.pixelColor(x, y) for x in range(countdown_area[0], countdown_area[2]) for y in range(countdown_area[1], countdown_area[3])]
check(is_red(ink(image, (20, hud._LIVE_Y - 11, countdown_left - 4, hud._LIVE_Y + 3))),
      "with a countdown on the same line, REC still shows")
check(not any(c.red() > 150 and c.red() > 1.8 * c.green() and c.red() > 1.8 * c.blue() for c in reds),
      "and stays clear of the countdown, measured as drawn")

widget.close()
sys.exit(1 if failures else 0)
