import math

from PyQt6.QtCore import QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QWidget


_COLOUR = QColor(120, 210, 250)

# Height of the window the beam is drawn inside.
_THICKNESS = 46

_FRAME_MS = 33

# Pulses of light that travel along the beam.
_PULSES = 3
_PULSE_SPEED = 0.006


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

    def __init__(self, left, right):
        super().__init__()

        self._left = left
        self._right = right
        self._phase = 0.0

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
        self._phase = (self._phase + _PULSE_SPEED) % 1.0

        self._reposition()

        if self.isVisible():
            self.update()

    def _reposition(self):
        """Sit in the gap between the two panels, whichever way round."""
        if not self._left.isVisible() or not self._right.isVisible():
            self.hide()
            return

        left = self._left.frameGeometry()
        right = self._right.frameGeometry()

        # Work out which panel is actually on the left.
        if left.center().x() > right.center().x():
            left, right = right, left

        start = left.right()
        end = right.left()

        width = end - start

        if width < 12:
            # They overlap or touch, so there is no gap to bridge.
            self.hide()
            return

        centre = (left.center().y() + right.center().y()) // 2

        self.setGeometry(
            QRect(start, centre - _THICKNESS // 2, width, _THICKNESS)
        )

        if not self.isVisible():
            self.show()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        middle = self.height() / 2

        if width <= 0:
            painter.end()
            return

        # A soft band that fades in at both ends, so it reads as light
        # rather than a drawn line.
        gradient = QLinearGradient(0.0, 0.0, float(width), 0.0)

        faint = QColor(_COLOUR)
        faint.setAlpha(0)

        soft = QColor(_COLOUR)
        soft.setAlpha(70)

        gradient.setColorAt(0.0, faint)
        gradient.setColorAt(0.18, soft)
        gradient.setColorAt(0.82, soft)
        gradient.setColorAt(1.0, faint)

        painter.setPen(QPen(gradient, 2.0))
        painter.drawLine(
            QPointF(0.0, middle), QPointF(float(width), middle)
        )

        halo = QColor(_COLOUR)
        halo.setAlpha(22)
        painter.setPen(QPen(halo, 7.0))
        painter.drawLine(
            QPointF(0.0, middle), QPointF(float(width), middle)
        )

        # Pulses of light travelling along the beam.
        for index in range(_PULSES):
            position = (self._phase + index / _PULSES) % 1.0
            x = position * width

            glow = QColor(_COLOUR)
            glow.setAlpha(int(150 * math.sin(position * math.pi)))

            painter.setPen(QPen(glow, 3.0, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(
                QPointF(max(0.0, x - 9), middle),
                QPointF(min(float(width), x + 9), middle),
            )

        painter.end()
