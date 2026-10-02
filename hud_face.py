"""JARVIS's face: a holographic head that rises out of the reactor core while he speaks.

A wireframe head in the reactor's own colour, its mouth moving with his
voice (the same envelope that drives the core and the waveform), its eyes
glancing and blinking, a scan band passing down it, and a faint flicker,
as a projection should. It fades in when he starts speaking and back into
the core when he stops, so the reactor is the reactor again at rest.

Cheap by design, because it is drawn thirty times a second beside
everything else on the HUD: the head (outline, contours, brows, nose) is
drawn once into an image for each colour and size and kept; each frame
only lays that image down, once more inside the scan band, and draws the
two eyes and the mouth, which are a handful of short curves.

Turn it off with JARVIS_HUD_FACE=0 in .env; the core is then drawn as it
always was.
"""

import math
import os
import random

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QRadialGradient


def enabled():
    return os.getenv("JARVIS_HUD_FACE", "1").strip().lower() not in ("0", "false", "no", "off")


# The head in its own units: 0 is the middle of the face, y runs down from
# -1 (crown) to 1 (chin). One side, crown to chin; the other is its mirror.
_SIDE = ((0.0, -1.0), (0.42, -0.93), (0.64, -0.66), (0.71, -0.28), (0.68, 0.08),
         (0.6, 0.4), (0.45, 0.7), (0.24, 0.92), (0.0, 1.0))

_EYES = (-0.27, 0.27)
_EYE_Y = -0.16
_MOUTH_Y = 0.55

# How quickly the face follows his speaking, each frame, in and out.
_FADE_IN = 0.14
_FADE_OUT = 0.08

# Blinks, in frames of the HUD's 30 a second.
_BLINK_FRAMES = 5
_BLINK_EVERY = (75, 190)


def _half_width(y):
    """How wide the head is at height y, from the outline."""
    for (x0, y0), (x1, y1) in zip(_SIDE, _SIDE[1:]):
        if y0 <= y <= y1:
            share = 0.0 if y1 == y0 else (y - y0) / (y1 - y0)
            return x0 + (x1 - x0) * share

    return 0.0


def _smooth(points):
    """A closed smooth path through [points] (Catmull-Rom, as cubic curves)."""
    path = QPainterPath(QPointF(*points[0]))
    count = len(points)

    for index in range(count):
        p0, p1 = points[index - 1], points[index]
        p2, p3 = points[(index + 1) % count], points[(index + 2) % count]
        path.cubicTo(QPointF(p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6),
                     QPointF(p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6),
                     QPointF(*p2))

    return path


def _outline():
    right = list(_SIDE)
    left = [(-x, y) for x, y in reversed(_SIDE[1:-1])]
    return _smooth(right + left)


class HoloFace:
    """The face's state and drawing. paint() each frame; nothing else needs calling."""

    def __init__(self):
        self.shown = 0.0            # 0 to 1: how far it has risen out of the core
        self._layer = None
        self._layer_key = None
        self._frame = 0
        self._blink_at = random.randint(*_BLINK_EVERY)
        self._glance = 0.0
        self._glance_to = 0.0
        self._random = random.Random(7)

    # ---- the frame clock ------------------------------------------------------------------

    def step(self, speaking):
        """Advance one frame: fade towards shown while [speaking], blink, glance."""
        aim = 1.0 if speaking else 0.0
        self.shown += (aim - self.shown) * (_FADE_IN if aim > self.shown else _FADE_OUT)

        if abs(aim - self.shown) < 0.004:
            self.shown = aim

        self._frame += 1

        if self._frame >= self._blink_at + _BLINK_FRAMES:
            self._blink_at = self._frame + self._random.randint(*_BLINK_EVERY)

        if self._frame % 50 == 0:
            self._glance_to = self._random.uniform(-0.05, 0.05)

        self._glance += (self._glance_to - self._glance) * 0.12

    def _eyes_open(self):
        into = self._frame - self._blink_at

        if 0 <= into < _BLINK_FRAMES:
            return abs(into - (_BLINK_FRAMES - 1) / 2) / ((_BLINK_FRAMES - 1) / 2)

        return 1.0

    # ---- the head, drawn once per colour and size ----------------------------------------

    def _head(self, accent, unit, ratio):
        key = (accent.rgb(), round(unit, 2), ratio)

        if key == self._layer_key:
            return self._layer

        margin = 6
        width, height = int(2 * 0.72 * unit + 2 * margin), int(2 * unit + 2 * margin)
        image = QImage(int(width * ratio), int(height * ratio), QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(width / 2, height / 2)
        painter.scale(unit, unit)

        def pen(alpha, pixels):
            colour = QColor(accent)
            colour.setAlpha(alpha)
            return QPen(colour, pixels / unit, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)

        outline = _outline()

        # A faint fill, so the head reads as a solid of light, not a drawing.
        glow = QRadialGradient(QPointF(0.0, -0.1), 1.1)
        inner, outer = QColor(accent), QColor(accent)
        inner.setAlpha(46)
        outer.setAlpha(6)
        glow.setColorAt(0.0, inner)
        glow.setColorAt(1.0, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawPath(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Contours across the head, bowed as if round, and meridians down it.
        painter.save()
        painter.setClipPath(outline)
        painter.setPen(pen(52, 0.8))

        for step in range(-6, 7):
            y = step * 0.14
            half = _half_width(y) + 0.02
            path = QPainterPath(QPointF(-half, y))
            path.quadTo(QPointF(0.0, y + 0.07), QPointF(half, y))
            painter.drawPath(path)

        for share in (-0.66, -0.33, 0.0, 0.33, 0.66):
            path = QPainterPath()

            for index in range(21):
                y = -1.0 + index * 0.1
                point = QPointF(share * _half_width(y), y)
                path.moveTo(point) if index == 0 else path.lineTo(point)

            painter.drawPath(path)

        painter.restore()

        # The outline: a soft glow, then the edge itself.
        painter.setPen(pen(60, 4.0))
        painter.drawPath(outline)
        painter.setPen(pen(215, 1.3))
        painter.drawPath(outline)

        # Brows, the line of the nose, the cheekbones.
        painter.setPen(pen(170, 1.2))

        for side in (-1, 1):
            brow = QPainterPath(QPointF(side * 0.13, _EYE_Y - 0.11))
            brow.quadTo(QPointF(side * 0.28, _EYE_Y - 0.19), QPointF(side * 0.43, _EYE_Y - 0.1))
            painter.drawPath(brow)

        painter.setPen(pen(120, 1.0))
        nose = QPainterPath(QPointF(0.0, _EYE_Y + 0.02))
        nose.quadTo(QPointF(0.07, 0.18), QPointF(0.05, 0.3))
        nose.quadTo(QPointF(0.0, 0.34), QPointF(-0.05, 0.3))
        painter.drawPath(nose)

        painter.setPen(pen(70, 0.9))

        for side in (-1, 1):
            cheek = QPainterPath(QPointF(side * 0.5, 0.02))
            cheek.quadTo(QPointF(side * 0.42, 0.25), QPointF(side * 0.3, 0.33))
            painter.drawPath(cheek)

        painter.end()
        self._layer, self._layer_key = image, key
        return image

    # ---- each frame ----------------------------------------------------------------------

    def paint(self, painter, centre, unit, accent, voice, seconds):
        """Draw the face at [centre], [unit] pixels to half its height, mouth open by [voice] (0 to 1).

        [seconds] is any steadily running clock (time.monotonic()), for the
        flicker, the sway and the scan band.
        """
        if self.shown <= 0.0:
            return

        ratio = painter.device().devicePixelRatioF() if painter.device() else 1.0
        head = self._head(accent, unit, ratio)
        width, height = head.width() / ratio, head.height() / ratio

        # Rising out of the core: up from a little below, and opening out.
        rise = (1.0 - self.shown) * unit * 0.35
        flicker = 0.93 + 0.07 * math.sin(seconds * 15.0) * math.sin(seconds * 4.6)

        painter.save()
        painter.translate(centre.x(), centre.y() + rise)
        painter.rotate(1.6 * math.sin(seconds * 0.6))
        painter.scale(0.86 + 0.14 * self.shown, 0.86 + 0.14 * self.shown)

        target = QRectF(-width / 2, -height / 2, width, height)
        painter.setOpacity(self.shown * flicker)
        painter.drawImage(target, head)

        # The scan band, a brighter stripe of the same head passing down it.
        band_y = -unit * 1.2 + (seconds * 0.28 % 1.0) * 2.6 * unit
        painter.save()
        painter.setClipRect(QRectF(-width / 2, band_y, width, unit * 0.16))
        painter.setOpacity(self.shown * 0.9)
        painter.drawImage(target, head)
        painter.restore()

        painter.setOpacity(self.shown * flicker)
        painter.scale(unit, unit)
        self._paint_eyes(painter, accent, unit)
        self._paint_mouth(painter, accent, unit, voice)

        painter.restore()

    def _paint_eyes(self, painter, accent, unit):
        open_ = self._eyes_open()
        bright = QColor(235, 255, 250)

        for x in _EYES:
            half_height = 0.075 * max(0.08, open_)
            eye = QPainterPath(QPointF(x - 0.11, _EYE_Y))
            eye.quadTo(QPointF(x, _EYE_Y - 2 * half_height), QPointF(x + 0.11, _EYE_Y))
            eye.quadTo(QPointF(x, _EYE_Y + 2 * half_height), QPointF(x - 0.11, _EYE_Y))

            colour = QColor(accent)
            colour.setAlpha(80)
            painter.setPen(QPen(QColor(accent), 1.2 / unit))
            painter.setBrush(colour)
            painter.drawPath(eye)

            if open_ > 0.35:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(bright)
                painter.drawEllipse(QPointF(x + self._glance, _EYE_Y), 0.032, 0.032 * open_)

        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_mouth(self, painter, accent, unit, voice):
        voice = max(0.0, min(1.0, voice)) ** 0.8
        opening = 0.02 + 0.22 * voice
        # Wide when nearly closed, rounder as it opens, as a mouth is.
        half = 0.21 * (1.0 - 0.25 * voice)

        upper = QPainterPath(QPointF(-half, _MOUTH_Y))
        upper.quadTo(QPointF(0.0, _MOUTH_Y - 0.03 - opening * 0.25), QPointF(half, _MOUTH_Y))
        lower = QPainterPath(QPointF(half, _MOUTH_Y))
        lower.quadTo(QPointF(0.0, _MOUTH_Y + opening * 1.6), QPointF(-half, _MOUTH_Y))

        inside = QPainterPath(upper)
        inside.connectPath(lower)
        dark = QColor(accent)
        dark.setAlpha(int(40 + 90 * voice))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dark)
        painter.drawPath(inside)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(accent), 1.4 / unit, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(upper)
        painter.drawPath(lower)
