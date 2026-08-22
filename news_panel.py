from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QImage,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QWidget


_WIDTH = 520
_HEIGHT = 470
_MARGIN = 26

_BACKDROP = QColor(8, 14, 21, 232)
_ACCENT = QColor(95, 200, 245)

# The market strip along the bottom.
_MARKET_TOP = _HEIGHT - 26
_ITEMS_BOTTOM = _MARKET_TOP - 26

# A headline picture, when one is showing, takes the lower part of the panel.
_IMAGE_HEIGHT = 150
_IMAGE_TOP = _MARKET_TOP - 34 - _IMAGE_HEIGHT

# Space left between this panel and the HUD, for the beam to cross.
_BEAM_GAP = 74

_UP = QColor(95, 235, 160)
_DOWN = QColor(255, 110, 110)
_FLAT = QColor(170, 190, 205)

# Headlines fade in one after another, which reads as the panel filling up.
_REVEAL_MS = 90
_FRAME_MS = 33


class NewsPanel(QWidget):
    """A translucent panel listing headlines, in the HUD's style."""

    show_news = pyqtSignal(str, list)
    hide_news = pyqtSignal()
    highlight = pyqtSignal(int)
    markets = pyqtSignal(list)
    picture = pyqtSignal(bytes, str)

    def __init__(self):
        super().__init__()

        self._region = ""
        self._items = []
        self._revealed = 0
        self._sweep = 0.0
        self._highlight = -1
        self._market_rows = []
        self._picture = None
        self._caption = ""
        self._drag_offset = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.show_news.connect(self._on_show)
        self.hide_news.connect(self._on_hide)
        self.highlight.connect(self._on_highlight)
        self.markets.connect(self._on_markets)
        self.picture.connect(self._on_picture)

        self._reveal = QTimer(self)
        self._reveal.timeout.connect(self._advance)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def _on_show(self, region, items):
        """Fill the panel, whether or not it is already open."""
        self._region = region or ""
        self._items = items or []
        self._revealed = 0
        self._highlight = -1
        self._picture = None
        self._caption = ""

        self._position()
        self.show()
        self.raise_()

        self._reveal.start(_REVEAL_MS)
        self._animate.start(_FRAME_MS)

        self.update()

    def _on_picture(self, data, caption):
        """Show a headline picture, or clear it when given nothing."""
        if not data:
            self._picture = None
            self._caption = ""
            self.update()
            return

        image = QImage()

        if not image.loadFromData(data):
            print("[JARVIS] could not decode the headline picture")
            self._picture = None
        else:
            self._picture = image
            self._caption = caption or ""

        self.update()

    def _on_markets(self, rows):
        """Latest prices, as (label, text, percent change)."""
        self._market_rows = list(rows or [])
        self.update()

    def _on_highlight(self, index):
        """Emphasise the story JARVIS is reading out."""
        self._highlight = index
        self.update()

    def _on_hide(self):
        self._reveal.stop()
        self._animate.stop()
        self.hide()

    def set_anchor(self, widget):
        """Sit alongside another window, joined by the beam."""
        self._anchor = widget

    def _position(self):
        screen = self.screen().availableGeometry()

        anchor = getattr(self, "_anchor", None)

        if anchor is not None and anchor.isVisible():
            frame = anchor.frameGeometry()

            # To the left of the HUD, with a gap for the beam, and centred
            # on it so the two read as one instrument.
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

    def _advance(self):
        if self._revealed >= len(self._items):
            self._reveal.stop()
            return

        self._revealed += 1
        self.update()

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

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

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body = QRectF(self.rect()).adjusted(5, 5, -5, -5)

        path = QPainterPath()
        path.addRoundedRect(body, 18, 18)

        painter.fillPath(path, _BACKDROP)

        glow = QColor(_ACCENT)
        glow.setAlpha(95)
        painter.setPen(QPen(glow, 1.5))
        painter.drawPath(path)

        painter.setClipPath(path)

        self._paint_corners(painter, body)
        self._paint_heading(painter)
        self._paint_items(painter)
        self._paint_picture(painter)
        self._paint_markets(painter)
        self._paint_scanline(painter, body)

        painter.end()

    def _tint(self, alpha):
        colour = QColor(_ACCENT)
        colour.setAlpha(alpha)
        return colour

    def _paint_corners(self, painter, body):
        painter.setPen(QPen(self._tint(155), 2.0))

        span = 20

        for x, y, dx, dy in (
            (body.left() + 12, body.top() + 12, 1, 1),
            (body.right() - 12, body.top() + 12, -1, 1),
            (body.left() + 12, body.bottom() - 12, 1, -1),
            (body.right() - 12, body.bottom() - 12, -1, -1),
        ):
            painter.drawLine(int(x), int(y), int(x + span * dx), int(y))
            painter.drawLine(int(x), int(y), int(x), int(y + span * dy))

    def _paint_heading(self, painter):
        heading = (self._region or "").upper() or "NEWS"

        if heading != "NEWS":
            heading = f"{heading} NEWS"

        painter.setPen(QPen(_ACCENT))

        font = QFont("Consolas", 12, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.0)
        painter.setFont(font)
        painter.drawText(_MARGIN, 48, heading)

        painter.setPen(QPen(self._tint(80), 1.0))
        painter.drawLine(_MARGIN, 58, _WIDTH - _MARGIN, 58)

    def _paint_items(self, painter):
        if not self._items:
            painter.setPen(QPen(self._tint(170)))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(_MARGIN, 96, "No headlines available, sir.")
            return

        number_font = QFont("Consolas", 9)
        title_font = QFont("Segoe UI", 10)
        metrics = QFontMetrics(title_font)

        y = 88
        width = _WIDTH - _MARGIN * 2 - 30

        for index, item in enumerate(self._items):
            if index >= self._revealed:
                break

            if y > self._items_bottom():
                break

            chosen = index == self._highlight
            lines = self._wrap(metrics, item.get("title", ""), width, 2)

            if chosen:
                # A soft bar behind the story being read out, sized to the
                # headline so it never bleeds into the next one.
                band = QRectF(
                    _MARGIN - 8, y - 13,
                    _WIDTH - _MARGIN * 2 + 16,
                    17 * len(lines) + 5,
                )
                painter.fillPath(self._rounded(band), self._tint(30))

            painter.setPen(QPen(self._tint(230 if chosen else 150)))
            painter.setFont(number_font)
            painter.drawText(_MARGIN, y, f"{index + 1:02d}")

            painter.setPen(
                QPen(QColor(255, 255, 255, 255) if chosen
                     else QColor(228, 240, 250, 240))
            )
            painter.setFont(title_font)

            for offset, line in enumerate(lines):
                painter.drawText(_MARGIN + 30, y + offset * 17, line)

            y += 17 * len(lines) + 15

    @staticmethod
    def _rounded(rect, radius=8):
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    @staticmethod
    def _wrap(metrics, text, width, max_lines):
        words = (text or "").split()
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

        if not lines:
            return [""]

        if len(lines) == max_lines:
            lines[-1] = metrics.elidedText(
                lines[-1], Qt.TextElideMode.ElideRight, width
            )

        return lines

    def _items_bottom(self):
        """Headlines stop higher up when a picture is showing."""
        return _IMAGE_TOP - 12 if self._picture else _ITEMS_BOTTOM

    def _paint_picture(self, painter):
        """The headline picture, fitted without distortion."""
        if not self._picture:
            return

        available = QRectF(
            _MARGIN, _IMAGE_TOP,
            _WIDTH - _MARGIN * 2, _IMAGE_HEIGHT,
        )

        scaled = self._picture.scaled(
            int(available.width()), int(available.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        x = available.left() + (available.width() - scaled.width()) / 2
        target = QRectF(x, available.top(), scaled.width(), scaled.height())

        painter.save()
        painter.setClipPath(self._rounded(target, 10))
        painter.drawImage(target.topLeft(), scaled)
        painter.restore()

        painter.setPen(QPen(self._tint(110), 1.2))
        painter.drawPath(self._rounded(target, 10))

        if self._caption:
            painter.setPen(QPen(self._tint(170)))
            painter.setFont(QFont("Segoe UI", 8))

            metrics = QFontMetrics(painter.font())
            text = metrics.elidedText(
                self._caption, Qt.TextElideMode.ElideRight,
                _WIDTH - _MARGIN * 2,
            )
            painter.drawText(_MARGIN, int(target.bottom()) + 15, text)

    def _paint_markets(self, painter):
        """A live price strip along the foot of the panel."""
        if not self._market_rows:
            return

        painter.setPen(QPen(self._tint(70), 1.0))
        painter.drawLine(
            _MARGIN, _MARKET_TOP - 16, _WIDTH - _MARGIN, _MARKET_TOP - 16
        )

        font = QFont("Consolas", 9)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        x = _MARGIN

        for label, text, change in self._market_rows[:4]:
            if change > 0.05:
                colour, mark = _UP, "\u25b2"
            elif change < -0.05:
                colour, mark = _DOWN, "\u25bc"
            else:
                colour, mark = _FLAT, "\u2013"

            painter.setPen(QPen(self._tint(150)))
            painter.drawText(x, _MARKET_TOP, label)
            x += metrics.horizontalAdvance(label) + 6

            piece = f"{mark} {text}"
            painter.setPen(QPen(colour))
            painter.drawText(x, _MARKET_TOP, piece)
            x += metrics.horizontalAdvance(piece) + 20

            if x > _WIDTH - _MARGIN - 40:
                break

    def _paint_scanline(self, painter, body):
        y = body.top() + body.height() * self._sweep

        painter.setPen(QPen(self._tint(26), 1.0))
        painter.drawLine(int(body.left()), int(y), int(body.right()), int(y))
