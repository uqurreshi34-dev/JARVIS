"""The start-up sequence: a real systems check, its sound, and the HUD.

Checked, with nothing played aloud and the HUD drawn off-screen:

- the check has one line per system, each saying ONLINE, READY, OFFLINE,
  or NONE for something not set up, which is never counted as down;
- a system whose probe fails says OFFLINE, never a reassuring ONLINE;
- the closing line counts what is offline;
- the sound is stereo, as long as the sequence, never clips, and follows
  BOOT_VOLUME, with silence at 0;
- a glassy ping sounds as each line appears, a lower falling pair for a
  system that is down, and ignition lands with a sub hit;
- the check is mostly quiet between its pings: nothing like a siren;
- BOOT_SEQUENCE and BOOT_SOUND switch it off;
- the HUD types the check out, shows OFFLINE in red and the summary at the
  end, then returns to its ordinary face;
- where main.py can be imported, starting the sequence returns at once, so
  the greeting is never held up.

    python tools/test_boot.py
"""

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

import boot  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- the check -------------------------------------------------------------------

items = boot.checks()
check(len(items) == 6 and all(status in (boot.ONLINE, boot.READY, boot.OFFLINE, boot.NONE) for _label, status in items),
      f"one line per system, each online, ready, offline or none: {items}")


def broken():
    raise RuntimeError("did not start")


check(boot._check("PHONE LINK", broken) == ("PHONE LINK", boot.OFFLINE), "a system that fails its probe says OFFLINE")

check(boot.summary([("A", boot.ONLINE), ("B", boot.READY)]) == "ALL SYSTEMS ONLINE", "all up: ALL SYSTEMS ONLINE")
check(boot.summary([("A", boot.OFFLINE), ("B", boot.ONLINE)]) == "1 SYSTEM OFFLINE", "one down is counted")
check(boot.summary([("A", boot.OFFLINE), ("B", boot.OFFLINE)]) == "2 SYSTEMS OFFLINE", "and several")
check(boot.summary([("SERVICES (0)", boot.NONE), ("B", boot.ONLINE)]) == "ALL SYSTEMS ONLINE",
      "nothing set up is not counted as down")

# ---- the sound ---------------------------------------------------------------------

sample = [("VOICE", boot.ONLINE), ("SPEECH", boot.ONLINE), ("MODELS", boot.ONLINE),
          ("MEMORY", boot.ONLINE), ("SERVICES", boot.READY), ("PHONE LINK", boot.OFFLINE)]

started = time.perf_counter()
audio = boot.sound(sample)
took = time.perf_counter() - started

check(audio.ndim == 2 and audio.shape[1] == 2, "the sound is stereo")
check(abs(len(audio) / boot.SAMPLE_RATE - boot.DURATION) < 0.01, f"and lasts the sequence ({len(audio) / boot.SAMPLE_RATE:.2f}s)")
check(np.isfinite(audio).all() and float(np.max(np.abs(audio))) <= 0.7 * 0.45 + 1e-6, "it never clips, at the default volume")
check(float(np.max(np.abs(audio[-50:]))) < 0.01, "and ends in silence, with no click")
print(f"     (made in {took * 1000:.0f} ms)")


def band_energy(signal, at, low, high, span=0.05):
    start = int(at * boot.SAMPLE_RATE)
    window = signal[start:start + int(span * boot.SAMPLE_RATE)]
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    freqs = np.fft.rfftfreq(len(window), 1 / boot.SAMPLE_RATE)
    return float(np.sum(spectrum[(freqs >= low) & (freqs < high)]))


mono = audio[:, 0]

third = boot.line_time(2) + 0.18
check(band_energy(mono, third, 1500, 4000, span=0.1) > 3 * band_energy(mono, third - 0.1, 1500, 4000, span=0.1),
      "a glassy ping sounds as each system reports")

offline = boot.line_time(5) + 0.18
online = boot.line_time(4) + 0.18
check(band_energy(mono, offline, 380, 720, span=0.18) > 3 * band_energy(mono, online, 380, 720, span=0.18),
      "an offline system gets a lower, falling pair instead")


def quiet_share(signal, start, end, span=0.01):
    """How much of a stretch is near-silent, in 10 ms slices."""
    levels = np.array([
        float(np.sqrt(np.mean(signal[int(at * boot.SAMPLE_RATE):int((at + span) * boot.SAMPLE_RATE)] ** 2)))
        for at in np.arange(start, end, span)
    ])
    return float(np.mean(levels < 0.15 * levels.max()))


# An earlier rising whine read as a siren: a tone that never stops. Digital
# pings are mostly silence between pips; the whine was never quiet at all.
quiet = quiet_share(mono, boot.FIRST_LINE + 0.1, boot.IGNITION - 0.6)
check(quiet > 0.3, f"no siren: the check is mostly quiet between its pings ({quiet:.0%} of the time)")

check(band_energy(mono, boot.IGNITION, 30, 120, span=0.05) > 5 * band_energy(mono, boot.IGNITION - 0.12, 30, 120, span=0.05),
      "ignition lands with a sub hit")

os.environ["BOOT_VOLUME"] = "0"
check(float(np.max(np.abs(boot.sound(sample)))) == 0.0, "BOOT_VOLUME=0 is silent")
os.environ["BOOT_VOLUME"] = "nonsense"
check(float(np.max(np.abs(boot.sound(sample)))) > 0.0, "a volume that is not a number falls back to the default")
del os.environ["BOOT_VOLUME"]

for name, switch in (("BOOT_SEQUENCE", boot.enabled), ("BOOT_SOUND", boot.sound_enabled)):
    check(switch(), f"{name} is on by default")
    os.environ[name] = "off"
    check(not switch(), f"{name}=off switches it off")
    del os.environ[name]

# ---- the HUD -----------------------------------------------------------------------

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import hud  # noqa: E402

app = QApplication.instance() or QApplication([])
widget = hud.Hud()


def frame_at(seconds):
    widget._boot["started"] = time.monotonic() - seconds
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image, QPoint(0, 0))
    return image


def count(image, test, area):
    left, top, right, bottom = area
    return sum(1 for x in range(left, right) for y in range(top, bottom) if test(image.pixelColor(x, y)))


def is_red(colour):
    return colour.red() > 200 and colour.green() < 130 and colour.blue() < 130


def is_green(colour):
    return colour.green() > 200 and colour.red() < 170 and colour.blue() < 200


def is_text(colour):
    return colour.alpha() > 200 and max(colour.red(), colour.green(), colour.blue()) > 120


widget.boot_requested.emit(sample)
app.processEvents()

column = (hud._PANEL_X, 70, hud._PANEL_RIGHT, 70 + 6 * 17)
early = count(frame_at(boot.line_time(0) + 0.05), is_text, column)
later = count(frame_at(boot.line_time(4) + 0.3), is_text, column)
check(later > early * 2, "the check types itself out down the right-hand side")

check(count(frame_at(boot.line_time(5) + 0.6), is_red, (hud._PANEL_X, 82 + 5 * 17 - 12, hud._PANEL_RIGHT, 82 + 5 * 17 + 3)) > 5,
      "an offline system is shown in red")

end = frame_at(boot.DURATION - 0.2)
check(count(end, lambda c: c.red() > 200 and 150 < c.green() < 225 and c.blue() < 140,
            (hud._PANEL_X, 30, hud._PANEL_RIGHT, 52)) > 10,
      "one down, the summary says so in amber")

widget.boot_requested.emit([(label, boot.ONLINE) for label, _ in sample])
check(count(frame_at(boot.DURATION - 0.2), is_green, (hud._PANEL_X, 30, hud._PANEL_RIGHT, 52)) > 10,
      "all up, it says ALL SYSTEMS ONLINE in green")

frame_at(boot.DURATION + 0.1)
check(widget._boot is None, "then the HUD returns to its ordinary face")

widget.close()

# ---- never holding up the greeting -----------------------------------------------------

try:
    import main
except Exception as error:  # needs JARVIS's full Windows environment
    print(f"SKIP start-up timing (could not import main: {error})")
else:
    from unittest.mock import MagicMock

    shown = []
    fake = MagicMock()
    fake._hud.boot_requested.emit = shown.append

    real_sound, real_play = boot.sound, boot.play
    boot.sound = lambda items: (time.sleep(0.5), np.zeros((10, 2), dtype=np.float32))[1]
    boot.play = lambda audio: None

    try:
        started = time.perf_counter()
        main.Assistant._start_boot(fake)
        waited = time.perf_counter() - started
    finally:
        boot.sound, boot.play = real_sound, real_play

    check(shown and waited < 0.2, f"starting the sequence returns at once, so the greeting follows ({waited:.2f}s)")

sys.exit(1 if failures else 0)
