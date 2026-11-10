"""The Arbitrage Sandbox: a live view of the ARB scanner's console."""
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
import re
from datetime import datetime
from pathlib import Path

# Changed log file so it doesn't mix with the sandwich bot's logs
_LOG_PATH = Path.home() / "JARVIS" / "arb.log"


_WIDTH = 720
_HEIGHT = 480
_MARGIN = 22

_BACKDROP = QColor(10, 12, 16, 240)
_ACCENT = QColor(180, 120, 255)      # Purple for Arbitrage
_MUTED = QColor(140, 130, 150)
_WARN = QColor(230, 150, 100)

_BEAM_GAP = 74
_FRAME_MS = 33

_STYLE = """
QPlainTextEdit {
    background: rgba(0, 0, 0, 90);
    border: 1px solid rgba(180, 120, 255, 80);
    border-radius: 4px;
    color: #dcd2e8;
    font-family: Consolas, "Courier New", monospace;
    font-size: 9pt;
    padding: 6px;
}
QPushButton {
    background: rgba(180, 120, 255, 34);
    border: 1px solid rgba(180, 120, 255, 110);
    border-radius: 4px;
    color: #f0ecf8;
    padding: 6px 16px;
    font-weight: bold;
}
QPushButton:hover { background: rgba(180, 120, 255, 62); }
QPushButton:disabled { color: #63556a; border-color: rgba(180,120,255,40); }
QPushButton#arm { background: rgba(180, 120, 255, 90); }
QPushButton#disarm {
    background: rgba(230, 100, 100, 70);
    border-color: rgba(230, 100, 100, 140);
}
"""

_PRIVATE_KEY_SHAPE = re.compile(r"^[0-9a-fA-F]{64}$")


def _sanitize(line):
    parts = []
    for token in line.split():
        core = token.strip("[](),;:'\"")
        if _PRIVATE_KEY_SHAPE.match(core):
            parts.append("[REDACTED]")
        else:
            parts.append(token)
    return " ".join(parts)


class ArbPanel(QWidget):
    show_requested = pyqtSignal()
    hide_requested = pyqtSignal()
    line_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    arm_clicked = pyqtSignal()
    disarm_clicked = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._sweep = 0.0
        self._drag_offset = None
        self._anchor = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)
        self.setStyleSheet(_STYLE)

        self._build()
        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

        self.show_requested.connect(self._on_show)
        self.hide_requested.connect(self._on_hide)
        self.line_received.connect(self._on_line)
        self.status_changed.connect(self._on_status)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_MARGIN, _MARGIN - 4, _MARGIN, _MARGIN - 6)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self._title = QLabel("ARB SANDBOX", self)
        self._title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._title.setStyleSheet("color: rgb(180, 120, 255);")
        head.addWidget(self._title)
        head.addStretch(1)

        self._status_label = QLabel("IDLE", self)
        self._status_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self._status_label.setStyleSheet("color: rgb(140, 130, 150);")
        head.addWidget(self._status_label)
        layout.addLayout(head)

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._log, 1)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self._arm_btn = QPushButton("ARM", self)
        self._arm_btn.setObjectName("arm")
        self._arm_btn.clicked.connect(self.arm_clicked.emit)
        controls.addWidget(self._arm_btn)

        self._disarm_btn = QPushButton("DISARM", self)
        self._disarm_btn.setObjectName("disarm")
        self._disarm_btn.clicked.connect(self.disarm_clicked.emit)
        self._disarm_btn.setEnabled(False)
        controls.addWidget(self._disarm_btn)
        controls.addStretch(1)

        close_btn = QPushButton("CLOSE", self)
        close_btn.clicked.connect(self._on_close_clicked)
        controls.addWidget(close_btn)
        layout.addLayout(controls)

        hint = QLabel(
            "ARM starts the arbitrage scanner. Voice cannot start it.", self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: rgb(140, 130, 150); font-size: 8pt;")
        layout.addWidget(hint)

    def _on_show(self):
        self._position()
        self.show()
        self.raise_()
        if not self._animate.isActive():
            self._animate.start(_FRAME_MS)

    def _on_hide(self):
        self._animate.stop()
        self.hide()

    def _on_close_clicked(self):
        self._animate.stop()
        self.hide()
        self.closed.emit()

    def _on_line(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {_sanitize(text)}"
        self._log.appendPlainText(line)
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[arb_panel] could not write log: {e}")

    def _on_status(self, status):
        colours = {"IDLE": _MUTED, "ARMING": _WARN,
                   "ARMED": _ACCENT, "STOPPING": _WARN, "STOPPED": _MUTED}
        colour = colours.get(status, _MUTED)
        self._status_label.setText(status)
        self._status_label.setStyleSheet(
            f"color: rgb({colour.red()},{colour.green()},{colour.blue()});")
        running = status in ("ARMED", "ARMING")
        self._arm_btn.setEnabled(not running)
        self._disarm_btn.setEnabled(running)

    def set_anchor(self, widget):
        self._anchor = widget

    def _position(self):
        screen = self.screen().availableGeometry()
        if self._anchor is not None and self._anchor.isVisible():
            frame = self._anchor.frameGeometry()
            x = frame.left() - _WIDTH - _BEAM_GAP
            y = frame.center().y() - _HEIGHT // 2
            x = max(screen.left() + 12, x)
            y = max(screen.top() + 12, min(y, screen.bottom() - _HEIGHT - 12))
            self.move(int(x), int(y))
            return
        self.move(screen.left() + 24, screen.bottom() - _HEIGHT - 24)

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(float(body.x()), float(body.y()), float(
            body.width()), float(body.height()), 10, 10)
        painter.fillPath(path, _BACKDROP)

        edge = QColor(_ACCENT)
        edge.setAlpha(110)
        painter.setPen(QPen(edge, 1))
        painter.drawPath(path)

        y = _MARGIN + 20
        painter.setPen(QPen(QColor(180, 120, 255, 70), 1))
        painter.drawLine(_MARGIN, y, _WIDTH - _MARGIN, y)

        span = _WIDTH - 2 * _MARGIN
        head = _MARGIN + int(self._sweep * span)
        glow = QColor(_ACCENT)
        glow.setAlpha(150)
        painter.setPen(QPen(glow, 2))
        painter.drawLine(max(_MARGIN, head - 40), y, head, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - \
                self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
