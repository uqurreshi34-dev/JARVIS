from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QWidget


_WIDTH = 600
_HEIGHT = 470
_MARGIN = 22

_BACKDROP = QColor(8, 14, 21, 236)
_ACCENT = QColor(95, 200, 245)

# Space left for the beam to cross to the HUD.
_BEAM_GAP = 74

_FRAME_MS = 33

# Where the attribution caption sits and how tall a strip it needs.
_CAPTION_HEIGHT = 20


class ImagePanel(QWidget):
    """Shows a photo fetched from Unsplash, with its required credit line.

    Built on the same window, drag, and beam conventions as camera_panel
    and chart_panel. The one addition is the caption strip along the
    bottom: Unsplash's guidelines require the photographer and Unsplash
    itself to be credited wherever a photo is shown, at every access
    tier, not only once an app is approved for production.
    """

    show_view = pyqtSignal(bytes, str, str, str, float)
    hide_view = pyqtSignal()

    def __init__(self):
        super().__init__()

        self._image = None
        self._title = ""
        self._caption = ""
        self._caption_link = None
        self._caption_rect = None
        # How far to zoom the photo, on top of the panel's own fit-to-
        # window sizing. Reported separately from the image bytes
        # rather than baked into them — see actions/images.py's
        # current_display() for exactly why that matters.
        self._scale = 1.0
        self._sweep = 0.0
        self._anchor = None
        self._drag_offset = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.show_view.connect(self._on_show)
        self.hide_view.connect(self._on_hide)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        """Sit alongside another window, joined by the beam."""
        self._anchor = widget

    def _on_show(self, data, title, caption, caption_link, scale):
        """Display a photo, or clear it when given nothing."""
        if not data:
            self._on_hide()
            return

        image = QImage()

        if not image.loadFromData(data):
            print("[JARVIS] could not decode the picture")
            return

        self._image = image
        self._title = title or ""
        self._caption = caption or ""
        self._caption_link = caption_link or None
        self._scale = scale if scale else 1.0

        self._position()
        self.show()
        self.raise_()

        self._animate.start(_FRAME_MS)
        self.update()

    def _on_hide(self):
        self._animate.stop()
        self._image = None
        self._title = ""
        self._caption = ""
        self._scale = 1.0
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

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        # A click on the credit line opens it rather than starting a
        # drag — Unsplash's guidelines call for attribution to be linked,
        # not just present as text.
        if (
            self._caption_link
            and self._caption_rect
            and self._caption_rect.contains(event.position())
        ):
            QDesktopServices.openUrl(QUrl(self._caption_link))
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

        self._paint_corners(painter, body)
        self._paint_heading(painter)
        self._paint_photo(painter)
        self._paint_caption(painter, body)
        self._paint_scanline(painter, body)

        painter.end()

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
        painter.setPen(QPen(_ACCENT))

        font = QFont("Consolas", 11, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.5)
        painter.setFont(font)

        metrics = QFontMetrics(font)
        heading = metrics.elidedText(
            (self._title or "IMAGE").upper(),
            Qt.TextElideMode.ElideRight,
            _WIDTH - _MARGIN * 2,
        )

        painter.drawText(_MARGIN, 42, heading)

        painter.setPen(QPen(self._tint(80), 1.0))
        painter.drawLine(_MARGIN, 52, _WIDTH - _MARGIN, 52)

    def _paint_photo(self, painter):
        if not self._image:
            painter.setPen(QPen(self._tint(170)))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(_MARGIN, 90, "Nothing to show, sir.")
            return

        available = QRectF(
            _MARGIN, 66,
            _WIDTH - _MARGIN * 2,
            _HEIGHT - 66 - _MARGIN - _CAPTION_HEIGHT,
        )

        # Fit to the available area at "100%" first, then apply the
        # actual zoom on top of that baseline. Applying zoom AFTER
        # fitting — rather than fitting whatever pixel size the image
        # happens to be — is what makes "make it bigger" visible at
        # all: a resized source image, fit straight back down to this
        # same window, would just come out the same on-screen size
        # either way. See actions/images.py's current_display().
        fitted = self._image.scaled(
            int(available.width()), int(available.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        target_w = max(1, round(fitted.width() * self._scale))
        target_h = max(1, round(fitted.height() * self._scale))

        scaled = self._image.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        x = available.left() + (available.width() - scaled.width()) / 2
        y = available.top() + (available.height() - scaled.height()) / 2

        target = QRectF(x, y, scaled.width(), scaled.height())

        # An enlarged photo can be bigger than the available area now;
        # clip to it so the overflow crops at the frame rather than
        # drawing over the heading or the caption below it.
        bounds = QPainterPath()
        bounds.addRect(available)
        clip = self._rounded(target, 8).intersected(bounds)

        painter.save()
        painter.setClipPath(clip)
        painter.drawImage(target.topLeft(), scaled)
        painter.setPen(QPen(self._tint(100), 1.2))
        painter.drawPath(self._rounded(target, 8))
        painter.restore()

    @staticmethod
    def _rounded(rect, radius=8):
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    def _paint_caption(self, painter, body):
        """Photographer and Unsplash credit, required at every access
        tier — see the module docstring. Clickable when a link is set;
        see mousePressEvent."""
        if not self._caption:
            self._caption_rect = None
            return

        font = QFont("Segoe UI", 8)
        colour = self._tint(190) if self._caption_link else self._tint(150)

        if self._caption_link:
            font.setUnderline(True)

        painter.setPen(QPen(colour))
        painter.setFont(font)

        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(self._caption)
        baseline_y = body.bottom() - 8

        painter.drawText(int(_MARGIN), int(baseline_y), self._caption)

        self._caption_rect = QRectF(
            _MARGIN, baseline_y - metrics.ascent(),
            text_width, metrics.height(),
        )

    def _paint_scanline(self, painter, body):
        y = body.top() + body.height() * self._sweep

        painter.setPen(QPen(self._tint(24), 1.0))
        painter.drawLine(int(body.left()), int(y), int(body.right()), int(y))
