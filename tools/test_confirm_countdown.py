"""The HUD's countdown ring for a held question ("Shall I go ahead?").

Drawn off-screen and read back pixel by pixel. Checked:

- nothing is drawn on the outer ring until a question is held;
- asked, an amber ring runs from the top, with the time left written out;
- it drains: after most of the time has gone, most of the ring has too;
- the last stretch turns red;
- yes lights the whole ring green and fades; no leaves grey that fades;
- running out with nothing said ends it on its own.

    python tools/test_confirm_countdown.py
"""

import math
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import hud  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


app = QApplication.instance() or QApplication([])
widget = hud.Hud()


def frame():
    app.processEvents()
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image, QPoint(0, 0))
    return image


def on_ring(image, degrees):
    """The colour on the outer ring at a clock angle: 0 is the top, 90 the right."""
    angle = math.radians(degrees)
    x = hud._CENTRE.x() + math.sin(angle) * hud._R_OUTER
    y = hud._CENTRE.y() - math.cos(angle) * hud._R_OUTER
    return image.pixelColor(int(round(x)), int(round(y)))


def amber(colour):
    return colour.red() > 200 and 150 < colour.green() < 225 and colour.blue() < 140


def red(colour):
    # By hue, not brightness: the last stretch pulses, and its dim point is
    # still red. A brightness threshold failed about one run in six.
    r, g, b = colour.red(), colour.green(), colour.blue()
    return r > 120 and r > 1.8 * g and r > 1.8 * b


def green(colour):
    return colour.green() > 200 and colour.red() < 170


def grey(colour):
    return abs(colour.red() - colour.blue()) < 45 and 90 < colour.red() < 200 and colour.green() < 200


def countdown_text(image):
    """True if the countdown's colour appears where its text is written."""
    for x in range(120, hud._PANEL_X - 10):
        for y in range(hud._HEIGHT - 32, hud._HEIGHT - 18):
            colour = image.pixelColor(x, y)
            if amber(colour) or red(colour):
                return True
    return False


# ---- nothing held ------------------------------------------------------------

image = frame()
check(not any(amber(on_ring(image, a)) for a in range(0, 360, 15)), "no ring is drawn until a question is held")
check(not countdown_text(image), "and no countdown is written")

# ---- asked -------------------------------------------------------------------

widget.confirmation_changed.emit(120.0, "")
image = frame()
check(sum(amber(on_ring(image, a)) for a in range(0, 360, 15)) >= 22, "asked: an amber ring runs all the way round")
check(countdown_text(image), "and the time left is written under it")

# ---- draining ------------------------------------------------------------------

widget._confirm_until = time.monotonic() + 30  # a quarter of the time left
image = frame()
lit = [a for a in range(5, 360, 10) if amber(on_ring(image, a))]
check(lit and max(lit) <= 360 and min(lit) >= 265, f"with a quarter left, only the last quarter before the top is lit ({lit})")

widget._confirm_until = time.monotonic() + 9
image = frame()
check(any(red(on_ring(image, a)) for a in range(300, 360, 5)), "the last stretch turns red")

# ---- how it ends ------------------------------------------------------------------

widget.confirmation_changed.emit(0.0, "yes")
image = frame()
check(sum(green(on_ring(image, a)) for a in range(0, 360, 15)) >= 22, "yes lights the whole ring green")

widget._confirm_ended = ("yes", time.monotonic() - hud._CONFIRM_FLASH_SECONDS - 0.1, 0.0)
image = frame()
check(not any(green(on_ring(image, a)) for a in range(0, 360, 15)), "then fades away")

widget.confirmation_changed.emit(120.0, "")
widget._confirm_until = time.monotonic() + 60
widget.confirmation_changed.emit(0.0, "no")
image = frame()
check(any(grey(on_ring(image, a)) for a in range(185, 355, 10)), "no leaves what was left in grey")
check(not countdown_text(image), "and the countdown text goes")

widget.confirmation_changed.emit(120.0, "")
widget._confirm_until = time.monotonic() - 0.1
frame()
check(widget._confirm_until is None and widget._confirm_ended and widget._confirm_ended[0] == "lapsed",
      "running out with nothing said ends it on its own")

widget.close()
sys.exit(1 if failures else 0)
