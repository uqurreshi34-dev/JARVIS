import math
import random

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget


# Square, because the thing inside it is a sphere. Smaller than a panel,
# which matters: every pixel of a translucent window is composited by
# Windows on every frame.
_SIZE = 560

_FRAME_MS = 50

# Points on the sphere, and how many neighbours each joins to.
_NODES = 54
_LINKS_EACH = 3

# How far the sphere swells at full volume, as a fraction of its size.
_SWELL = 0.055

# Pulses travelling the surface. Kept low: the whole-sphere breath is the
# main effect, and these are seasoning.
_MAX_PULSES = 34
_PULSE_SPEED = 0.06

_RATES = {
    "idle": 0.04,
    "listening": 0.12,
    "thinking": 0.4,
    "speaking": 0.22,
    # Slower than speaking: the sphere should settle while he recites
    # rather than fire the way it does mid-sentence.
    "reciting": 0.14,
}

# A dark disc behind the sphere. Without it the whole thing disappears
# against a pale desktop, since everything drawn is light on nothing.
_BACKING = QColor(8, 13, 19)
_BACKING_ALPHA = 205

_COLOURS = {
    "idle": QColor(120, 175, 215),
    "listening": QColor(95, 210, 245),
    "thinking": QColor(255, 190, 90),
    "speaking": QColor(95, 235, 160),
    # The same gold the HUD and the recitation page use, so the three
    # read as one instrument rather than three that happen to be open.
    "reciting": QColor(205, 175, 95),
}


class BrainPanel(QWidget):
    """A sphere of light that breathes with whatever JARVIS is doing.

    The sphere itself never changes shape, so it is drawn once into an
    image and then scaled and brightened each frame. That is a blit and a
    handful of pulses per frame rather than a hundred primitives, which is
    what makes it cheap enough to leave open.
    """

    show_brain = pyqtSignal()
    hide_brain = pyqtSignal()

    state_changed = pyqtSignal(str)

    # His voice while speaking, the microphone while listening.
    amplitude_changed = pyqtSignal(float)
    level_changed = pyqtSignal(float)

    def __init__(self):
        super().__init__()

        self._state = "idle"
        self._level = 0.0
        self._smoothed = 0.0
        self._phase = 0.0
        self._drag_offset = None

        self._points = []
        self._links = []
        self._pulses = []

        self._sphere = None
        self._sphere_state = None

        self._build()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_SIZE, _SIZE)

        self.show_brain.connect(self._on_show)
        self.hide_brain.connect(self._on_hide)
        self.state_changed.connect(self._on_state)
        self.amplitude_changed.connect(self._on_level)
        self.level_changed.connect(self._on_level)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---------------------------------------------------------------- build

    def _build(self):
        """Scatter points evenly over a sphere and join near neighbours."""
        random.seed(11)

        radius = _SIZE * 0.36
        centre = _SIZE / 2

        placed = []

        for index in range(_NODES):
            # A Fibonacci sphere: even coverage without clustering at the
            # poles, which a naive latitude and longitude grid gives.
            y = 1 - (index / (_NODES - 1)) * 2
            ring = math.sqrt(max(0.0, 1 - y * y))
            angle = index * 2.399963

            placed.append((math.cos(angle) * ring, y, math.sin(angle) * ring))

        for x, y, z in placed:
            # A gentle perspective, so the front of the sphere reads as
            # nearer than the back.
            scale = 0.78 + 0.22 * (z + 1) / 2

            self._points.append((
                centre + x * radius * scale,
                centre + y * radius * scale,
                (z + 1) / 2,
            ))

        for a, first in enumerate(placed):
            near = []

            for b, second in enumerate(placed):
                if a == b:
                    continue

                gap = sum((p - q) ** 2 for p, q in zip(first, second))
                near.append((gap, b))

            near.sort()

            for _, b in near[:_LINKS_EACH]:
                if (b, a) not in self._links:
                    self._links.append((a, b))

    # ---------------------------------------------------------------- slots

    def set_anchor(self, widget):
        """Kept so existing wiring still works; the sphere sits centrally."""
        self._anchor = widget

    def _on_show(self):
        self._position()
        self.show()
        self.raise_()

        if not self._timer.isActive():
            self._timer.start(_FRAME_MS)

    def _on_hide(self):
        self._timer.stop()
        self.hide()

    def _on_state(self, state):
        settled = state if state in _COLOURS else "idle"

        if settled != self._state:
            self._state = settled
            self._sphere = None

    def _on_level(self, value):
        self._level = max(0.0, min(1.0, float(value)))

    def _position(self):
        screen = self.screen().availableGeometry()

        self.move(
            screen.left() + (screen.width() - _SIZE) // 2,
            screen.top() + (screen.height() - _SIZE) // 2,
        )

    # --------------------------------------------------------------- moving

    def _tick(self):
        self._phase = (self._phase + 0.035) % (2 * math.pi)

        # Smooth the level, or the sphere jitters on every syllable.
        self._smoothed = self._smoothed * 0.72 + self._level * 0.28

        rate = _RATES.get(self._state, 0.04) + self._smoothed * 0.3

        if self._links and random.random() < rate:
            self._pulses.append({
                "link": random.randrange(len(self._links)),
                "at": 0.0,
                "back": random.random() < 0.5,
            })

        alive = []

        for pulse in self._pulses:
            pulse["at"] += _PULSE_SPEED

            if pulse["at"] < 1.0:
                alive.append(pulse)

        self._pulses = alive[:_MAX_PULSES]

        self.update()

    # -------------------------------------------------------------- drawing

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event):
        self._on_hide()

    def _accent(self, alpha=255):
        colour = QColor(_COLOURS.get(self._state, _COLOURS["idle"]))

        # Callers work out alpha from depth and strength, which can land
        # outside the range; Qt warns rather than clamping, so it is done
        # here once instead of at every call site.
        colour.setAlpha(max(0, min(255, int(alpha))))

        return colour

    def _make_sphere(self):
        """Draw the sphere once. Only its size and brightness change after."""
        image = QImage(_SIZE, _SIZE, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # A dark disc first, fading out at the edge, so the sphere reads
        # against a white window as well as a dark one. It is what gives
        # the thing presence rather than looking like a faint overlay.
        backing = QRadialGradient(_SIZE / 2, _SIZE / 2, _SIZE * 0.5)

        near = QColor(_BACKING)
        near.setAlpha(_BACKING_ALPHA)

        middle = QColor(_BACKING)
        middle.setAlpha(int(_BACKING_ALPHA * 0.72))

        gone = QColor(_BACKING)
        gone.setAlpha(0)

        backing.setColorAt(0.0, near)
        backing.setColorAt(0.62, middle)
        backing.setColorAt(1.0, gone)

        painter.fillRect(0, 0, _SIZE, _SIZE, backing)

        # Then the glow, which now sits on something dark and so shows.
        gradient = QRadialGradient(_SIZE / 2, _SIZE / 2, _SIZE * 0.5)

        gradient.setColorAt(0.0, self._accent(70))
        gradient.setColorAt(0.55, self._accent(34))
        gradient.setColorAt(1.0, self._accent(0))

        painter.fillRect(0, 0, _SIZE, _SIZE, gradient)

        # Links, dimmer at the back of the sphere.
        for a, b in self._links:
            ax, ay, az = self._points[a]
            bx, by, bz = self._points[b]

            depth = (az + bz) / 2

            painter.setPen(QPen(self._accent(int(26 + 62 * depth)), 1.2))
            painter.drawLine(QPointF(ax, ay), QPointF(bx, by))

        painter.setPen(Qt.PenStyle.NoPen)

        for x, y, z in self._points:
            painter.setBrush(self._accent(int(95 + 160 * z)))
            painter.drawEllipse(QPointF(x, y), 1.9 + 2.6 * z, 1.9 + 2.6 * z)

        painter.end()

        self._sphere = image
        self._sphere_state = self._state

    def paintEvent(self, event):
        if self._sphere is None or self._sphere_state != self._state:
            self._make_sphere()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        breath = 0.5 + 0.5 * math.sin(self._phase)

        # The whole sphere swells with his voice, with a slow breath beneath
        # so it is never completely still.
        swell = 1.0 + (self._smoothed * _SWELL) + (breath * 0.012)

        span = _SIZE * swell
        offset = (_SIZE - span) / 2

        # One blit carries the halo, the links and every node. Brightness
        # rides on the opacity rather than being repainted.
        painter.setOpacity(
            0.82 + 0.18 * min(1.0, self._smoothed * 1.7 + breath * 0.12)
        )
        painter.drawImage(QRectF(offset, offset, span, span), self._sphere)
        painter.setOpacity(1.0)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_pulses(painter, swell)

        painter.end()

    def _paint_pulses(self, painter, swell):
        centre = _SIZE / 2

        for pulse in self._pulses:
            a, b = self._links[pulse["link"]]

            start = self._points[b if pulse["back"] else a]
            finish = self._points[a if pulse["back"] else b]

            position = pulse["at"]

            x = start[0] + (finish[0] - start[0]) * position
            y = start[1] + (finish[1] - start[1]) * position

            # Follow the sphere as it swells, or the pulses drift off it.
            x = centre + (x - centre) * swell
            y = centre + (y - centre) * swell

            strength = math.sin(position * math.pi)
            depth = (start[2] + finish[2]) / 2

            painter.setBrush(self._accent(int(200 * strength * (0.4 + depth))))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x, y), 2.4, 2.4)

        painter.setBrush(Qt.BrushStyle.NoBrush)
