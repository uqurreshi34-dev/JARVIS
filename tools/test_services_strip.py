"""The HUD's connected-services strip, drawn off-screen and read back.

Checked:

- nothing is drawn until services are listed;
- listed but not yet connected, each name is shown dim;
- connected, it is lit; busy, it pulses brighter;
- waiting on a yes it turns amber, and answering sets it back;
- done flashes green and refused flashes red, each settling to connected;
- unavailable stays red;
- more services than fit are counted, not drawn over the corner bracket.

    python tools/test_services_strip.py
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

STRIP = (hud._PANEL_X, hud._SERVICES_Y - 10, hud._PANEL_RIGHT, hud._SERVICES_Y + 3)


def frame():
    app.processEvents()
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image, QPoint(0, 0))
    return image


def pixels(image, test, area=STRIP):
    left, top, right, bottom = area
    return sum(1 for x in range(left, right) for y in range(top, bottom) if test(image.pixelColor(x, y)))


def bright(colour):
    return max(colour.red(), colour.green(), colour.blue()) > 150


def strongest(image, area=STRIP, share=0.2):
    """The strip's brightest pixels: the ink itself, not its anti-aliased edge.

    Judging the ink's colour, rather than counting pixels over a threshold,
    holds on any font: Windows draws small Consolas thin, and a count that
    passes with a heavier font can fail there.
    """
    left, top, right, bottom = area
    lit = [image.pixelColor(x, y) for x in range(left, right) for y in range(top, bottom)]
    lit = [c for c in lit if c.alpha() > 0 and max(c.red(), c.green(), c.blue()) > 40]
    lit.sort(key=lambda c: max(c.red(), c.green(), c.blue()), reverse=True)
    return lit[:max(1, int(len(lit) * share))] if lit else []


def peak(image):
    """How bright the strip's ink is at its brightest."""
    top = strongest(image)
    return sum(max(c.red(), c.green(), c.blue()) for c in top) / len(top) if top else 0.0


def hue(image, area=STRIP):
    """The average colour of the strip's brightest ink, as (r, g, b)."""
    top = strongest(image, area)
    if not top:
        return (0.0, 0.0, 0.0)
    return tuple(sum(getattr(c, part)() for c in top) / len(top) for part in ("red", "green", "blue"))


def is_amber(rgb):
    r, g, b = rgb
    return r > 100 and 0.6 * r < g < 0.92 * r and b < 0.62 * r


def is_green(rgb):
    r, g, b = rgb
    return g > 100 and g > 1.25 * r and g > 1.1 * b


def is_red(rgb):
    r, g, b = rgb
    return r > 100 and r > 1.8 * g and r > 1.8 * b


empty = peak(frame())

widget.services_listed.emit(["github"])
configured = peak(frame())
check(configured > empty, "a listed service is drawn")

widget.service_activity.emit("github", "connected")
connected = peak(frame())
check(connected > configured + 30, f"connected, it is lit brighter than when only listed ({configured:.0f} -> {connected:.0f})")

widget.service_activity.emit("github", "busy")
check(peak(frame()) >= connected - 5, "busy is at least as bright as connected")

widget.service_activity.emit("github", "held")
check(is_amber(hue(frame())), f"waiting on a yes, the service turns amber {hue(frame())}")

widget.confirmation_changed.emit(120.0, "")
widget.confirmation_changed.emit(0.0, "no")
check(not is_amber(hue(frame())) and widget._services["github"][0] == "connected",
      "answering the question sets it back to connected")

widget.service_activity.emit("github", "done")
check(is_green(hue(frame())), f"an action that went through flashes green {hue(frame())}")

widget._services["github"][1] = time.monotonic() - hud._SERVICE_FLASH["done"] - 0.1
frame()
check(widget._services["github"][0] == "connected", "then settles to connected")

widget.service_activity.emit("github", "refused")
refused = hue(frame())
check(is_red(refused), f"a refusal flashes red {refused}")
widget._services["github"][1] = time.monotonic() - hud._SERVICE_FLASH["refused"] - 0.1
frame()
check(widget._services["github"][0] == "connected", "and settles too")

widget.service_activity.emit("github", "unavailable")
widget._services["github"][1] = time.monotonic() - 60
unavailable = hue(frame())
check(is_red(unavailable) and unavailable[0] < refused[0],
      f"a service that could not connect stays red, dimmer than a refusal {unavailable}")

widget.services_listed.emit(["filesystem", "obs", "home_assistant", "notion", "spotify", "calendar"])
image = frame()
corner = (hud._PANEL_RIGHT - 24, hud._SERVICES_Y - 10, hud._WIDTH - 5, hud._SERVICES_Y + 3)
check(sum(1 for x in range(corner[0], corner[2]) for y in range(corner[1], corner[3]) if bright(image.pixelColor(x, y))) == 0,
      "more services than fit never run into the corner bracket")

widget.close()
sys.exit(1 if failures else 0)
