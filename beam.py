import math

from PyQt6.QtCore import QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QWidget


_COLOUR = QColor(120, 210, 250)

_FRAME_MS = 33

# Each end of the projection is pulled in from its window's edge by this
# much, so the light sits inside the frame rather than against it.
_EDGE_INSET = 14

# Bands of light travelling out along the cone.
_BANDS = 4
_BAND_SPEED = 0.0055

# Fine horizontal lines across the cone, which is what makes it read as a
# projection rather than a painted shape.
_SCANLINE_GAP = 7


class Beam(QWidget):
    """A ray of light joining the news panel to the HUD.

    It is its own window because the two panels are separate top level
    windows, and nothing can be drawn in the gap between them otherwise. The
    geometry is refreshed on a timer so the beam follows either panel when
    it is dragged.

    Showing and hiding go through signals, because a Qt timer may only be
    started from the thread that owns the widget, and the assistant runs on
    a worker thread.
    """

    shown = pyqtSignal()
    hidden = pyqtSignal()

    def __init__(self, panel, hud):
        super().__init__()

        self._panel = panel
        self._hud = hud
        self._phase = 0.0
        self._hud_on_right = True
        self._hud_edge = (0.0, 0.0)
        self._panel_edge = (0.0, 0.0)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        # These slots always run on this widget's own thread, whichever
        # thread emitted the signal.
        self.shown.connect(self._follow)
        self.hidden.connect(self._stop)

    def _follow(self):
        """Start tracking the two panels. Main thread only."""
        if not self._timer.isActive():
            self._timer.start(_FRAME_MS)

        self._reposition()
        self.show()

    def _stop(self):
        """Stop tracking. Main thread only."""
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._phase = (self._phase + _BAND_SPEED) % 1.0

        self._reposition()

        if self.isVisible():
            self.update()

    def _reposition(self):
        """Span the gap, with each end anchored to the window it touches.

        The narrow end matches the HUD's height exactly and the wide end
        matches the panel's, so the light can never spill past either.
        """
        if not self._panel.isVisible() or not self._hud.isVisible():
            self.hide()
            return

        panel = self._panel.frameGeometry()
        hud = self._hud.frameGeometry()

        hud_on_right = hud.center().x() > panel.center().x()

        if hud_on_right:
            start_x, end_x = panel.right(), hud.left()
        else:
            start_x, end_x = hud.right(), panel.left()

        width = end_x - start_x

        if width < 12:
            # They overlap, so there is no gap to project across.
            self.hide()
            return

        # Tall enough to hold both windows' vertical extents.
        top = min(panel.top(), hud.top())
        bottom = max(panel.bottom(), hud.bottom())

        self.setGeometry(QRect(start_x, top, width, bottom - top))

        # Edges in this window's own coordinates, clamped to each window.
        self._hud_on_right = hud_on_right
        self._hud_edge = (hud.top() - top + _EDGE_INSET,
                          hud.bottom() - top - _EDGE_INSET)
        self._panel_edge = (panel.top() - top + _EDGE_INSET,
                            panel.bottom() - top - _EDGE_INSET)

        if not self.isVisible():
            self.show()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        height = self.height()

        if width <= 0 or height <= 0:
            painter.end()
            return

        # The light leaves the HUD and opens out to the panel, so the narrow
        # end is always the HUD's edge.
        if self._hud_on_right:
            apex_x, far_x = float(width), 0.0
        else:
            apex_x, far_x = 0.0, float(width)

        apex_top, apex_bottom = self._hud_edge
        far_top, far_bottom = self._panel_edge

        cone = QPainterPath()
        cone.moveTo(apex_x, apex_top)
        cone.lineTo(far_x, far_top)
        cone.lineTo(far_x, far_bottom)
        cone.lineTo(apex_x, apex_bottom)
        cone.closeSubpath()

        # Brightest at the lens, fading as the light spreads out.
        gradient = QLinearGradient(apex_x, 0.0, far_x, 0.0)

        near = QColor(_COLOUR)
        near.setAlpha(150)

        mid = QColor(_COLOUR)
        mid.setAlpha(78)

        far = QColor(_COLOUR)
        far.setAlpha(38)

        gradient.setColorAt(0.0, near)
        gradient.setColorAt(0.45, mid)
        gradient.setColorAt(1.0, far)

        painter.fillPath(cone, gradient)

        painter.save()
        painter.setClipPath(cone)

        self._paint_scanlines(painter, width, height)
        self._paint_bands(painter, apex_x, far_x,
                          apex_top, apex_bottom, far_top, far_bottom)

        painter.restore()

        # Crisp edges, which is what stops it looking like a smudge.
        edge = QColor(_COLOUR)
        edge.setAlpha(205)
        painter.setPen(QPen(edge, 1.8))
        painter.drawLine(QPointF(apex_x, apex_top), QPointF(far_x, far_top))
        painter.drawLine(
            QPointF(apex_x, apex_bottom), QPointF(far_x, far_bottom)
        )

        # A bright core where the light leaves the HUD.
        glow = QColor(_COLOUR)
        glow.setAlpha(210)
        painter.setPen(QPen(glow, 2.6, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawLine(
            QPointF(apex_x, apex_top + 4), QPointF(apex_x, apex_bottom - 4)
        )

        painter.end()

    def _paint_scanlines(self, painter, width, height):
        """Fine horizontal lines, the giveaway of a projected image."""
        line = QColor(_COLOUR)
        line.setAlpha(34)
        painter.setPen(QPen(line, 1.0))

        offset = int(self._phase * _SCANLINE_GAP)

        for y in range(offset, height, _SCANLINE_GAP):
            painter.drawLine(0, y, width, y)

    def _paint_bands(self, painter, apex_x, far_x,
                     apex_top, apex_bottom, far_top, far_bottom):
        """Bands of light travelling from the lens out to the panel."""
        for index in range(_BANDS):
            position = (self._phase + index / _BANDS) % 1.0

            x = apex_x + (far_x - apex_x) * position

            top = apex_top + (far_top - apex_top) * position
            bottom = apex_bottom + (far_bottom - apex_bottom) * position

            # Brightest in the middle of its journey, so bands appear to
            # travel rather than blink at the ends.
            strength = math.sin(position * math.pi)

            band = QColor(_COLOUR)
            band.setAlpha(int(140 * strength))

            painter.setPen(QPen(band, 2.4))
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
