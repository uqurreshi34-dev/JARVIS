import math
import random
from collections import deque

from PyQt6.QtCore import QLineF, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget


# Square, because the thing inside it is a sphere. Smaller than a panel,
# which matters: every pixel of a translucent window is composited by
# Windows on every frame.
_SIZE = 560

_FRAME_MS = 50

# The sphere's radius as a fraction of the window, leaving room outside it
# for the rings and the voice halo.
_RADIUS = 0.29

# Points on the sphere, and how many neighbours each joins to.
_NODES = 54
_LINKS_EACH = 3

# How far the sphere swells at full volume, as a fraction of its size.
_SWELL = 0.055

# Pulses travelling the surface. Kept low: the whole-sphere breath is the
# main effect, and these are seasoning.
_MAX_PULSES = 34
_PULSE_SPEED = 0.06

# Drawing is batched by depth: every line or node in a band shares one pen,
# so a frame is a few dozen calls rather than one per primitive.
_BANDS = 4

# The sphere leans towards the viewer a little, so it turns like a globe
# on a desk rather than a wheel seen edge-on.
_TILT = 0.38

_RATES = {
    "idle": 0.04,
    "listening": 0.12,
    "thinking": 0.4,
    "speaking": 0.22,
    # Slower than speaking: the sphere should settle while he recites
    # rather than fire the way it does mid-sentence.
    "reciting": 0.14,
}

# Radians per frame the sphere turns. Thinking spins it up; reciting is
# the stillest, for the same reason as its pulse rate.
_SPIN = {
    "idle": 0.006,
    "listening": 0.010,
    "thinking": 0.034,
    "speaking": 0.014,
    "reciting": 0.004,
}

# Two gyroscope rings: (tilt of the ring's plane, turns per frame, size as
# a multiple of the sphere). Turning in opposite senses at unequal speeds
# is what keeps them from ever looking synchronised.
_RINGS = (
    (1.05, 0.021, 1.17),
    (-0.72, -0.014, 1.27),
)

# The voice halo: the last couple of seconds of his voice (or the
# microphone) running down both sides of the sphere from the top, newest
# first, mirrored so the ring has no seam and reads like a voice-print.
_HALO_SAMPLES = 37
_DIAL_TICKS = 72
_HALO_GAP = 1.36
_HALO_REACH = 0.065

# Frames a change of state takes to fade from one colour to the next.
_FADE_FRAMES = 8

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


def _mix(first, second, amount):
    return QColor(
        int(first.red() + (second.red() - first.red()) * amount),
        int(first.green() + (second.green() - first.green()) * amount),
        int(first.blue() + (second.blue() - first.blue()) * amount),
    )


class BrainPanel(QWidget):
    """A turning sphere of light that breathes with whatever JARVIS is doing.

    What never moves -- the dark backing and the glow -- is drawn once per
    state into an image and blitted. What moves is drawn live but batched:
    the sphere's links and nodes in a few depth bands, two gyroscope rings,
    the pulses and the voice halo. A frame costs a couple of milliseconds,
    which is what makes it cheap enough to leave open.
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
        self._spin = 0.0
        self._swell = 1.0
        self._drag_offset = None

        self._sphere_points = []   # unit vectors on the sphere
        self._points = []          # this frame's (x, y, depth), window space
        self._links = []
        self._pulses = []

        self._ring_angles = [0.0 for _ in _RINGS]
        self._halo = deque([0.0] * _HALO_SAMPLES, maxlen=_HALO_SAMPLES)

        # The colour fades between states rather than snapping.
        self._colour = QColor(_COLOURS["idle"])
        self._fade_from = QColor(self._colour)
        self._fade = 1.0

        self._backdrops = {}       # state -> backing and glow, drawn once
        self._previous_state = "idle"

        self._build()
        self._project()

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

        for index in range(_NODES):
            # A Fibonacci sphere: even coverage without clustering at the
            # poles, which a naive latitude and longitude grid gives.
            y = 1 - (index / (_NODES - 1)) * 2
            ring = math.sqrt(max(0.0, 1 - y * y))
            angle = index * 2.399963

            self._sphere_points.append((math.cos(angle) * ring, y, math.sin(angle) * ring))

        placed = self._sphere_points

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

    def _project(self):
        """Turn the sphere to this frame's angle and place it in the window."""
        # The swell is applied here, to the coordinates, rather than as a
        # painter transform: a scaled painter loses Qt's fast paths and cost
        # a millisecond a frame.
        radius = _SIZE * _RADIUS * self._swell
        centre = _SIZE / 2

        spin_cos, spin_sin = math.cos(self._spin), math.sin(self._spin)
        tilt_cos, tilt_sin = math.cos(_TILT), math.sin(_TILT)

        projected = []

        for x, y, z in self._sphere_points:
            # Turn about the vertical axis, then lean towards the viewer.
            x, z = x * spin_cos + z * spin_sin, z * spin_cos - x * spin_sin
            y, z = y * tilt_cos - z * tilt_sin, z * tilt_cos + y * tilt_sin

            # A gentle perspective, so the front reads as nearer than the back.
            scale = 0.78 + 0.22 * (z + 1) / 2

            projected.append((
                centre + x * radius * scale,
                centre + y * radius * scale,
                (z + 1) / 2,
            ))

        self._points = projected

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
            self._previous_state = self._state
            self._fade_from = QColor(self._colour)
            self._fade = 0.0
            self._state = settled

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

        # The voice only reaches the halo while there is a voice to show.
        heard = self._smoothed if self._state in ("listening", "speaking", "reciting") else 0.0
        self._halo.appendleft(heard)

        spin = _SPIN.get(self._state, _SPIN["idle"]) * (1.0 + self._smoothed * 1.5)
        self._spin = (self._spin + spin) % (2 * math.pi)

        speed = 2.2 if self._state == "thinking" else 1.0

        for index, (_tilt, turn, _size) in enumerate(_RINGS):
            self._ring_angles[index] = (self._ring_angles[index] + turn * speed) % (2 * math.pi)

        if self._fade < 1.0:
            self._fade = min(1.0, self._fade + 1.0 / _FADE_FRAMES)

        target = _COLOURS.get(self._state, _COLOURS["idle"])
        self._colour = _mix(self._fade_from, target, self._fade) if self._fade < 1.0 else QColor(target)

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

        # The whole sphere swells with his voice, with a slow breath beneath
        # so it is never completely still.
        breath = 0.5 + 0.5 * math.sin(self._phase)
        self._swell = 1.0 + (self._smoothed * _SWELL) + (breath * 0.012)

        self._project()
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

    def _accent(self, alpha=255, colour=None):
        colour = QColor(colour or self._colour)

        # Callers work out alpha from depth and strength, which can land
        # outside the range; Qt warns rather than clamping, so it is done
        # here once instead of at every call site.
        colour.setAlpha(max(0, min(255, int(alpha))))

        return colour

    def _backdrop(self, state):
        """The backing disc and glow for a state, drawn once and kept."""
        if state in self._backdrops:
            return self._backdrops[state]

        colour = _COLOURS.get(state, _COLOURS["idle"])

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

        gradient.setColorAt(0.0, self._accent(70, colour))
        gradient.setColorAt(0.55, self._accent(34, colour))
        gradient.setColorAt(1.0, self._accent(0, colour))

        painter.fillRect(0, 0, _SIZE, _SIZE, gradient)

        # A hot core, which the voice brightens through the frame's opacity.
        core = QRadialGradient(_SIZE / 2, _SIZE / 2, _SIZE * _RADIUS * 0.55)

        core.setColorAt(0.0, self._accent(90, colour))
        core.setColorAt(1.0, self._accent(0, colour))

        painter.fillRect(0, 0, _SIZE, _SIZE, core)

        # A fine dial where the voice halo rises from: its floor when he
        # speaks, an instrument bezel when he doesn't. Drawn here, once, it
        # costs nothing a frame.
        centre = _SIZE / 2
        dial = _SIZE * _RADIUS * _HALO_GAP

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._accent(48, colour), 1.0))
        painter.drawEllipse(QPointF(centre, centre), dial, dial)

        ticks = []

        for index in range(_DIAL_TICKS):
            theta = index / _DIAL_TICKS * 2 * math.pi
            length = 7 if index % 6 == 0 else 3

            cos_t, sin_t = math.cos(theta), math.sin(theta)
            ticks.append(QLineF(
                centre + cos_t * (dial - length), centre + sin_t * (dial - length),
                centre + cos_t * dial, centre + sin_t * dial,
            ))

        painter.setPen(QPen(self._accent(70, colour), 1.0))
        painter.drawLines(ticks)

        painter.end()

        self._backdrops[state] = image

        return image

    def paintEvent(self, event):
        painter = QPainter(self)

        breath = 0.5 + 0.5 * math.sin(self._phase)
        glow = 0.82 + 0.18 * min(1.0, self._smoothed * 1.7 + breath * 0.12)

        # The backdrop is blitted unscaled -- half the cost of a scaled one --
        # and brightens with his voice through its opacity. It fades across a
        # change of state too.
        if self._fade < 1.0:
            painter.setOpacity(glow * (1.0 - self._fade))
            painter.drawImage(QPointF(0, 0), self._backdrop(self._previous_state))

        painter.setOpacity(glow * self._fade)
        painter.drawImage(QPointF(0, 0), self._backdrop(self._state))
        painter.setOpacity(1.0)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        self._paint_rings(painter, behind=True)
        self._paint_sphere(painter)
        self._paint_pulses(painter)
        self._paint_rings(painter, behind=False)
        self._paint_halo(painter, self._swell)

        painter.end()

    def _paint_sphere(self, painter):
        """Links and nodes, in depth bands: dim and small at the back."""
        lines = [[] for _ in range(_BANDS)]
        nodes = [[] for _ in range(_BANDS)]

        for a, b in self._links:
            ax, ay, az = self._points[a]
            bx, by, bz = self._points[b]

            band = min(_BANDS - 1, int((az + bz) / 2 * _BANDS))
            lines[band].append(QLineF(ax, ay, bx, by))

        for x, y, z in self._points:
            band = min(_BANDS - 1, int(z * _BANDS))
            nodes[band].append(QPointF(x, y))

        for band in range(_BANDS):
            depth = (band + 0.5) / _BANDS

            if lines[band]:
                # Smoothing long lines is most of a frame's cost. The back
                # half is dim enough that its steps do not show, so only the
                # front half pays for it.
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, band >= _BANDS // 2)
                painter.setPen(QPen(self._accent(26 + 62 * depth), 1.0))
                painter.drawLines(lines[band])
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # Filled circles, not wide round-capped points: the same look at
            # a twentieth of the cost.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._accent(95 + 160 * depth))

            for point in nodes[band]:
                painter.drawEllipse(point, 1.9 + 2.6 * depth, 1.9 + 2.6 * depth)

        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _ring_points(self, index, steps):
        """Points round one gyroscope ring, with their depth."""
        tilt, _turn, size = _RINGS[index]
        angle = self._ring_angles[index]

        radius = _SIZE * _RADIUS * size * self._swell
        centre = _SIZE / 2

        cos_t, sin_t = math.cos(tilt), math.sin(tilt)
        cos_a, sin_a = math.cos(angle), math.sin(angle)

        for step in range(steps):
            theta = step / steps * 2 * math.pi

            # A circle in its own plane, tipped by the tilt, then turned
            # about the vertical axis by its own angle.
            x, y, z = math.cos(theta), math.sin(theta) * cos_t, math.sin(theta) * sin_t
            x, z = x * cos_a + z * sin_a, z * cos_a - x * sin_a

            yield theta, centre + x * radius, centre + y * radius, (z + 1) / 2

    def _paint_rings(self, painter, behind):
        """The back half of each ring before the sphere, the front after."""
        for index in range(len(_RINGS)):
            points = list(self._ring_points(index, 64))
            segments = []

            for (_, x1, y1, z1), (_, x2, y2, _z2) in zip(points, points[1:] + points[:1]):
                if (z1 < 0.5) == behind:
                    segments.append(QLineF(x1, y1, x2, y2))

            if segments:
                painter.setPen(QPen(self._accent(38 if behind else 92), 1.1))
                painter.drawLines(segments)

            # A comet runs each ring, with a short tail behind it.
            head = int(((self._phase * (3 + index)) / (2 * math.pi)) * len(points)) % len(points)

            for trail in range(6):
                _, x, y, z = points[(head - trail * 2) % len(points)]

                if (z < 0.5) != behind:
                    continue

                strength = (1 - trail / 6) * (0.45 + 0.55 * z)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self._accent(255 * strength))
                painter.drawEllipse(QPointF(x, y), 3.2 - trail * 0.4, 3.2 - trail * 0.4)

        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_pulses(self, painter):
        painter.setPen(Qt.PenStyle.NoPen)

        for pulse in self._pulses:
            a, b = self._links[pulse["link"]]

            start = self._points[b if pulse["back"] else a]
            finish = self._points[a if pulse["back"] else b]

            depth = (start[2] + finish[2]) / 2

            # Two faint dots behind the head give it a trail of light.
            for trail, size in ((0.0, 2.6), (0.08, 1.9), (0.16, 1.3)):
                position = pulse["at"] - trail

                if position <= 0:
                    continue

                x = start[0] + (finish[0] - start[0]) * position
                y = start[1] + (finish[1] - start[1]) * position

                strength = math.sin(position * math.pi) * (1 - trail * 3)

                painter.setBrush(self._accent(220 * strength * (0.4 + depth)))
                painter.drawEllipse(QPointF(x, y), size, size)

        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_halo(self, painter, swell):
        """A ring round the sphere, pushed out by his voice a moment ago.

        One outline rather than a tick per sample: stroking cost follows
        the length of line drawn, and a wave round the sphere carries the
        same picture in a third of the ink.
        """
        if max(self._halo) < 0.02:
            return

        centre = _SIZE / 2
        inner = _SIZE * _RADIUS * _HALO_GAP * swell
        reach = _SIZE * _HALO_REACH

        samples = list(self._halo)
        last = len(samples) - 1

        # Down the right side from the top, then back up the left: the two
        # sides meet at the newest sample above and the oldest below.
        order = [(index, 1) for index in range(len(samples))] + [(index, -1) for index in range(last - 1, 0, -1)]

        outline = []

        for index, side in order:
            theta = -math.pi / 2 + side * index / last * math.pi
            radius = inner + reach * min(1.0, samples[index] * 1.4)

            outline.append(QPointF(centre + math.cos(theta) * radius, centre + math.sin(theta) * radius))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._accent(170), 1.6))
        painter.drawPolygon(QPolygonF(outline))
