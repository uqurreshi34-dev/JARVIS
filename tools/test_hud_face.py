"""JARVIS's face in the reactor (hud_face.py), drawn offscreen.

Checked:

- at rest there is no face, and the core is drawn as it always was;
- speaking, the face rises out of the core, and when he stops it fades
  back into it;
- the mouth follows his voice: a loud moment opens it, a quiet one closes it;
- he blinks now and then, and his eyes are open between blinks;
- the head is drawn once and kept, not drawn again every frame; a new
  colour or size draws it afresh;
- JARVIS_HUD_FACE=0 turns it off, and the HUD draws no face at all.

    python tools/test_hud_face.py
"""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import hud  # noqa: E402
import hud_face  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


ACCENT = QColor(95, 255, 195)


def drawn(face, voice=0.0, seconds=1.0, size=140):
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    face.paint(painter, QPointF(size / 2, size / 2), 44.0, ACCENT, voice, seconds)
    painter.end()
    return image


def lit(image, top=0, bottom=None):
    """How many pixels are lit, in rows [top, bottom)."""
    bottom = image.height() if bottom is None else bottom
    return sum(1 for y in range(top, bottom) for x in range(image.width()) if image.pixelColor(x, y).alpha() > 40)


face = hud_face.HoloFace()
check(lit(drawn(face)) == 0, "at rest: no face")

for _ in range(60):
    face.step(True)

check(face.shown == 1.0 and lit(drawn(face)) > 800, f"speaking, it rises out of the core ({face.shown:.2f})")

# The mouth sits in the lower part of the face; a loud moment opens it.
mouth_rows = (70 + int(0.45 * 44), 70 + int(0.9 * 44))
face._blink_at = face._frame + 1000
quiet, loud = lit(drawn(face, 0.0), *mouth_rows), lit(drawn(face, 1.0), *mouth_rows)
check(loud > quiet, f"the mouth opens with his voice ({quiet} lit, then {loud})")

blinks = []
face._blink_at = face._frame + 10

for _ in range(400):
    face.step(True)
    blinks.append(face._eyes_open())

check(min(blinks) < 0.2 and blinks.count(1.0) > 300, "he blinks now and then, eyes open between")

first = face._head(ACCENT, 44.0, 1.0)

for _ in range(5):
    drawn(face)

check(face._head(ACCENT, 44.0, 1.0) is first, "the head is drawn once and kept")
check(face._head(QColor(255, 180, 65), 44.0, 1.0) is not first, "and drawn afresh for a new colour")

for _ in range(120):
    face.step(False)

check(face.shown == 0.0 and lit(drawn(face)) == 0, "when he stops, it fades back into the core")

# ---- in the HUD ----------------------------------------------------------------------------------

def hud_image(view):
    image = QImage(view.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    view.render(painter)
    painter.end()
    return image


view = hud.Hud()
view._on_state(hud.SPEAKING)

for _ in range(30):
    view._tick()

check(view._face.shown == 0.0, "the HUD's state alone does not raise it: nothing is playing yet")

view._on_speaking(True)

for _ in range(60):
    view._on_amplitude(0.5)
    view._tick()

check(view._face is not None and view._face.shown == 1.0, "the HUD raises it while he speaks")
speaking = hud_image(view)

view._on_state(hud.IDLE)

for _ in range(120):
    view._tick()

check(view._face.shown == 0.0, "and lets it go at rest")
check(hud_image(view) is not None, "and draws the core again")

# Never stuck: the state left on SPEAKING by a reply stopped part way, but
# the speech engine says it has finished.
view._on_state(hud.SPEAKING)
view._on_speaking(True)

for _ in range(60):
    view._on_amplitude(0.5)
    view._tick()

view._on_speaking(False)

for _ in range(120):
    view._tick()

check(view._face.shown == 0.0, "the state left on speaking, but speech ended: it goes")

# Or even both left claiming speech, with the voice gone silent.
view._on_speaking(True)

for _ in range(60):
    view._on_amplitude(0.5)
    view._tick()

view._voice_at -= hud._FACE_SILENCE + 0.1

for _ in range(120):
    view._tick()

check(view._face.shown == 0.0, "and with everything left on but the voice silent, it still goes")
view._on_state(hud.IDLE)
view._on_speaking(False)

os.environ["JARVIS_HUD_FACE"] = "0"
plain = hud.Hud()
plain._on_state(hud.SPEAKING)

for _ in range(30):
    plain._tick()

check(plain._face is None and hud_image(plain) is not None, "JARVIS_HUD_FACE=0: no face, the core as it was")
os.environ.pop("JARVIS_HUD_FACE")

sys.exit(1 if failures else 0)
