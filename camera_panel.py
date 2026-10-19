from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QWidget


_WIDTH = 600
_HEIGHT = 470
_MARGIN = 22

# Every camera at once: a bigger panel, so a tile is worth looking at and one enlarged fills it.
_GRID_WIDTH = 900
_GRID_HEIGHT = 600
_GAP = 12

# A press that moves less than this is a tap (a touchpad's too), not a drag.
_TAP_DISTANCE = 6

_BACKDROP = QColor(8, 14, 21, 236)
_ACCENT = QColor(95, 200, 245)

# Space left for the beam to cross to the HUD.
_BEAM_GAP = 74

_FRAME_MS = 33


class CameraPanel(QWidget):
    """Shows what JARVIS last saw through the camera."""

    show_view = pyqtSignal(bytes, str)
    hide_view = pyqtSignal()
    # Every camera at once: show_grid([(name, title)]) opens the tiles, grid_frame(name, jpeg) updates one,
    # focus_camera(name) makes one big; a tap on a tile does the same, and BACK returns to them all.
    show_grid = pyqtSignal(list)
    grid_frame = pyqtSignal(str, bytes)
    focus_camera = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        self._image = None
        self._title = ""
        self._sweep = 0.0
        self._anchor = None
        self._drag_offset = None
        self._pressed_at = None
        self._tiles = {}        # name -> {"title", "image"}, in the order shown
        self._focused = None    # the tile made big, or None
        self._grid = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.show_view.connect(self._on_show)
        self.hide_view.connect(self._on_hide)
        self.show_grid.connect(self._on_grid)
        self.grid_frame.connect(self._on_grid_frame)
        self.focus_camera.connect(self._on_focus)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        """Sit alongside another window, joined by the beam."""
        self._anchor = widget

    def _on_show(self, data, title):
        """Display a chart, or clear it when given nothing."""
        if not data:
            self._on_hide()
            return

        image = QImage()

        if not image.loadFromData(data):
            print("[JARVIS] could not decode the picture")
            return

        self._leave_grid()

        # A live camera sends many frames a second: once the panel is up, each one only replaces the
        # picture, so it is not moved back or raised over whatever you are doing on every frame.
        if self.isVisible() and self._image is not None and (title or "") == self._title:
            self._image = image
            self.update()
            return

        self._image = image
        self._title = title or ""

        self._position()
        self.show()
        self.raise_()

        self._animate.start(_FRAME_MS)
        self.update()

    def _on_hide(self):
        self._animate.stop()
        self._image = None
        self._title = ""
        self._leave_grid()
        self.hide()

    # ---- every camera at once -------------------------------------------------------------------

    def _size(self):
        return (_GRID_WIDTH, _GRID_HEIGHT) if self._grid else (_WIDTH, _HEIGHT)

    def _leave_grid(self):
        if self._grid:
            self._grid = False
            self._tiles = {}
            self._focused = None
            self.setFixedSize(_WIDTH, _HEIGHT)

    def _on_grid(self, cameras):
        """Open a tile for each (name, title) in [cameras], each waiting for its first frame."""
        self._image = None
        self._title = "CAMERAS"
        self._tiles = {}

        for entry in cameras:
            name, title = (entry["name"], entry["title"]) if isinstance(entry, dict) else entry
            self._tiles[name] = {"title": title, "image": None}

        self._focused = None
        self._grid = True
        self.setFixedSize(_GRID_WIDTH, _GRID_HEIGHT)
        self._position()
        self.show()
        self.raise_()
        self._animate.start(_FRAME_MS)
        self.update()

    def _on_grid_frame(self, name, data):
        tile = self._tiles.get(name)

        if tile is None or not data:
            return

        image = QImage()

        if image.loadFromData(data):
            tile["image"] = image
            self.update()

    def _on_focus(self, name):
        """Make camera [name]'s tile fill the panel; None or "" goes back to every camera."""
        self._focused = name if name in self._tiles else None
        self.update()

    def _tile_rects(self):
        """[(name, rect)] of the tiles, laid out in as square a grid as their number allows."""
        names = list(self._tiles)

        if not names:
            return []

        width, height = self._size()
        columns = 1

        while columns * columns < len(names):
            columns += 1

        rows = -(-len(names) // columns)
        area = QRectF(_MARGIN, 66, width - _MARGIN * 2, height - 66 - _MARGIN)
        cell_w = (area.width() - _GAP * (columns - 1)) / columns
        cell_h = (area.height() - _GAP * (rows - 1)) / rows

        return [(name, QRectF(area.left() + (index % columns) * (cell_w + _GAP),
                              area.top() + (index // columns) * (cell_h + _GAP), cell_w, cell_h))
                for index, name in enumerate(names)]

    def _back_rect(self):
        width, _height = self._size()
        return QRectF(width - _MARGIN - 92, 24, 92, 26)

    def _tapped(self, point):
        """A tap at [point]: a tile makes that camera big; BACK returns to them all."""
        if not self._grid:
            return

        if self._focused:
            if self._back_rect().contains(point):
                self._focused = None
                self.update()

            return

        for name, rect in self._tile_rects():
            if rect.contains(point):
                self._focused = name
                self.update()
                return

    def _position(self):
        screen = self.screen().availableGeometry()
        width, height = self._size()

        if self._anchor is not None and self._anchor.isVisible():
            frame = self._anchor.frameGeometry()

            x = frame.left() - width - _BEAM_GAP
            y = frame.center().y() - height // 2

            x = max(screen.left() + 12, x)
            y = max(screen.top() + 12, min(y, screen.bottom() - height - 12))

            self.move(int(x), int(y))
            return

        self.move(
            screen.left() + (screen.width() - width) // 2,
            screen.top() + (screen.height() - height) // 2,
        )

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._pressed_at = event.globalPosition()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        pressed, self._pressed_at = self._pressed_at, None
        self._drag_offset = None

        # Hardly moved: a tap, not a drag -- on a tile, or on BACK.
        if pressed is not None and (event.globalPosition() - pressed).manhattanLength() < _TAP_DISTANCE:
            self._tapped(QPointF(event.position()))

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

        if self._grid and not self._focused:
            self._paint_tiles(painter)
        elif self._grid:
            self._paint_back(painter)
            self._paint_picture(painter, self._tiles[self._focused]["image"],
                                QRectF(_MARGIN, 66, self.width() - _MARGIN * 2, self.height() - 66 - _MARGIN))
        else:
            self._paint_chart(painter)

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

        width = self.width()
        title = self._tiles[self._focused]["title"] if self._grid and self._focused else self._title
        metrics = QFontMetrics(font)
        heading = metrics.elidedText(
            (title or "CAMERA").upper(),
            Qt.TextElideMode.ElideRight,
            width - _MARGIN * 2 - (110 if self._grid and self._focused else 0),
        )

        painter.drawText(_MARGIN, 42, heading)

        painter.setPen(QPen(self._tint(80), 1.0))
        painter.drawLine(_MARGIN, 52, width - _MARGIN, 52)

    def _paint_back(self, painter):
        rect = self._back_rect()
        painter.setPen(QPen(self._tint(170), 1.2))
        painter.fillPath(self._rounded(rect, 6), self._tint(30))
        painter.drawPath(self._rounded(rect, 6))
        painter.setPen(QPen(_ACCENT))
        painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "\u2039  BACK")

    def _paint_tiles(self, painter):
        label_font = QFont("Consolas", 9, QFont.Weight.Bold)
        label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)

        for name, rect in self._tile_rects():
            tile = self._tiles[name]
            painter.fillPath(self._rounded(rect, 8), QColor(0, 0, 0, 120))

            if tile["image"] is not None:
                self._paint_picture(painter, tile["image"], rect, cover=True)
            else:
                painter.setPen(QPen(self._tint(150)))
                painter.setFont(QFont("Segoe UI", 10))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Waking the camera...")

            painter.setPen(QPen(self._tint(110), 1.2))
            painter.drawPath(self._rounded(rect, 8))

            band = QRectF(rect.left(), rect.bottom() - 26, rect.width(), 26)
            painter.save()
            painter.setClipPath(self._rounded(rect, 8))
            painter.fillRect(band, QColor(8, 14, 21, 190))
            painter.restore()
            painter.setPen(QPen(_ACCENT))
            painter.setFont(label_font)
            painter.drawText(band.adjusted(10, 0, -10, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, tile["title"])

    def _paint_picture(self, painter, image, available, cover=False):
        """[image] in [available]: whole (letterboxed), or with [cover] filling it, cropped to fit."""
        if image is None:
            painter.setPen(QPen(self._tint(150)))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(available, Qt.AlignmentFlag.AlignCenter, "Waking the camera...")
            return

        mode = (Qt.AspectRatioMode.KeepAspectRatioByExpanding if cover
                else Qt.AspectRatioMode.KeepAspectRatio)
        scaled = image.scaled(int(available.width()), int(available.height()), mode,
                              Qt.TransformationMode.SmoothTransformation)
        x = available.left() + (available.width() - scaled.width()) / 2
        y = available.top() + (available.height() - scaled.height()) / 2
        target = QRectF(x, y, scaled.width(), scaled.height())
        shown = available if cover else target

        painter.save()
        painter.setClipPath(self._rounded(shown, 8))
        painter.drawImage(target.topLeft(), scaled)
        painter.restore()

        painter.setPen(QPen(self._tint(100), 1.2))
        painter.drawPath(self._rounded(shown, 8))

    def _paint_chart(self, painter):
        if not self._image:
            painter.setPen(QPen(self._tint(170)))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(_MARGIN, 90, "Nothing to show, sir.")
            return

        available = QRectF(
            _MARGIN, 66,
            _WIDTH - _MARGIN * 2,
            _HEIGHT - 66 - _MARGIN,
        )

        scaled = self._image.scaled(
            int(available.width()), int(available.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        x = available.left() + (available.width() - scaled.width()) / 2
        y = available.top() + (available.height() - scaled.height()) / 2

        target = QRectF(x, y, scaled.width(), scaled.height())

        painter.save()
        painter.setClipPath(self._rounded(target, 8))
        painter.drawImage(target.topLeft(), scaled)
        painter.restore()

        painter.setPen(QPen(self._tint(100), 1.2))
        painter.drawPath(self._rounded(target, 8))

    @staticmethod
    def _rounded(rect, radius=8):
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    def _paint_scanline(self, painter, body):
        y = body.top() + body.height() * self._sweep

        painter.setPen(QPen(self._tint(24), 1.0))
        painter.drawLine(int(body.left()), int(y), int(body.right()), int(y))
