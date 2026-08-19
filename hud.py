import math

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget


IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"

_PALETTE = {
    IDLE: QColor(90, 130, 165),
    LISTENING: QColor(80, 210, 255),
    THINKING: QColor(255, 185, 70),
    SPEAKING: QColor(90, 255, 190),
}

_LABEL = {
    IDLE: "STANDBY",
    LISTENING: "LISTENING",
    THINKING: "PROCESSING",
    SPEAKING: "SPEAKING",
}

_WIDTH = 380
_HEIGHT = 210
_MARGIN = 26
_RING_CENTRE = (70, 105)
_RING_RADIUS = 34

# Slow faint breathing when JARVIS is not speaking.
_BREATH_SPEED = 0.022
_BREATH_DEPTH = 0.22

# How quickly the ring follows the voice envelope. Higher rises faster.
_ATTACK = 0.55
_RELEASE = 0.16


class Hud(QWidget):
    """Frameless always-on-top JARVIS status overlay."""

    state_changed = pyqtSignal(str)
    heard_changed = pyqtSignal(str)
    reply_changed = pyqtSignal(str)
    amplitude_changed = pyqtSignal(float)
    shutdown = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()

        self._state = IDLE
        self._heard = ""
        self._reply = ""
        self._phase = 0.0
        self._drag_offset = None

        # Target amplitude from the voice, and the smoothed value we draw.
        self._target = 0.0
        self._level = 0.0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.state_changed.connect(self._on_state)
        self.heard_changed.connect(self._on_heard)
        self.reply_changed.connect(self._on_reply)
        self.amplitude_changed.connect(self._on_amplitude)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._position()

    def _position(self):
        screen = self.screen().availableGeometry()

        self.move(
            screen.right() - _WIDTH - 24,
            screen.bottom() - _HEIGHT - 24,
        )

    def _on_state(self, state):
        self._state = state if state in _PALETTE else IDLE

        if self._state != SPEAKING:
            self._target = 0.0

        self.update()

    def _on_heard(self, text):
        self._heard = text
        self.update()

    def _on_reply(self, text):
        self._reply = text
        self.update()

    def _on_amplitude(self, value):
        self._target = max(0.0, min(1.0, value))

    def _tick(self):
        # Ease the drawn level toward the target so the ring never jumps.
        rate = _ATTACK if self._target > self._level else _RELEASE
        self._level += (self._target - self._level) * rate

        self._phase = (self._phase + _BREATH_SPEED) % (2 * math.pi)

        self.update()

    def _energy(self):
        """0..1 drive for the ring: voice envelope, or breathing when quiet."""
        breath = _BREATH_DEPTH * (0.5 + 0.5 * math.sin(self._phase))

        if self._state == SPEAKING:
            return max(breath * 0.4, self._level)

        return breath

    # Let the user drag the HUD anywhere on screen.
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        accent = _PALETTE[self._state]
        energy = self._energy()

        self._paint_panel(painter, accent)
        self._paint_reactor(painter, accent, energy)
        self._paint_text(painter, accent)

        painter.end()

    def _paint_panel(self, painter, accent):
        body = self.rect().adjusted(6, 6, -6, -6)

        path = QPainterPath()
        path.addRoundedRect(float(body.x()), float(body.y()),
                            float(body.width()), float(body.height()), 18, 18)

        painter.fillPath(path, QColor(10, 16, 24, 214))

        glow = QColor(accent)
        glow.setAlpha(90)
        painter.setPen(QPen(glow, 1.6))
        painter.drawPath(path)

    def _paint_reactor(self, painter, accent, energy):
        cx, cy = _RING_CENTRE

        halo_radius = _RING_RADIUS * (1.55 + 0.75 * energy)

        halo = QRadialGradient(cx, cy, halo_radius)
        centre_glow = QColor(accent)
        centre_glow.setAlpha(int(55 + 120 * energy))
        halo.setColorAt(0.0, centre_glow)
        halo.setColorAt(1.0, QColor(
            accent.red(), accent.green(), accent.blue(), 0))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(
            int(cx - halo_radius), int(cy - halo_radius),
            int(halo_radius * 2), int(halo_radius * 2),
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)

        track = QColor(accent)
        track.setAlpha(60)
        painter.setPen(QPen(track, 2.0))
        painter.drawEllipse(
            cx - _RING_RADIUS, cy - _RING_RADIUS,
            _RING_RADIUS * 2, _RING_RADIUS * 2,
        )

        # Outer ring expands with the voice.
        pulse_radius = _RING_RADIUS + int(10 * energy)
        pulse = QColor(accent)
        pulse.setAlpha(int(40 + 150 * energy))
        painter.setPen(QPen(pulse, 2.0 + 1.5 * energy))
        painter.drawEllipse(
            cx - pulse_radius, cy - pulse_radius,
            pulse_radius * 2, pulse_radius * 2,
        )

        # Rotating arc keeps a sense of life while thinking/listening.
        span = 90 if self._state in (IDLE, SPEAKING) else 150
        start = int(-self._phase * 180 / math.pi * 6) % 360

        arc = QColor(accent)
        arc.setAlpha(235)
        painter.setPen(
            QPen(arc, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(
            cx - _RING_RADIUS, cy - _RING_RADIUS,
            _RING_RADIUS * 2, _RING_RADIUS * 2,
            start * 16, span * 16,
        )

        # Core swells with the voice.
        inner = QColor(accent)
        inner.setAlpha(int(110 + 130 * energy))
        radius = int(8 + 9 * energy)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(inner)
        painter.drawEllipse(cx - radius, cy - radius, radius * 2, radius * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_text(self, painter, accent):
        left = _RING_CENTRE[0] + _RING_RADIUS + 26
        width = _WIDTH - left - _MARGIN

        painter.setPen(QPen(accent))
        font = QFont("Segoe UI", 11, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        painter.setFont(font)
        painter.drawText(left, 56, _LABEL[self._state])

        painter.setPen(QPen(QColor(236, 244, 252, 235)))
        painter.setFont(QFont("Segoe UI", 10))
        self._draw_wrapped(painter, self._heard or "—", left, 84, width, 2)

        painter.setPen(QPen(QColor(150, 170, 190, 205)))
        painter.setFont(QFont("Segoe UI", 9))
        self._draw_wrapped(painter, self._reply, left, 138, width, 2)

    @staticmethod
    def _draw_wrapped(painter, text, x, y, width, max_lines):
        if not text:
            return

        metrics = QFontMetrics(painter.font())
        spacing = metrics.height() + 2

        words = text.split()
        lines = []
        current = ""

        for word in words:
            trial = f"{current} {word}".strip()

            if metrics.horizontalAdvance(trial) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word

                if len(lines) == max_lines:
                    break

        if current and len(lines) < max_lines:
            lines.append(current)

        for index, line in enumerate(lines[:max_lines]):
            if index == max_lines - 1 and len(lines) > max_lines - 1:
                line = metrics.elidedText(
                    line, Qt.TextElideMode.ElideRight, width)

            painter.drawText(x, y + index * spacing, line)
