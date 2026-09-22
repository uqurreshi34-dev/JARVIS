"""The sky around you, drawn.

aircraft.py knows what is up there. This draws it: a radar face with you
at the centre, north at the top, and every aircraft as a mark pointing
the way it is actually flying.

The feeds are polled every few seconds, but nothing on screen waits for
them. Between fetches each aircraft is advanced along its own track at
its own speed, and climbed or descended at its own rate, so the marks
glide and their altitudes drift rather than jumping on every refresh.
That is dead reckoning, and it is what the position readouts in a real
cockpit do between fixes -- it is also the difference between a display
that looks alive and one that twitches every twelve seconds.

Nothing here fetches anything. The panel is given aircraft through a
signal, the same way the Quran page is given verses, so it can be tested
with invented traffic and never needs the network to draw.
"""

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget


_WIDTH = 520
_HEIGHT = 520

# Matches the Quran page, so the beam meets it the same way.
_BEAM_GAP = 74

_FRAME_MS = 33

# How long the sweep takes to go round once, in seconds. Slow enough to
# read, fast enough to feel live.
_SWEEP_SECONDS = 4.0

# Metres per degree of latitude. Good to a fraction of a percent, which
# is far beyond what a radar face twenty miles across can show.
_METRES_PER_DEGREE = 111320.0

_METRES_PER_NM = 1852.0
_METRES_PER_FOOT = 0.3048

# Altitude colours. Low is warm, high is cold, and everything between is
# mixed from the two -- so height reads at a glance without a legend.
_LOW = QColor(255, 186, 92)
_HIGH = QColor(120, 210, 250)

# The altitude at which a mark is fully _HIGH. Roughly airliner cruise.
_CEILING = 11000.0

_FACE = QColor(10, 18, 26)
_RING = QColor(120, 210, 250)
_TEXT = QColor(214, 238, 250)


def _advance(entry, seconds):
    """Where an aircraft will be after this long, and how high.

    Straight-line from its last known fix along its own track. Over the
    few seconds between fetches that is accurate to a few metres, and it
    is discarded the moment a real position arrives.
    """
    lat = entry.get("latitude")
    lon = entry.get("longitude")
    altitude = entry.get("altitude")

    if lat is None or lon is None or seconds <= 0:
        return lat, lon, altitude

    speed = entry.get("speed")
    track = entry.get("track")

    if speed and track is not None:
        distance = speed * seconds
        heading = math.radians(track)

        lat = lat + distance * math.cos(heading) / _METRES_PER_DEGREE
        lon = lon + distance * math.sin(heading) / (
            _METRES_PER_DEGREE * max(0.1, math.cos(math.radians(lat)))
        )

    climb = entry.get("climb")

    if climb and altitude is not None:
        altitude = max(0.0, altitude + climb * seconds)

    return lat, lon, altitude


def _blend(low, high, amount):
    """A colour between two, by fraction."""
    amount = max(0.0, min(1.0, amount))

    return QColor(
        int(low.red() + (high.red() - low.red()) * amount),
        int(low.green() + (high.green() - low.green()) * amount),
        int(low.blue() + (high.blue() - low.blue()) * amount),
    )


class RadarPanel(QWidget):
    """Live traffic, projected beside the HUD."""

    # In.
    updated = pyqtSignal(list, tuple, str)
    message = pyqtSignal(str)

    # Out.
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()

        self._aircraft = []
        self._centre = None
        self._source = ""
        self._note = ""
        self._fetched_at = 0.0
        self._sweep = 0.0
        self._radius_nm = 25.0
        self._anchor = None
        self._drag_offset = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.updated.connect(self._on_updated)
        self.message.connect(self._on_message)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    # ---- the outside world ---------------------------------------------

    def set_anchor(self, widget):
        """Sit alongside another window, joined by the beam."""
        self._anchor = widget

    def set_radius(self, radius_nm):
        """How far the outer ring is, in nautical miles."""
        self._radius_nm = max(1.0, float(radius_nm))

    def _on_updated(self, aircraft, centre, source):
        self._aircraft = list(aircraft)
        self._centre = tuple(centre[:2]) if centre else None
        self._source = source or ""
        self._note = ""
        self._fetched_at = time.monotonic()

        if not self.isVisible():
            self._position()
            self.show()

        if not self._animate.isActive():
            self._animate.start(_FRAME_MS)

        self.update()

    def _on_message(self, text):
        self._note = text or ""
        self.update()

    def hideEvent(self, event):
        self._animate.stop()
        super().hideEvent(event)
        self.closed.emit()

    # ---- placement and dragging ----------------------------------------

    def _position(self):
        screen = self.screen().availableGeometry()

        if self._anchor is not None and self._anchor.isVisible():
            frame = self._anchor.frameGeometry()

            x = frame.left() - _WIDTH - _BEAM_GAP
            y = frame.center().y() - _HEIGHT // 2

            x = max(screen.left() + 12, x)
            y = max(screen.top() + 12,
                    min(y, screen.bottom() - _HEIGHT - 12))

            self.move(int(x), int(y))
            return

        self.move(
            screen.right() - _WIDTH - 500,
            screen.bottom() - _HEIGHT - 24,
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.pos()
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    # ---- drawing --------------------------------------------------------

    def _tick(self):
        self._sweep = (self._sweep + _FRAME_MS / 1000.0 / _SWEEP_SECONDS) % 1.0
        self.update()

    def _plot(self, entry, elapsed, centre_point, pixels_per_metre):
        """Where a mark goes on the face, and how high it is by then.

        Returns (point, altitude) or None when it is off the face.
        """
        if not self._centre:
            return None

        lat, lon, altitude = _advance(entry, elapsed)

        if lat is None or lon is None:
            return None

        # Flat earth is fine across twenty-odd miles, and it keeps this
        # to two multiplications per aircraft per frame.
        north = (lat - self._centre[0]) * _METRES_PER_DEGREE
        east = (lon - self._centre[1]) * _METRES_PER_DEGREE * math.cos(
            math.radians(self._centre[0])
        )

        x = centre_point.x() + east * pixels_per_metre
        y = centre_point.y() - north * pixels_per_metre

        return QPointF(x, y), altitude

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        face = QRectF(self.rect()).adjusted(18, 18, -18, -18)
        centre_point = face.center()
        radius = min(face.width(), face.height()) / 2.0

        self._paint_face(painter, centre_point, radius)
        self._paint_rings(painter, centre_point, radius)
        self._paint_sweep(painter, centre_point, radius)
        self._paint_aircraft(painter, centre_point, radius)
        self._paint_legend(painter)

        painter.end()

    def _paint_face(self, painter, centre_point, radius):
        glow = QRadialGradient(centre_point, radius)

        inner = QColor(_FACE)
        inner.setAlpha(238)

        outer = QColor(_FACE)
        outer.setAlpha(198)

        glow.setColorAt(0.0, inner)
        glow.setColorAt(1.0, outer)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(centre_point, radius, radius)

    def _paint_rings(self, painter, centre_point, radius):
        """Range rings and compass ticks, labelled in miles."""
        painter.setBrush(Qt.BrushStyle.NoBrush)

        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)

        # Four rings, so each is a quarter of the range whatever the
        # range happens to be.
        rings = 4

        for step in range(1, rings + 1):
            fraction = step / rings

            colour = QColor(_RING)
            colour.setAlpha(58 if step < rings else 120)
            painter.setPen(QPen(colour, 1.0))
            painter.drawEllipse(centre_point, radius * fraction,
                                radius * fraction)

            label = QColor(_RING)
            label.setAlpha(110)
            painter.setPen(QPen(label))
            # Just inside the ring rather than on it, so the outermost
            # label cannot collide with the N at the top of the face.
            painter.drawText(
                QPointF(centre_point.x() + 5,
                        centre_point.y() - radius * fraction + 12),
                f"{self._radius_nm * fraction:.0f}",
            )

        spokes = QColor(_RING)
        spokes.setAlpha(40)
        painter.setPen(QPen(spokes, 1.0))

        for degrees in range(0, 360, 30):
            angle = math.radians(degrees - 90)
            painter.drawLine(
                QPointF(centre_point.x() + math.cos(angle) * radius * 0.18,
                        centre_point.y() + math.sin(angle) * radius * 0.18),
                QPointF(centre_point.x() + math.cos(angle) * radius,
                        centre_point.y() + math.sin(angle) * radius),
            )

        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(_TEXT))

        for name, degrees in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            angle = math.radians(degrees - 90)
            painter.drawText(
                QRectF(centre_point.x() + math.cos(angle) * (radius + 9) - 8,
                       centre_point.y() + math.sin(angle) * (radius + 9) - 8,
                       16, 16),
                Qt.AlignmentFlag.AlignCenter,
                name,
            )

        # You, at the middle.
        painter.setPen(QPen(_TEXT, 1.4))
        painter.drawLine(QPointF(centre_point.x() - 5, centre_point.y()),
                         QPointF(centre_point.x() + 5, centre_point.y()))
        painter.drawLine(QPointF(centre_point.x(), centre_point.y() - 5),
                         QPointF(centre_point.x(), centre_point.y() + 5))

    def _paint_sweep(self, painter, centre_point, radius):
        """The turning wedge. Decorative, and it makes the face read live."""
        angle = self._sweep * 360.0

        path = QPainterPath()
        path.moveTo(centre_point)
        path.arcTo(QRectF(centre_point.x() - radius, centre_point.y() - radius,
                          radius * 2, radius * 2),
                   90.0 - angle, -46.0)
        path.closeSubpath()

        wedge = QColor(_RING)
        wedge.setAlpha(26)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(wedge)
        painter.drawPath(path)

        leading = math.radians(angle - 90)
        edge = QColor(_RING)
        edge.setAlpha(150)

        painter.setPen(QPen(edge, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(
            centre_point,
            QPointF(centre_point.x() + math.cos(leading) * radius,
                    centre_point.y() + math.sin(leading) * radius),
        )

    def _paint_aircraft(self, painter, centre_point, radius):
        if not self._aircraft or not self._centre:
            return

        elapsed = time.monotonic() - self._fetched_at
        pixels_per_metre = radius / (self._radius_nm * _METRES_PER_NM)

        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)

        for entry in self._aircraft:
            plotted = self._plot(entry, elapsed, centre_point,
                                 pixels_per_metre)

            if not plotted:
                continue

            point, altitude = plotted

            # Off the face entirely -- it has flown out of range since
            # the last fix.
            offset = math.hypot(point.x() - centre_point.x(),
                                point.y() - centre_point.y())

            if offset > radius - 2:
                continue

            colour = _blend(_LOW, _HIGH,
                            (altitude or 0.0) / _CEILING)

            self._paint_mark(painter, point, entry.get("track"), colour)
            self._paint_tag(painter, point, entry, altitude, colour)

    def _paint_mark(self, painter, point, track, colour):
        """A small delta pointing the way the aircraft is going."""
        painter.save()
        painter.translate(point)

        if track is not None:
            painter.rotate(track)

        body = QPolygonF([
            QPointF(0.0, -6.5),
            QPointF(4.4, 5.0),
            QPointF(0.0, 2.6),
            QPointF(-4.4, 5.0),
        ])

        halo = QColor(colour)
        halo.setAlpha(70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QPointF(0.0, 0.0), 9.0, 9.0)

        painter.setBrush(colour)
        painter.setPen(QPen(QColor(12, 20, 28), 0.8))
        painter.drawPolygon(body)

        painter.restore()

    def _paint_tag(self, painter, point, entry, altitude, colour):
        """Callsign and height, sitting just above the mark."""
        name = (entry.get("callsign") or entry.get("registration")
                or entry.get("type") or "")

        if not name:
            return

        climb = entry.get("climb") or 0.0

        # Feet, to the nearest hundred, which is how altitude is spoken
        # and how it is shown on every other display of this kind.
        if altitude is None:
            height = ""
        else:
            hundreds = int(round(altitude / _METRES_PER_FOOT / 100.0))
            # Half a metre a second is about a hundred feet a minute --
            # below that an aircraft is level, not climbing.
            arrow = "\u2191" if climb > 0.5 else (
                "\u2193" if climb < -0.5 else ""
            )
            height = f"{hundreds * 100:,}{arrow}"

        painter.setPen(QPen(_TEXT))
        painter.drawText(
            QRectF(point.x() - 46, point.y() - 27, 92, 11),
            Qt.AlignmentFlag.AlignCenter,
            name,
        )

        if height:
            faint = QColor(colour)
            faint.setAlpha(205)
            painter.setPen(QPen(faint))
            painter.drawText(
                QRectF(point.x() - 46, point.y() - 17, 92, 11),
                Qt.AlignmentFlag.AlignCenter,
                height,
            )

    def _paint_legend(self, painter):
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        if self._note:
            painter.setPen(QPen(_TEXT))
            painter.drawText(QRectF(14, _HEIGHT - 24, _WIDTH - 28, 16),
                             Qt.AlignmentFlag.AlignCenter, self._note)
            return

        faint = QColor(_TEXT)
        faint.setAlpha(150)
        painter.setPen(QPen(faint))

        painter.drawText(QRectF(14, 8, _WIDTH - 28, 16),
                         Qt.AlignmentFlag.AlignLeft,
                         f"{len(self._aircraft)} contacts")

        painter.drawText(QRectF(14, 8, _WIDTH - 28, 16),
                         Qt.AlignmentFlag.AlignRight,
                         f"{self._radius_nm:.0f} nm  ·  {self._source}")
