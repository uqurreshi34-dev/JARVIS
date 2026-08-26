from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget


_WIDTH = 980
_HEIGHT = 520
_MARGIN = 22
_GAP = 16

_BACKDROP = QColor(8, 14, 21, 242)
_ACCENT = QColor(95, 200, 245)

# Space left for the beam to cross to the HUD.
_BEAM_GAP = 74

_FRAME_MS = 33

# Where the row of cards sits, shared by paintEvent and mousePressEvent
# so what you see and what you can click are always exactly the same
# region — computed once here rather than as separate magic numbers in
# each place, which is how those two can quietly drift apart.
_CARD_TOP = 70
_CARD_HEIGHT = _HEIGHT - 100


class ImageChoicesPanel(QWidget):
    """Three Unsplash results at once: click a card, or say its number."""

    show_choices = pyqtSignal(object)
    hide_choices = pyqtSignal()
    choice_clicked = pyqtSignal(int)

    def __init__(self):
        super().__init__()

        self._choices = []
        self._anchor = None
        self._drag_offset = None
        self._sweep = 0.0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.show_choices.connect(self._on_show)
        self.hide_choices.connect(self._on_hide)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        """Sit alongside another window, joined by the beam."""
        self._anchor = widget

    def _on_show(self, choices):
        """Display up to three candidates, or clear the panel when given
        none."""
        self._choices = list(choices or [])[:3]

        if not self._choices:
            self._on_hide()
            return

        self._position()
        self.show()
        self.raise_()

        self._animate.start(_FRAME_MS)
        self.update()

    def _on_hide(self):
        self._animate.stop()
        self._choices = []
        self.hide()

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
            screen.left() + (screen.width() - _WIDTH) // 2,
            screen.top() + (screen.height() - _HEIGHT) // 2,
        )

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

    def _card_rect(self, index):
        """The rectangle for card 0, 1, or 2 — the one source both
        paintEvent and mousePressEvent draw from."""
        card_width = (_WIDTH - 2 * _MARGIN - 2 * _GAP) / 3
        left = _MARGIN + index * (card_width + _GAP)

        return QRectF(left, _CARD_TOP, card_width, _CARD_HEIGHT)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        point = event.position()

        for index in range(len(self._choices)):
            if self._card_rect(index).contains(point):
                self.choice_clicked.emit(index + 1)
                return

        self._drag_offset = (
            event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def _tint(self, alpha):
        colour = QColor(_ACCENT)
        colour.setAlpha(alpha)
        return colour

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body = QRectF(self.rect()).adjusted(5, 5, -5, -5)

        path = QPainterPath()
        path.addRoundedRect(body, 18, 18)

        painter.fillPath(path, _BACKDROP)
        painter.setPen(QPen(self._tint(95), 1.5))
        painter.drawPath(path)

        painter.setClipPath(path)

        self._paint_heading(painter)
        self._paint_cards(painter)
        self._paint_scanline(painter, body)

        painter.end()

    def _paint_heading(self, painter):
        painter.setPen(QPen(_ACCENT))
        painter.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        painter.drawText(
            _MARGIN, 42, "SELECT IMAGE  \u2022  SAY OR CLICK 1, 2, OR 3"
        )

        painter.setPen(QPen(self._tint(80), 1.0))
        painter.drawLine(_MARGIN, 52, _WIDTH - _MARGIN, 52)

    def _paint_cards(self, painter):
        for index, choice in enumerate(self._choices):
            rect = self._card_rect(index)

            card = QPainterPath()
            card.addRoundedRect(rect, 10, 10)

            painter.fillPath(card, QColor(4, 10, 16, 235))
            painter.setPen(QPen(self._tint(105), 1.2))
            painter.drawPath(card)

            self._paint_thumbnail(painter, rect, choice.get("data", b""))

            painter.setPen(QPen(_ACCENT))
            painter.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
            painter.drawText(
                int(rect.left() + 12), int(rect.bottom() - 18),
                str(index + 1),
            )

            painter.setPen(QPen(self._tint(175)))
            painter.setFont(QFont("Segoe UI", 9))

            title = (choice.get("title") or "Image").replace("\n", " ")
            painter.drawText(
                int(rect.left() + 40), int(rect.bottom() - 18), title[:42]
            )

    @staticmethod
    def _paint_thumbnail(painter, rect, data):
        image = QImage()

        if not image.loadFromData(data):
            return

        target = QRectF(
            rect.left() + 8, rect.top() + 8,
            rect.width() - 16, rect.height() - 58,
        )

        scaled = image.scaled(
            int(target.width()), int(target.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        x = target.left() + (target.width() - scaled.width()) / 2
        y = target.top() + (target.height() - scaled.height()) / 2

        painter.drawImage(
            QRectF(x, y, scaled.width(), scaled.height()), scaled)

    def _paint_scanline(self, painter, body):
        y = body.top() + body.height() * self._sweep

        painter.setPen(QPen(self._tint(22), 1.0))
        painter.drawLine(int(body.left()), int(y), int(body.right()), int(y))
