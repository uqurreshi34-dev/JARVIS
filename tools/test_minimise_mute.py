"""The HUD's minimise and mute buttons.

Checked for real, with no microphone opened and no window on screen:

- voice.py: muted, nothing the PC microphone hears is acted on, a follow-up
  window cannot open, and half a sentence from before the mute is thrown away;
  unmuted, he listens again.
- hud.Hud, drawn off-screen and clicked: the two buttons, MUTED on the label,
  and the work-in-progress strip kept clear of them.
- hud_mini.Minimiser: minimising hides the HUD and the panels showing behind a
  small core; a panel opened or closed meanwhile is done when the core is
  clicked; the beams are left alone; the radar does not take it as closing.
- main.py's wiring, read from its source.

    python tools/test_minimise_mute.py
"""

import contextlib
import io
import os
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- voice.py: the microphone muted -------------------------------------------------------------

sys.modules["sounddevice"] = MagicMock(name="sounddevice")
sys.modules["vosk"] = types.SimpleNamespace(KaldiRecognizer=MagicMock(), Model=MagicMock())

_real_speech = sys.modules.get("speech")
sys.modules["speech"] = types.SimpleNamespace(is_speaking=lambda: False, speech_epoch=lambda: 0)

import transcriber  # noqa: E402


class Engine:
    """Hands back one transcription per audio block, in order, and counts resets."""

    def __init__(self):
        self.said, self.active, self.name, self.resets, self.fed = [], False, "test", 0, 0

    def reset(self):
        self.resets += 1

    def feed(self, data):
        self.fed += 1

        if not self.said:
            return None

        return types.SimpleNamespace(text=self.said.pop(0), confidence=None)


engine = Engine()
transcriber.build_engine = lambda model, block: engine

import voice  # noqa: E402

if _real_speech is not None:
    sys.modules["speech"] = _real_speech
else:
    del sys.modules["speech"]

voice.REQUIRE_WAKE_WORD = True
voice._make_wake_recognizer = lambda: None
voice._wake_listener = None
voice.ACKNOWLEDGE_WAKE = False

statuses = []
voice.set_status_listener(statuses.append)

check(not voice.is_muted(), "JARVIS starts listening, not muted")

voice.arm_follow_up()
voice.set_muted(True)
check(voice.is_muted() and not voice.is_armed(), "muting closes an open follow-up window")
check(statuses and statuses[-1] == "standby", "and the HUD is told he is not listening")

voice.arm_follow_up()
check(not voice.is_armed(), "muted, a reply cannot open a follow-up window")

result = {}


def run_listen():
    with contextlib.redirect_stdout(io.StringIO()):
        result["command"] = voice.listen()


engine.said = ["jarvis open chrome"]
listener = threading.Thread(target=run_listen, daemon=True)
listener.start()

for _ in range(8):
    voice.audio_queue.put(b"\x00\x00" * 8)
    time.sleep(0.05)

time.sleep(0.2)
check(listener.is_alive() and "command" not in result and engine.fed == 0,
      "muted, 'Jarvis, open Chrome' said to the PC is not even listened to")

resets_before = engine.resets
engine.said = ["jarvis what time is it"]
voice.set_muted(False)

for _ in range(10):
    if "command" in result:
        break

    voice.audio_queue.put(b"\x00\x00" * 8)
    time.sleep(0.05)

listener.join(timeout=2.0)
check(result.get("command") == "what time is it", "unmuted, he hears 'Jarvis, what time is it' again")
check(engine.resets > resets_before, "and starts on a clean slate, not mid-sentence from before the mute")

voice.arm_follow_up()
check(voice.is_armed(), "unmuted, follow-up windows open again")
voice.disarm()


# ---- hud.Hud: the buttons ----------------------------------------------------------------------

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])

import hud  # noqa: E402
import hud_mini  # noqa: E402

widget = hud.Hud()
widget._boot = None
widget.show()
app.processEvents()

clicks = []
widget.minimise_clicked.connect(lambda: clicks.append("minimise"))
widget.mute_clicked.connect(lambda: clicks.append("mute"))
widget.stop_clicked.connect(lambda: clicks.append("stop"))
widget.core_clicked.connect(lambda: clicks.append("core"))


def point(rect):
    centre = rect.center()
    return QPoint(int(centre.x()), int(centre.y()))


QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point(hud._MINIMISE_RECT))
check(clicks == ["minimise"], "clicking the bar, top right, asks to minimise")

clicks.clear()
QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point(hud._MUTE_RECT))
check(clicks == ["mute"], "clicking the microphone asks to mute")

clicks.clear()
start = point(hud._MUTE_RECT)
QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
QTest.mouseMove(widget, start + QPoint(-60, 40))
QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=start + QPoint(-60, 40))
check(not clicks, "a drag that starts on a button moves the HUD and presses nothing")

check(not hud._MINIMISE_HIT.intersects(hud._MUTE_HIT), "the two buttons' click areas do not overlap")
check(not hud._MUTE_HIT.contains(hud._STOP_HIT.center()) and not hud._MINIMISE_HIT.contains(hud._STOP_HIT.center()),
      "and are well away from the stop button")
check(hud._PROGRESS_RIGHT < hud._MUTE_HIT.left(), "the work-in-progress strip ends before the buttons begin")

widget.muted_changed.emit(True)
app.processEvents()
check(widget._muted, "the HUD knows the microphone is muted")
image = widget.grab()
check(not image.isNull(), "and draws itself muted without error")

source = (ROOT / "hud.py").read_text(encoding="utf-8")
check('label = "MUTED" if muted_label else _LABEL[self._state]' in source
      and "self._muted and self._state in (IDLE, LISTENING)" in source,
      "muted, STANDBY and LISTENING read MUTED; thinking and speaking still say so")

widget.muted_changed.emit(False)
app.processEvents()
check(not widget._muted, "and knows when it is listening again")


# ---- hud_mini.Minimiser: minimise and restore ---------------------------------------------------

def tool_window():
    window = QWidget()
    window.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
    window.resize(120, 80)
    return window


class FakeBeam(QWidget):
    pass


quran = tool_window()          # open when minimised; closes itself meanwhile
news = tool_window()           # open when minimised; still wanted afterwards
camera = tool_window()         # closed when minimised; opened meanwhile
chart = tool_window()          # closed throughout
beam = FakeBeam()
beam.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)

for window in (quran, news, beam):
    window.show()

core = hud_mini.MiniCore()
minimiser = hud_mini.Minimiser(widget, core, skip=lambda w: isinstance(w, FakeBeam))

moves = []
widget.moved.connect(lambda: moves.append(1))

widget.show()
app.processEvents()
hud_corner = widget.frameGeometry().topRight()

minimiser.minimise()
app.processEvents()

check(widget.minimised and not widget.isVisible(), "minimised, the HUD is hidden")
check(core.isVisible(), "and the small core is showing")
check(abs(core.frameGeometry().topRight().x() - hud_corner.x()) < 20
      and abs(core.frameGeometry().top() - hud_corner.y()) < 20,
      "where the HUD's minimise button was")
check(not quran.isVisible() and not news.isVisible(), "every panel that was open is hidden with it")
check(beam.isVisible(), "the beams are left to look after themselves")

minimiser.minimise()
check(core.isVisible() and not widget.isVisible(), "minimising again changes nothing")

quran.hide()
camera.show()
app.processEvents()
check(not camera.isVisible(), "a panel opened while minimised waits for the HUD")

QTest.mouseClick(core, Qt.MouseButton.LeftButton, pos=QPoint(core.width() // 2, core.height() // 2))
app.processEvents()

check(not widget.minimised and widget.isVisible() and not core.isVisible(), "clicking the core brings the HUD back")
check(news.isVisible(), "with the panel that was open")
check(camera.isVisible(), "and the panel opened meanwhile")
check(not quran.isVisible(), "but not the panel that closed itself meanwhile")
check(not chart.isVisible(), "and not one that was never open")
check(all("show" not in vars(window) and "hide" not in vars(window) for window in (quran, news, camera, chart)),
      "afterwards every panel's own show and hide work as before")
check(moves, "panels docked to the HUD are told to line up with it again")

quran.show()
app.processEvents()
check(quran.isVisible(), "a panel opened after restoring shows at once")

minimiser.restore()
check(widget.isVisible() and not core.isVisible(), "restoring when not minimised changes nothing")

# ---- the core dragged, as a real mouse does it -----------------------------------------------------

from PyQt6.QtCore import QEvent, QPointF  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402


def mouse(kind, screen_point, held):
    """An event as Windows sends it: the position inside the core worked out from where the core is now."""
    local = QPointF(screen_point - QPointF(core.frameGeometry().topLeft()))
    button = Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
    buttons = Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton
    QApplication.sendEvent(core, QMouseEvent(kind, local, screen_point, button, buttons,
                                             Qt.KeyboardModifier.NoModifier))


def drag(start, offset, steps=6):
    mouse(QEvent.Type.MouseButtonPress, start, True)

    for step in range(1, steps + 1):
        mouse(QEvent.Type.MouseMove, start + offset * (step / steps), True)

    mouse(QEvent.Type.MouseButtonRelease, start + offset, False)
    app.processEvents()


minimiser.minimise()
app.processEvents()
first_place = core.frameGeometry().topLeft()
centre_on_screen = QPointF(first_place) + QPointF(core.width() / 2.0, core.height() / 2.0)

drag(centre_on_screen, QPointF(-150.0, 90.0))
check(widget.minimised and core.isVisible(), "dragging the core by its centre moves it and does not bring the HUD back")
check(core.frameGeometry().topLeft() == first_place + QPoint(-150, 90), "it is where it was dropped")

rim = QPointF(core.frameGeometry().topLeft()) + QPointF(4.0, core.height() / 2.0)
mouse(QEvent.Type.MouseButtonPress, rim, True)
mouse(QEvent.Type.MouseButtonRelease, rim, False)
app.processEvents()
check(widget.minimised, "a click on the rim does nothing: only the centre brings the HUD back")

dropped = core.frameGeometry().topLeft()
centre_on_screen = QPointF(dropped) + QPointF(core.width() / 2.0, core.height() / 2.0)
mouse(QEvent.Type.MouseButtonPress, centre_on_screen, True)
mouse(QEvent.Type.MouseMove, centre_on_screen + QPointF(2.0, 1.0), True)
mouse(QEvent.Type.MouseButtonRelease, centre_on_screen + QPointF(2.0, 1.0), False)
app.processEvents()
check(not widget.minimised and widget.isVisible(), "a click on the centre, a wobble of a pixel or two, brings it back")

minimiser.minimise()
app.processEvents()
check(core.frameGeometry().topLeft() == dropped, "minimised again, the core is where it was left, not back at the corner")
minimiser.restore()
app.processEvents()

widget.muted_changed.connect(core.muted_changed.emit)
widget.state_changed.connect(core.state_changed.emit)
widget.muted_changed.emit(True)
widget.state_changed.emit(hud.SPEAKING)
app.processEvents()
check(core._muted and core._state == hud.SPEAKING, "the core shows his state and the mute while minimised")
check(not core.grab().isNull(), "and draws itself without error")
widget.muted_changed.emit(False)

# ---- a beam asked for while minimised stays hidden ---------------------------------------------------

from beam import Beam  # noqa: E402

panel = tool_window()
panel.move(100, 100)
panel.show()
widget.move(600, 100)
app.processEvents()
real_beam = Beam(panel, widget)
real_beam.shown.emit()
app.processEvents()
check(real_beam.isVisible(), "a beam joins a panel to the HUD")
# The beams skipped, as main.py skips them: they hide and show themselves.
minimiser = hud_mini.Minimiser(widget, hud_mini.MiniCore(), skip=lambda w: isinstance(w, (FakeBeam, Beam)))
minimiser.minimise()
app.processEvents()
real_beam._tick()
check(not real_beam.isVisible(), "minimised, the beam goes with them")
for _ in range(5):
    real_beam.shown.emit()      # a live view asks for its beam with every frame
    app.processEvents()
check(not real_beam.isVisible(), "and stays gone though a live view keeps asking for it, not flashing on")
minimiser.restore()
app.processEvents()
real_beam.shown.emit()
app.processEvents()
check(real_beam.isVisible(), "back with the HUD")
real_beam.hidden.emit()
panel.hide()
app.processEvents()

# ---- the radar: hidden by a minimise is not closed ------------------------------------------------

source = (ROOT / "radar_panel.py").read_text(encoding="utf-8")
hide_event = source.split("def hideEvent(self, event):", 1)[1].split("\n    def ", 1)[0]
check('if not getattr(self._anchor, "minimised", False):' in hide_event and "self.closed.emit()" in hide_event,
      "the radar's feed is stopped by closing it, not by the HUD minimising")

# ---- main.py's wiring, read from its source (it needs the desktop to import) ---------------------

source = (ROOT / "main.py").read_text(encoding="utf-8")
check("hud.minimise_clicked.connect(minimiser.minimise)" in source, "main.py: the minimise button minimises")
check("skip=lambda widget: isinstance(widget, Beam)" in source, "main.py: the beams are skipped")
check("hud.mute_clicked.connect(toggle_mute)" in source and "set_muted(muted)" in source
      and "hud.muted_changed.emit(muted)" in source, "main.py: the mute button mutes, and the HUD shows it")
check("hud.state_changed.connect(mini_core.state_changed.emit)" in source,
      "main.py: the core follows his state")

sys.exit(1 if failures else 0)
