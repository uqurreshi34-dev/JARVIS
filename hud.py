import math
import time

import psutil
import re
from collections import deque

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
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

# Distinct from SPEAKING because it is not JARVIS talking. The waveform
# behaves the same -- it is still his output driving the strip rather
# than the microphone -- but the label and the colour should not claim
# the recitation as his own voice.
RECITING = "reciting"

# Anything that is JARVIS's own audio rather than the room's.
_HIS_AUDIO = (SPEAKING, RECITING)

_PALETTE = {
    IDLE: QColor(95, 165, 205),
    LISTENING: QColor(80, 215, 255),
    THINKING: QColor(255, 180, 65),
    SPEAKING: QColor(95, 255, 195),
    RECITING: QColor(205, 175, 95),
}

_LABEL = {
    IDLE: "STANDBY",
    LISTENING: "LISTENING",
    THINKING: "PROCESSING",
    SPEAKING: "SPEAKING",
    RECITING: "RECITING",
}

_WIDTH = 460
_HEIGHT = 292

_CENTRE = QPointF(132.0, 135.0)

# The stop button: inside the top-left corner bracket, clear of the outer
# ring. The click area is a little larger than what is drawn.
_STOP_RECT = QRectF(22.0, 22.0, 26.0, 26.0)
_STOP_HIT = _STOP_RECT.adjusted(-6.0, -6.0, 6.0, 6.0)
_R_OUTER = 108.0
_R_TICKS = 96.0
_R_RING1 = 84.0
_R_RING2 = 68.0
_R_RING3 = 52.0
_R_CORE = 30.0

_PANEL_X = 258
_PANEL_RIGHT = _WIDTH - 26

# Vertical layout of the right-hand column. Defined together so the reply can
# be clamped to exactly the room above the telemetry.
_REPLY_TOP = 122
_TELEMETRY_TOP = _HEIGHT - 84

# Gap kept between the last line of the reply and the telemetry.
_TEXT_GAP = 10

# Slow faint breathing when JARVIS is not speaking.
_BREATH_SPEED = 0.022
_BREATH_DEPTH = 0.22

# How quickly the core follows the voice envelope.
_ATTACK = 0.55
_RELEASE = 0.16

_TELEMETRY_MS = 1500

# Points across the waveform strip, and its height in pixels.
_WAVE_POINTS = 56
_WAVE_HEIGHT = 13

# Frames without a fresh reading before the held level starts decaying.
_LEVEL_STALE_FRAMES = 3
_LEVEL_DECAY = 0.72

# The countdown for a held question ("Shall I go ahead?") is drawn on the
# outer ring: amber while there is time, red for the last stretch, a green
# flash for yes and a grey fade for anything else.
_CONFIRM_COLOUR = QColor(255, 196, 80)
_CONFIRM_URGENT = QColor(255, 88, 88)
_CONFIRM_YES = QColor(95, 255, 160)
_CONFIRM_ENDED = QColor(150, 165, 180)
_CONFIRM_URGENT_SECONDS = 15.0
_CONFIRM_FLASH_SECONDS = 0.8

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text):
    """Split spoken reply text into display-sized sentence units."""
    text = " ".join((text or "").split()).strip()

    if not text:
        return []

    return [
        sentence.strip()
        for sentence in _SENTENCE_END.split(text)
        if sentence.strip()
    ]


class Hud(QWidget):
    """Frameless always-on-top JARVIS overlay in an Iron Man style."""

    state_changed = pyqtSignal(str)
    heard_changed = pyqtSignal(str)
    reply_sentence_changed = pyqtSignal(int)
    reply_changed = pyqtSignal(str)
    documents_changed = pyqtSignal(int)
    amplitude_changed = pyqtSignal(float)
    level_changed = pyqtSignal(float)
    files_dropped = pyqtSignal(list)
    shutdown = pyqtSignal()
    closed = pyqtSignal()
    core_clicked = pyqtSignal()
    speaking_changed = pyqtSignal(bool)
    stop_clicked = pyqtSignal()

    # (seconds, "") when a held question is asked; (0, outcome) when it ends.
    confirmation_changed = pyqtSignal(float, str)

    def __init__(self):
        super().__init__()

        self._state = IDLE
        self._heard = ""
        self._reply = ""
        self._reply_sentences = []
        self._reply_index = 0
        self._documents_count = 0
        self._phase = 0.0
        self._sweep = 0.0
        self._drag_offset = None
        self._press_pos = None
        self._press_on_stop = False
        self._drag_hover = False
        self._speaking = False

        self._confirm_until = None      # monotonic deadline of a held question
        self._confirm_total = 0.0
        self._confirm_ended = None      # (outcome, when, fraction left)

        self._target = 0.0
        self._level = 0.0

        # Recent input levels, oldest first, drawn as a live waveform. The
        # strip scrolls on the render clock rather than on audio events, so
        # it never stalls when one source stops feeding it.
        self._wave = deque([0.0] * _WAVE_POINTS, maxlen=_WAVE_POINTS)
        self._current_level = 0.0
        self._level_age = 0

        self._cpu = 0.0
        self._ram = 0.0
        self._battery = None
        self._charging = False

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
        self.reply_sentence_changed.connect(self._on_reply_sentence)
        self.documents_changed.connect(self._on_documents)
        self.amplitude_changed.connect(self._on_amplitude)
        self.level_changed.connect(self._on_level)
        self.speaking_changed.connect(self._on_speaking)
        self.confirmation_changed.connect(self._on_confirmation)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        # Telemetry is polled slowly; reading it every frame would be wasteful.
        self._telemetry = QTimer(self)
        self._telemetry.timeout.connect(self._poll_telemetry)
        self._telemetry.start(_TELEMETRY_MS)
        self._poll_telemetry()

        self._position()

        # drag drop documents
        self.setAcceptDrops(True)

    def _position(self):
        screen = self.screen().availableGeometry()

        self.move(
            screen.right() - _WIDTH - 24,
            screen.bottom() - _HEIGHT - 24,
        )

    def _poll_telemetry(self):
        try:
            self._cpu = psutil.cpu_percent(interval=None)
            self._ram = psutil.virtual_memory().percent

            battery = psutil.sensors_battery()

            if battery is not None:
                self._battery = battery.percent
                self._charging = bool(battery.power_plugged)
            else:
                self._battery = None

        except Exception:
            pass

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._drag_hover = True
            self.update()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

            if not self._drag_hover:
                self._drag_hover = True

            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drag_hover = False
        self.update()

    def dropEvent(self, event):
        paths = []

        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())

        self._drag_hover = False
        self.update()

        if paths:
            self.files_dropped.emit(paths)

        event.acceptProposedAction()

    def _on_speaking(self, active):
        self._speaking = bool(active)
        self.update()

    def _on_confirmation(self, seconds, outcome):
        now = time.monotonic()

        if seconds > 0:
            self._confirm_until = now + seconds
            self._confirm_total = seconds
            self._confirm_ended = None
        elif self._confirm_until is not None:
            left = max(0.0, (self._confirm_until - now) / self._confirm_total)
            self._confirm_ended = (outcome or "dropped", now, left)
            self._confirm_until = None

        self.update()

    def _on_state(self, state):
        self._state = state if state in _PALETTE else IDLE

        if self._state not in _HIS_AUDIO:
            self._target = 0.0

        self.update()

    def _on_heard(self, text):
        self._heard = text
        self.update()

    def _on_reply(self, text):
        self._reply = text or ""
        self._reply_sentences = _split_sentences(self._reply)
        self._reply_index = 0
        self.update()

    def _on_reply_sentence(self, index):
        if not self._reply_sentences:
            return

        self._reply_index = max(
            0,
            min(int(index), len(self._reply_sentences) - 1),
        )
        self.update()

    def _on_documents(self, count):
        self._documents_count = max(0, min(8, int(count)))
        self.update()

    def _on_amplitude(self, value):
        self._target = max(0.0, min(1.0, value))

        # While JARVIS talks, the waveform shows his voice rather than the
        # microphone, so the strip is never dead.
        if self._state in _HIS_AUDIO:
            self._current_level = max(0.0, min(1.0, value))
            self._level_age = 0

    def _on_level(self, value):
        """Microphone level, ignored while JARVIS's own audio plays."""
        if self._state in _HIS_AUDIO:
            return

        self._current_level = max(0.0, min(1.0, value))
        self._level_age = 0

    def _tick(self):
        rate = _ATTACK if self._target > self._level else _RELEASE
        self._level += (self._target - self._level) * rate

        self._phase = (self._phase + _BREATH_SPEED) % (2 * math.pi)
        self._sweep = (self._sweep + 0.006) % 1.0

        # Scroll the waveform every frame. If no fresh level has arrived the
        # held value decays, so the strip glides to flat instead of stalling.
        self._level_age += 1

        if self._level_age > _LEVEL_STALE_FRAMES:
            self._current_level *= _LEVEL_DECAY

        self._wave.append(self._current_level)

        self.update()

    def _energy(self):
        """0..1 drive for the core: voice envelope, or breathing when quiet."""
        breath = _BREATH_DEPTH * (0.5 + 0.5 * math.sin(self._phase))

        if self._state in _HIS_AUDIO:
            return max(breath * 0.4, self._level)

        return breath

    def _spin(self):
        """Degrees of rotation, faster while busy."""
        speed = 26.0 if self._state in (THINKING, LISTENING) else 9.0

        return self._phase * speed * 180.0 / math.pi

    # Let the user drag the HUD anywhere on screen. A press on the core
    # that releases without much movement is a click instead — that's what
    # toggles the mind view — so this has to tell the two apart rather than
    # treating every press as the start of a drag.
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position()

            # Whether the stop button was showing is decided at the press.
            # "Speaking" reaches the HUD as a queued signal from the speech
            # thread; with the mind view animating, the queue is busier, and
            # a press made while he spoke could be released after the HUD
            # had caught up with a pause, and be ignored.
            self._press_on_stop = self._speaking and _STOP_HIT.contains(self._press_pos)
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._press_pos is not None
        ):
            moved = (event.position() - self._press_pos).manhattanLength()

            if moved < 6 and self._press_on_stop:
                self.stop_clicked.emit()

            elif moved < 6 and self._distance_from_centre(self._press_pos) <= _R_RING3:
                self.core_clicked.emit()

        self._drag_offset = None
        self._press_pos = None
        self._press_on_stop = False

    @staticmethod
    def _distance_from_centre(pos):
        return math.hypot(pos.x() - _CENTRE.x(), pos.y() - _CENTRE.y())

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        accent = _PALETTE[self._state]
        energy = self._energy()

        self._paint_panel(painter, accent)
        self._paint_ticks(painter, accent)
        self._paint_rings(painter, accent, energy)
        self._paint_countdown(painter)
        self._paint_reticle(painter, accent)
        self._paint_core(painter, accent, energy)
        self._paint_text(painter, accent)
        self._paint_telemetry(painter, accent)
        self._paint_scanline(painter, accent)
        self._paint_stop(painter)
        self._paint_drop_hint(painter, accent)

        painter.end()

    def _tint(self, accent, alpha):
        colour = QColor(accent)
        colour.setAlpha(alpha)
        return colour

    def _paint_stop(self, painter):
        """The stop button, top left, drawn only while JARVIS is speaking."""
        if not self._speaking:
            return

        painter.save()

        red = QColor(255, 88, 88)

        painter.setPen(QPen(red, 1.6))
        painter.setBrush(QColor(255, 88, 88, 40))
        painter.drawRoundedRect(_STOP_RECT, 6, 6)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(red)
        painter.drawRect(_STOP_RECT.adjusted(8, 8, -8, -8))

        painter.restore()

    def _paint_countdown(self, painter):
        """Time left to answer a held question, draining round the outer ring."""
        now = time.monotonic()

        # Run out with nothing said: shown as lapsed here, whatever arrives.
        if self._confirm_until is not None and now >= self._confirm_until:
            self._confirm_ended = ("lapsed", now, 0.0)
            self._confirm_until = None

        if self._confirm_until is not None:
            remaining = self._confirm_until - now
            fraction = remaining / self._confirm_total

            if remaining <= _CONFIRM_URGENT_SECONDS:
                # Red, pulsing a little faster as the end nears.
                colour = QColor(_CONFIRM_URGENT)
                colour.setAlpha(int(170 + 85 * (0.5 + 0.5 * math.sin(now * 8.0))))
            else:
                colour = QColor(_CONFIRM_COLOUR)

            self._arc(painter, _R_OUTER, 0, 360, self._tint(colour, 40), 3.0)
            # From the top, draining anticlockwise to nothing.
            self._arc(painter, _R_OUTER, 90, 360 * fraction, colour, 3.2)

            minutes, seconds = divmod(int(math.ceil(remaining)), 60)
            text = f"CONFIRM {minutes}:{seconds:02d}"

            font = QFont("Consolas", 8, QFont.Weight.Bold)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
            painter.setFont(font)
            painter.setPen(QPen(colour))

            width = QFontMetrics(font).horizontalAdvance(text)
            painter.drawText(int(_PANEL_X - 14 - width), _HEIGHT - 22, text)
            return

        if self._confirm_ended is None:
            return

        outcome, when, left = self._confirm_ended
        age = (now - when) / _CONFIRM_FLASH_SECONDS

        if age >= 1.0:
            self._confirm_ended = None
            return

        fade = 1.0 - age

        if outcome == "yes":
            # The whole ring lights green and fades: agreed.
            self._arc(painter, _R_OUTER, 0, 360, self._tint(_CONFIRM_YES, int(235 * fade)), 3.2 + 2.0 * fade)
        else:
            # What was left greys out and fades: nothing was done.
            self._arc(painter, _R_OUTER, 90, 360 * max(left, 0.08),
                      self._tint(_CONFIRM_ENDED, int(200 * fade)), 3.2)

    def _paint_drop_hint(self, painter, accent):
        """Glow around the reactor while a file is dragged over JARVIS."""
        if not self._drag_hover:
            return

        painter.save()

        radius = _R_OUTER + 5.0

        # Soft outer glow.
        glow = QRadialGradient(_CENTRE, radius + 18.0)
        glow.setColorAt(0.0, self._tint(accent, 0))
        glow.setColorAt(0.72, self._tint(accent, 0))
        glow.setColorAt(0.90, self._tint(accent, 55))
        glow.setColorAt(1.0, self._tint(accent, 0))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(
            _CENTRE,
            radius + 18.0,
            radius + 18.0,
        )

        # Bright drop-target ring.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                self._tint(accent, 235),
                3.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawEllipse(
            _CENTRE,
            radius,
            radius,
        )

        painter.restore()

    def _paint_panel(self, painter, accent):
        body = QRectF(self.rect().adjusted(5, 5, -5, -5))

        path = QPainterPath()
        path.addRoundedRect(body, 20, 20)

        painter.fillPath(path, QColor(7, 12, 19, 222))

        painter.setPen(QPen(self._tint(accent, 95), 1.5))
        painter.drawPath(path)

        # Corner brackets, for that instrument-panel look.
        painter.setPen(QPen(self._tint(accent, 150), 2.0))

        span = 22
        for x, y, dx, dy in (
            (body.left() + 12, body.top() + 12, 1, 1),
            (body.right() - 12, body.top() + 12, -1, 1),
            (body.left() + 12, body.bottom() - 12, 1, -1),
            (body.right() - 12, body.bottom() - 12, -1, -1),
        ):
            painter.drawLine(int(x), int(y), int(x + span * dx), int(y))
            painter.drawLine(int(x), int(y), int(x), int(y + span * dy))

    def _paint_ticks(self, painter, accent):
        painter.save()
        painter.translate(_CENTRE)

        for index in range(72):
            angle = index * 5.0
            major = index % 6 == 0

            length = 10.0 if major else 5.0
            alpha = 175 if major else 85

            painter.save()
            painter.rotate(angle)
            painter.setPen(
                QPen(self._tint(accent, alpha), 1.6 if major else 1.0))
            painter.drawLine(
                QPointF(_R_TICKS, 0.0),
                QPointF(_R_TICKS + length, 0.0),
            )
            painter.restore()

        painter.restore()

    def _arc(self, painter, radius, start_deg, span_deg, colour, width):
        rect = QRectF(
            _CENTRE.x() - radius,
            _CENTRE.y() - radius,
            radius * 2,
            radius * 2,
        )

        painter.setPen(
            QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
        )
        painter.drawArc(rect, int(start_deg * 16), int(span_deg * 16))

    def _paint_rings(self, painter, accent, energy):
        spin = self._spin()

        # Faint full circles as structure.
        for radius in (_R_OUTER, _R_RING2):
            self._arc(painter, radius, 0, 360, self._tint(accent, 45), 1.2)

        # Ring 1: three long segments, clockwise.
        for offset in (0, 120, 240):
            self._arc(
                painter, _R_RING1,
                -spin + offset, 84,
                self._tint(accent, 205), 2.6,
            )

        # Ring 2: many short dashes, counter-rotating.
        for index in range(12):
            self._arc(
                painter, _R_RING2,
                spin * 0.7 + index * 30, 14,
                self._tint(accent, 130), 2.0,
            )

        # Ring 3: two thick sweeps that widen with the voice.
        width = 3.0 + 3.0 * energy

        for offset in (0, 180):
            self._arc(
                painter, _R_RING3,
                -spin * 1.6 + offset, 60,
                self._tint(accent, int(150 + 90 * energy)), width,
            )

    def _paint_reticle(self, painter, accent):
        painter.save()
        painter.translate(_CENTRE)
        painter.setPen(QPen(self._tint(accent, 110), 1.2))

        inner = _R_CORE + 12
        outer = _R_RING3 - 8

        for angle in (0, 90, 180, 270):
            painter.save()
            painter.rotate(angle)
            painter.drawLine(QPointF(inner, 0.0), QPointF(outer, 0.0))
            painter.restore()

        painter.restore()

    def _paint_core(self, painter, accent, energy):
        radius = _R_CORE * (0.62 + 0.38 * energy)

        halo_radius = _R_CORE * (1.7 + 1.1 * energy)
        halo = QRadialGradient(_CENTRE, halo_radius)
        halo.setColorAt(0.0, self._tint(accent, int(120 + 110 * energy)))
        halo.setColorAt(1.0, self._tint(accent, 0))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(_CENTRE, halo_radius, halo_radius)

        painter.setBrush(self._tint(accent, int(150 + 105 * energy)))
        painter.drawEllipse(_CENTRE, radius, radius)

        painter.setBrush(QColor(240, 252, 255, int(120 + 120 * energy)))
        painter.drawEllipse(_CENTRE, radius * 0.42, radius * 0.42)

        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Triangular vanes, echoing the reactor core.
        painter.save()
        painter.translate(_CENTRE)
        painter.rotate(self._spin() * 0.5)
        painter.setPen(QPen(self._tint(accent, 165), 1.4))

        for angle in (0, 120, 240):
            painter.save()
            painter.rotate(angle)
            path = QPainterPath()
            path.moveTo(0.0, -_R_CORE * 0.78)
            path.lineTo(_R_CORE * 0.62, _R_CORE * 0.42)
            path.lineTo(-_R_CORE * 0.62, _R_CORE * 0.42)
            path.closeSubpath()
            painter.drawPath(path)
            painter.restore()

        painter.restore()

    def _paint_text(self, painter, accent):
        left = _PANEL_X
        width = _PANEL_RIGHT - _PANEL_X

        painter.setPen(QPen(accent))
        font = QFont("Consolas", 11, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.2)
        painter.setFont(font)
        painter.drawText(left, 48, _LABEL[self._state])

        # Sit the meter to the right of the label, on the same line.
        label_width = QFontMetrics(font).horizontalAdvance(_LABEL[self._state])
        wave_x = left + label_width + 16
        wave_width = _PANEL_RIGHT - wave_x

        if wave_width > 40:
            self._paint_wave(painter, accent, wave_x, 44, wave_width)

        painter.setPen(QPen(self._tint(accent, 70), 1.0))
        painter.drawLine(left, 60, _PANEL_RIGHT, 60)

        painter.setPen(QPen(QColor(232, 243, 252, 240)))
        painter.setFont(QFont("Segoe UI", 10))
        self._draw_wrapped(painter, self._heard or "—", left, 80, width, 2)

        # Shrink long replies, then clamp to the room left above the
        # telemetry. Text must never run into it, however long the reply.
        painter.setPen(QPen(self._tint(accent, 215)))

        if self._reply_sentences:
            reply = " ".join(
                self._reply_sentences[self._reply_index:]
            )
        else:
            reply = self._reply or ""

        if len(reply) > 210:
            size = 7
        elif len(reply) > 130:
            size = 8
        else:
            size = 9

        painter.setFont(QFont("Segoe UI", size))

        spacing = QFontMetrics(painter.font()).height() + 2
        available = _TELEMETRY_TOP - _TEXT_GAP - _REPLY_TOP
        lines = max(1, available // spacing)

        self._draw_wrapped(painter, reply, left, _REPLY_TOP, width, lines)

        # Document working-set indicator. Hidden completely when empty.
        if self._documents_count > 0:
            painter.setPen(QPen(self._tint(accent, 185)))
            font = QFont("Consolas", 8, QFont.Weight.Bold)
            font.setLetterSpacing(
                QFont.SpacingType.AbsoluteSpacing, 1.4
            )
            painter.setFont(font)

            painter.drawText(
                28,
                _HEIGHT - 22,
                f"DOCUMENTS {self._documents_count} / 8",
            )

    def _paint_wave(self, painter, accent, x, y, width):
        """A live level meter: microphone when listening, voice when speaking."""
        points = list(self._wave)

        if not points:
            return

        step = width / len(points)

        for index, level in enumerate(points):
            if level <= 0.02:
                continue

            # Newer readings sit to the right and are drawn brighter.
            fade = 90 + int(150 * (index / len(points)))
            painter.setPen(QPen(self._tint(accent, fade), 1.6))

            height = level * _WAVE_HEIGHT
            column = x + index * step

            painter.drawLine(
                QPointF(column, y - height),
                QPointF(column, y + height),
            )

    def _paint_telemetry(self, painter, accent):
        rows = [
            ("CPU", self._cpu),
            ("MEM", self._ram),
        ]

        if self._battery is not None:
            label = "PWR" if self._charging else "BAT"
            rows.append((label, self._battery))

        # The corner brackets occupy roughly 22px in from each corner, so the
        # last row and its percentage have to finish above and left of them.
        y = _TELEMETRY_TOP
        width = _PANEL_RIGHT - _PANEL_X - 92

        painter.setFont(QFont("Consolas", 8))

        for label, value in rows:
            value = max(0.0, min(100.0, float(value)))

            painter.setPen(QPen(self._tint(accent, 190)))
            painter.drawText(_PANEL_X, y + 4, label)

            track = QRectF(_PANEL_X + 30, y - 4, width, 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._tint(accent, 45))
            painter.drawRect(track)

            filled = QRectF(track)
            filled.setWidth(track.width() * value / 100.0)
            painter.setBrush(self._tint(accent, 205))
            painter.drawRect(filled)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            painter.setPen(QPen(self._tint(accent, 205)))
            painter.drawText(
                int(track.right() + 8), y + 4, f"{round(value):3d}%"
            )

            y += 16

    def _paint_scanline(self, painter, accent):
        body = QRectF(self.rect().adjusted(6, 6, -6, -6))

        y = body.top() + body.height() * self._sweep

        clip = QPainterPath()
        clip.addRoundedRect(body, 20, 20)

        painter.save()
        painter.setClipPath(clip)
        painter.setPen(QPen(self._tint(accent, 30), 1.0))
        painter.drawLine(
            QPointF(body.left(), y), QPointF(body.right(), y)
        )
        painter.restore()

    @staticmethod
    def _draw_wrapped(painter, text, x, y, width, max_lines):
        if not text:
            return

        metrics = QFontMetrics(painter.font())
        spacing = metrics.height() + 2

        words = text.split()
        lines = []
        current = ""
        remaining = []

        for position, word in enumerate(words):
            trial = f"{current} {word}".strip()

            if metrics.horizontalAdvance(trial) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word

                if len(lines) == max_lines:
                    remaining = words[position:]
                    break

        if current and len(lines) < max_lines:
            lines.append(current)

        shown = lines[:max_lines]
        truncated = len(lines) > max_lines or bool(remaining)

        for index, line in enumerate(shown):
            if index == len(shown) - 1 and truncated:
                # Mark the cut so a clipped reply is obvious.
                line = metrics.elidedText(
                    f"{line}\u2026", Qt.TextElideMode.ElideRight, width
                )

            painter.drawText(x, y + index * spacing, line)
