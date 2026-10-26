"""The HUD minimised: a small glowing core left on screen, clicked to bring everything back.

The HUD and its panels are frameless tool windows, so Windows gives them no
taskbar button and no minimise. Minimising here hides the HUD and every panel
showing, and leaves the core where the HUD's minimise button was. Clicking the
core shows the HUD again, with the panels that were showing.

A panel asked to open or close while minimised -- by the phone, an
announcement, or its own timer -- is not shown at once; what it asked for is
remembered, and done when the HUD comes back. So a panel that closed itself
meanwhile stays closed, and one opened meanwhile appears with the HUD.
"""

import math
import time

from PyQt6.QtCore import QObject, QPoint, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import hud as hud_module


_SIZE = 46

# Gap between the core and the HUD's top-right corner, where the minimise
# button is, so the core appears under the pointer that minimised.
_INSET = 6

_FRAME_MS = 50


class MiniCore(QWidget):
    """The minimised HUD: a small core in his current colour. Click to restore; drag to move."""

    clicked = pyqtSignal()
    state_changed = pyqtSignal(str)
    muted_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()

        self._state = hud_module.IDLE
        self._muted = False
        self._press = None
        self._drag_offset = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_SIZE, _SIZE)
        self.setToolTip("JARVIS -- click to show")

        self.state_changed.connect(self._on_state)
        self.muted_changed.connect(self._on_muted)

        # Only runs while the core is showing.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)

    def _on_state(self, state):
        self._state = state if state in hud_module._PALETTE else hud_module.IDLE
        self.update()

    def _on_muted(self, muted):
        self._muted = bool(muted)
        self.update()

    def showEvent(self, event):
        self._timer.start(_FRAME_MS)
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position()
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press is not None:
            if (event.position() - self._press).manhattanLength() < 6:
                self.clicked.emit()

        self._press = None
        self._drag_offset = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        accent = hud_module._PALETTE.get(self._state, hud_module._PALETTE[hud_module.IDLE])
        centre = QPointF(_SIZE / 2.0, _SIZE / 2.0)
        breath = 0.5 + 0.5 * math.sin(time.monotonic() * 2.2)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(7, 12, 19, 225))
        painter.drawEllipse(centre, _SIZE / 2.0 - 1, _SIZE / 2.0 - 1)

        ring = QColor(accent)
        ring.setAlpha(200)
        painter.setPen(QPen(ring, 2.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, _SIZE / 2.0 - 4, _SIZE / 2.0 - 4)

        glow = QRadialGradient(centre, _SIZE / 3.0)
        inner = QColor(accent)
        inner.setAlpha(int(150 + 90 * breath))
        outer = QColor(accent)
        outer.setAlpha(0)
        glow.setColorAt(0.0, inner)
        glow.setColorAt(1.0, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(centre, _SIZE / 3.0, _SIZE / 3.0)

        painter.setBrush(QColor(225, 245, 255, 230))
        painter.drawEllipse(centre, 4.0, 4.0)

        # Muted: a red dot on the rim, as on the HUD's microphone.
        if self._muted:
            painter.setBrush(hud_module._MUTED_COLOUR)
            painter.drawEllipse(QPointF(_SIZE - 9.0, 9.0), 4.0, 4.0)

        painter.end()


class Minimiser(QObject):
    """Hides the HUD and its panels behind a MiniCore, and brings them back.

    skip(widget) names windows that look after themselves -- the beams, which
    hide whenever the HUD or their panel is hidden and come back with them.
    """

    def __init__(self, hud, core, skip=lambda widget: False, windows=None):
        super().__init__(hud)

        self._hud = hud
        self._core = core
        self._skip = skip
        self._windows = windows or QApplication.topLevelWidgets
        self._wanted = {}       # window -> whether it should show when the HUD returns

        core.clicked.connect(self.restore)

    def is_minimised(self):
        return self._hud.minimised

    def _panels(self):
        """Every other window of ours: not the HUD, not the core, not a menu or tooltip."""
        panels = []

        for widget in self._windows():
            if widget is self._hud or widget is self._core or not widget.isWindow():
                continue

            kind = widget.windowType()

            if kind in (Qt.WindowType.Popup, Qt.WindowType.ToolTip, Qt.WindowType.Desktop):
                continue

            if self._skip(widget):
                continue

            panels.append(widget)

        return panels

    def minimise(self):
        if self._hud.minimised:
            return

        # First, so a panel hidden below can tell a minimise from being closed.
        self._hud.minimised = True

        corner = self._hud.frameGeometry().topRight()
        self._core.move(QPoint(corner.x() - _SIZE - _INSET, corner.y() + _INSET))
        self._core.show()

        for panel in self._panels():
            self._wanted[panel] = panel.isVisible()

            if panel.isVisible():
                QWidget.hide(panel)

            self._hold(panel)

        QWidget.hide(self._hud)
        print("[JARVIS] HUD minimised")

    def restore(self):
        if not self._hud.minimised:
            return

        QWidget.show(self._hud)
        self._hud.raise_()
        self._core.hide()

        wanted, self._wanted = self._wanted, {}

        for panel, show in wanted.items():
            self._release(panel)

            if show:
                QWidget.show(panel)
                panel.raise_()

        self._hud.minimised = False

        # Panels docked to the HUD line up with it again.
        self._hud.moved.emit()
        print("[JARVIS] HUD restored")

    def _hold(self, panel):
        """While minimised, the panel's own show() and hide() are remembered instead of done.

        Only Python's calls are caught (a panel's self.show(), a timer's hide);
        Qt's own handling of the window is left alone.
        """
        wanted = self._wanted

        def show():
            wanted[panel] = True

        def hide():
            wanted[panel] = False

        panel.show = show
        panel.hide = hide

    @staticmethod
    def _release(panel):
        for name in ("show", "hide"):
            try:
                delattr(panel, name)
            except AttributeError:
                pass
