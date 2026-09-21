"""The page JARVIS projects while he recites.

Built like the other panels -- frameless, translucent, anchored beside
the HUD with the beam crossing the gap -- but with real controls on it
rather than painted text alone. A verse box, an auto-continue tick, a
pause and a stop, and the choice of reciter.

The controls emit signals and do nothing else. Whoever wires this panel
decides what they mean, which keeps the session logic in one place and
out of a widget. The panel's own job is to show the verse that is
sounding, right now, and to make the next one one click away.

It is driven from the recitation thread, so everything that arrives
from outside comes in through a signal. Touching a Qt widget from
another thread is the one thing that reliably crashes a Qt program.
"""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


_WIDTH = 560
_HEIGHT = 470
_MARGIN = 26

_BACKDROP = QColor(10, 12, 16, 236)

# Warmer than the HUD's cyan, matching the RECITING state rather than
# the speaking one. The page should not look like another readout.
_ACCENT = QColor(205, 175, 95)
_ARABIC = QColor(245, 238, 222)
_ENGLISH = QColor(168, 178, 190)
_MUTED = QColor(120, 132, 145)

# Space left between this panel and the HUD, for the beam to cross.
_BEAM_GAP = 74

_FRAME_MS = 33

# Windows ships the first two; the rest are there for anyone who has
# installed a proper naskh face. Qt takes the first that resolves.
_ARABIC_FAMILIES = (
    "Traditional Arabic",
    "Arabic Typesetting",
    "Amiri",
    "Scheherazade New",
    "Segoe UI",
)

_CONTROLS = """
QLineEdit, QComboBox {
    background: rgba(255, 255, 255, 18);
    border: 1px solid rgba(205, 175, 95, 90);
    border-radius: 4px;
    color: #e8e2d2;
    padding: 3px 6px;
    selection-background-color: rgba(205, 175, 95, 120);
}
QComboBox QAbstractItemView {
    background: #12151b;
    color: #e8e2d2;
    selection-background-color: rgba(205, 175, 95, 110);
    border: 1px solid rgba(205, 175, 95, 90);
}
QPushButton {
    background: rgba(205, 175, 95, 34);
    border: 1px solid rgba(205, 175, 95, 110);
    border-radius: 4px;
    color: #f0e9d8;
    padding: 4px 12px;
}
QPushButton:hover { background: rgba(205, 175, 95, 62); }
QPushButton:disabled { color: #6b7077; border-color: rgba(205,175,95,40); }
QCheckBox { color: #c9d2db; }
QCheckBox::indicator {
    width: 13px; height: 13px;
    border: 1px solid rgba(205, 175, 95, 130);
    border-radius: 3px;
    background: rgba(255, 255, 255, 14);
}
QCheckBox::indicator:checked { background: rgba(205, 175, 95, 190); }
"""


class QuranPanel(QWidget):
    """The verse being recited, and the controls for what follows it."""

    # Driven from the recitation thread.
    began = pyqtSignal(dict)
    verse = pyqtSignal(dict, dict)
    ended = pyqtSignal(str)
    message = pyqtSignal(str)
    reciters_loaded = pyqtSignal(list)

    # Raised by the controls. What they mean is decided elsewhere.
    jump_requested = pyqtSignal(int)
    auto_changed = pyqtSignal(bool)
    pause_changed = pyqtSignal(bool)
    stop_requested = pyqtSignal()
    reciter_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        self._sweep = 0.0
        self._drag_offset = None
        self._anchor = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)
        self.setStyleSheet(_CONTROLS)

        self._build()

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

        self.began.connect(self._on_began)
        self.verse.connect(self._on_verse)
        self.ended.connect(self._on_ended)
        self.message.connect(self._on_message)
        self.reciters_loaded.connect(self._on_reciters)

    # ---- construction ---------------------------------------------------

    def _label(self, colour, size, bold=False, arabic=False):
        label = QLabel("", self)
        font = QFont()

        if arabic:
            font.setFamilies(list(_ARABIC_FAMILIES))
        else:
            font.setFamily("Segoe UI")

        font.setPointSize(size)
        font.setBold(bold)

        label.setFont(font)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: rgb({colour.red()},"
                            f"{colour.green()},{colour.blue()});")

        return label

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_MARGIN, _MARGIN - 4, _MARGIN, _MARGIN - 6)
        layout.setSpacing(10)

        self._title = self._label(_ACCENT, 12, bold=True)
        self._counter = self._label(_MUTED, 9)

        head = QHBoxLayout()
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(self._counter)
        layout.addLayout(head)

        self._arabic = self._label(_ARABIC, 24, arabic=True)
        self._arabic.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        self._arabic.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._arabic.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._arabic, 3)

        self._english = self._label(_ENGLISH, 10)
        self._english.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        layout.addWidget(self._english, 2)

        self._message = self._label(QColor(235, 150, 120), 9)
        self._message.hide()
        layout.addWidget(self._message)

        layout.addLayout(self._controls())

    def _controls(self):
        row = QHBoxLayout()
        row.setSpacing(8)

        self._verse_box = QLineEdit(self)
        self._verse_box.setFixedWidth(52)
        self._verse_box.setPlaceholderText("verse")
        self._verse_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._verse_box.returnPressed.connect(self._go)
        row.addWidget(self._verse_box)

        go = QPushButton("Go", self)
        go.clicked.connect(self._go)
        row.addWidget(go)

        self._auto = QCheckBox("Auto", self)
        self._auto.toggled.connect(self.auto_changed.emit)
        row.addWidget(self._auto)

        self._pause = QPushButton("Pause", self)
        self._pause.setCheckable(True)
        self._pause.toggled.connect(self._on_pause)
        row.addWidget(self._pause)

        stop = QPushButton("Stop", self)
        stop.clicked.connect(self.stop_requested.emit)
        row.addWidget(stop)

        row.addStretch(1)

        self._reciter = QComboBox(self)
        self._reciter.setFixedWidth(150)
        self._reciter.addItem("Alafasy", "ar.alafasy")
        self._reciter.currentIndexChanged.connect(self._on_reciter_index)
        row.addWidget(self._reciter)

        return row

    # ---- driven from outside --------------------------------------------

    def _on_began(self, state):
        self._title.setText(
            f"{state.get('name', '')}"
            + (f"  ·  {state['translation']}" if state.get("translation")
               else "")
        )
        self._counter.setText("")
        self._arabic.setText("")
        self._english.setText("")
        self._on_message("")

        self._auto.blockSignals(True)
        self._auto.setChecked(bool(state.get("auto")))
        self._auto.blockSignals(False)

        self._pause.blockSignals(True)
        self._pause.setChecked(False)
        self._pause.setText("Pause")
        self._pause.blockSignals(False)

        self._select_reciter(state.get("reciter"))

        self._position()
        self.show()
        self.raise_()

        if not self._animate.isActive():
            self._animate.start(_FRAME_MS)

    def _on_verse(self, verse, state):
        self._counter.setText(
            f"{state.get('ayah', '?')} of {state.get('total', '?')}"
        )
        self._arabic.setText(verse.get("arabic") or "")
        self._english.setText(verse.get("english") or "")
        self._on_message("")

        if not self._verse_box.hasFocus():
            self._verse_box.setText(str(state.get("ayah", "")))

    def _on_ended(self, reason):
        self._animate.stop()
        self.hide()

    def _on_message(self, text):
        self._message.setText(text or "")
        self._message.setVisible(bool(text))

    def _on_reciters(self, reciters):
        """Fill the dropdown once the list has been fetched elsewhere."""
        if not reciters:
            return

        wanted = self._reciter.currentData()

        self._reciter.blockSignals(True)
        self._reciter.clear()

        for entry in reciters:
            self._reciter.addItem(entry["name"], entry["identifier"])

        self._reciter.blockSignals(False)
        self._select_reciter(wanted)

    def _select_reciter(self, identifier):
        if not identifier:
            return

        index = self._reciter.findData(identifier)

        if index < 0:
            return

        self._reciter.blockSignals(True)
        self._reciter.setCurrentIndex(index)
        self._reciter.blockSignals(False)

    # ---- controls -------------------------------------------------------

    def _go(self):
        text = self._verse_box.text().strip()

        if not text.isdigit():
            self._on_message("A verse number, please.")
            return

        self.jump_requested.emit(int(text))

    def _on_pause(self, paused):
        self._pause.setText("Resume" if paused else "Pause")
        self.pause_changed.emit(paused)

    def _on_reciter_index(self, index):
        identifier = self._reciter.itemData(index)

        if identifier:
            self.reciter_changed.emit(identifier)

    # ---- placement and painting -----------------------------------------

    def set_anchor(self, widget):
        """Sit alongside another window, joined by the beam."""
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

        self.move(
            screen.right() - _WIDTH - 500,
            screen.bottom() - _HEIGHT - 24,
        )

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body = self.rect().adjusted(1, 1, -1, -1)

        path = QPainterPath()
        path.addRoundedRect(float(body.x()), float(body.y()),
                            float(body.width()), float(body.height()), 10, 10)

        painter.fillPath(path, _BACKDROP)

        edge = QColor(_ACCENT)
        edge.setAlpha(110)
        painter.setPen(QPen(edge, 1))
        painter.drawPath(path)

        # A rule under the heading, and a slow highlight travelling along
        # it, so the page reads as lit rather than printed.
        y = _MARGIN + 22
        painter.setPen(QPen(QColor(205, 175, 95, 70), 1))
        painter.drawLine(_MARGIN, y, _WIDTH - _MARGIN, y)

        span = _WIDTH - 2 * _MARGIN
        head = _MARGIN + int(self._sweep * span)

        glow = QColor(_ACCENT)
        glow.setAlpha(150)
        painter.setPen(QPen(glow, 2))
        painter.drawLine(max(_MARGIN, head - 40), y, head, y)

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
