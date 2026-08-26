from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QFont
from PyQt6.QtWidgets import QWidget

_WIDTH = 980
_HEIGHT = 520
_MARGIN = 22
_GAP = 16
_BACKDROP = QColor(8, 14, 21, 242)
_ACCENT = QColor(95, 200, 245)
_BEAM_GAP = 74
_FRAME_MS = 33


class ImageChoicesPanel(QWidget):
    """JARVIS three-image chooser. Click a card or say its number."""

    show_choices = pyqtSignal(object)
    hide_choices = pyqtSignal()
    choice_clicked = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._choices = []
        self._anchor = None
        self._drag_offset = None
        self._sweep = 0.0
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)
        self.show_choices.connect(self._on_show)
        self.hide_choices.connect(self._on_hide)
        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        self._anchor = widget

    def _on_show(self, choices):
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
        else:
            self.move(screen.left() + (screen.width() - _WIDTH) // 2, screen.top() + (screen.height() - _HEIGHT) // 2)

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        y = event.position().y()
        card_width = (_WIDTH - 2 * _MARGIN - 2 * _GAP) / 3
        if 70 <= y <= _HEIGHT - 28:
            for index in range(3):
                left = _MARGIN + index * (card_width + _GAP)
                if left <= x <= left + card_width and index < len(self._choices):
                    self.choice_clicked.emit(index + 1)
                    return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

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
        painter.setPen(QPen(_ACCENT))
        painter.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        painter.drawText(_MARGIN, 42, "SELECT IMAGE  •  SAY OR CLICK 1, 2, OR 3")
        painter.setPen(QPen(self._tint(80), 1))
        painter.drawLine(_MARGIN, 52, _WIDTH - _MARGIN, 52)
        card_width = (_WIDTH - 2 * _MARGIN - 2 * _GAP) / 3
        for index, choice in enumerate(self._choices):
            left = _MARGIN + index * (card_width + _GAP)
            rect = QRectF(left, 70, card_width, _HEIGHT - 100)
            card = QPainterPath()
            card.addRoundedRect(rect, 10, 10)
            painter.fillPath(card, QColor(4, 10, 16, 235))
            painter.setPen(QPen(self._tint(105), 1.2))
            painter.drawPath(card)
            image = QImage()
            if image.loadFromData(choice.get("data", b"")):
                target = QRectF(rect.left() + 8, rect.top() + 8, rect.width() - 16, rect.height() - 58)
                scaled = image.scaled(int(target.width()), int(target.height()), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                x = target.left() + (target.width() - scaled.width()) / 2
                y = target.top() + (target.height() - scaled.height()) / 2
                painter.drawImage(QRectF(x, y, scaled.width(), scaled.height()), scaled)
            painter.setPen(QPen(_ACCENT))
            painter.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
            painter.drawText(int(rect.left() + 12), int(rect.bottom() - 18), str(index + 1))
            painter.setPen(QPen(self._tint(175)))
            painter.setFont(QFont("Segoe UI", 9))
            title = (choice.get("title") or "Image").replace("\n", " ")
            painter.drawText(int(rect.left() + 40), int(rect.bottom() - 18), title[:42])
        y = body.top() + body.height() * self._sweep
        painter.setPen(QPen(self._tint(22), 1))
        painter.drawLine(int(body.left()), int(y), int(body.right()), int(y))
        painter.end()
