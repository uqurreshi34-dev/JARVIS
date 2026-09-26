"""The file hologram: numbered cards, projected beside the HUD.

actions/file_hologram.py decides what to show; this draws it. A page of up
to ten cards, two by five, each with its number large enough to read from
across the room -- the number is what you say. When a card is asked about,
it lights and a slab rises from the foot of the projection with its first
lines, or its picture, or why there is nothing to read.

The hologram part is in the drawing: the projection leans a few degrees as
if cast from the HUD, cards materialise one after another with a scan line
passing down each, faint scan lines drift across the whole, and the light
flickers very slightly, the way projected light does. None of it touches a
word's legibility: the lean is small and the flicker never dims the text
below nine tenths.
"""

import math
import random
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QImage, QImageReader, QPainter, QPainterPath, QPen, QRegion,
                         QTransform)
from PyQt6.QtWidgets import QWidget

from sensor_panel import fitting, shrunk


_WIDTH = 540
_HEIGHT = 500
_BEAM_GAP = 74              # room for the beam to cross to the HUD

_MARGIN = 22
_HEADER = 58
_COLUMNS = 2
_ROWS = 5
_CARD_H = 58
_CARD_GAP = 8
_FOOT = 34
_ARROW_R = 11               # the round arrow buttons in the header

_LEAN_DEGREES = 6.0         # the projection leans towards the HUD it comes from
_APPEAR_EACH = 0.055        # seconds between one card materialising and the next
_APPEAR_FOR = 0.28          # how long one takes
_SLAB_RISE = 0.22           # the detail slab rising

_FRAME_MS = 33             # while cards materialise or the slab moves
_STEADY_MS = 66            # after: only the scan lines, flicker and arrows move

_ACCENT = QColor(95, 200, 245)
_BRIGHT = QColor(205, 240, 255)
_TEXT = QColor(215, 238, 250)
_DIM = QColor(120, 150, 172)
_FOLDER = QColor(255, 196, 80)
_BACKDROP = QColor(6, 14, 22, 214)


def _now():
    return time.monotonic()


def hints(view):
    """The foot's SAY: line for [view], longest first, to fit the room there.

    It names what can be said here: pages only when there are some, going
    back only inside a folder, and an example that is a card on this page,
    called what it is: "folder 3" on a page of folders, "file 13" on files.
    """
    extra = []

    if view.get("pages", 1) > 1:
        extra.append("PREVIOUS PAGE" if view.get("page", 0) == view["pages"] - 1 else "NEXT PAGE")

    if len(view.get("trail") or []) > 1:
        extra.append("GO BACK")

    cards = view.get("cards") or []

    if not cards:
        return ["SAY: " + " - ".join(extra + ["CLOSE FILES"])]

    example = cards[min(2, len(cards) - 1)]
    said = f"{'FOLDER' if example['kind'] == 'folder' else 'FILE'} {example['number']}"
    what, summary, opening = f"WHAT'S {said}", f"SUMMARISE {said}", f"OPEN {example['number']}"

    return [
        "SAY: " + " - ".join([what, summary, opening] + extra + ["CLOSE FILES"]),
        "SAY: " + " - ".join([summary, opening] + extra + ["CLOSE FILES"]),
        "SAY: " + " - ".join([summary] + extra[:1] + ["CLOSE FILES"]),
        "SAY: " + summary,
    ]


class FilesPanel(QWidget):
    """The projection. Fed a view by file_hologram through show_view."""

    show_view = pyqtSignal(dict)
    hide_view = pyqtSignal()
    card_clicked = pyqtSignal(int)
    nav_clicked = pyqtSignal(str)       # "back", "previous" or "next"
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()

        self._anchor = None
        self._view = None
        self._shown_at = 0.0
        self._focus_at = 0.0
        self._focus_key = None
        self._image = None
        self._scan = 0.0
        self._flicker = 1.0
        self._drag_offset = None
        self._press = None
        self._card_rects = {}
        self._nav_rects = {}
        self._arrows = []
        self._layer = None          # everything that holds still, drawn once
        self._layer_dirty = True
        self._layer_moving = False
        self._scan_lines = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.show_view.connect(self._on_view)
        self.hide_view.connect(self._on_hide)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---- placing it -----------------------------------------------------------------

    def set_anchor(self, widget):
        """Sit alongside the HUD, joined to it by the beam."""
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

        self.move(screen.left() + (screen.width() - _WIDTH) // 2, screen.top() + (screen.height() - _HEIGHT) // 2)

    # ---- what to show ---------------------------------------------------------------

    def _on_view(self, view):
        first = self._view is None
        before = self._view

        # A new folder or page materialises afresh; a focus change does not.
        fresh = first or (before.get("trail"), before.get("page")) != (view.get("trail"), view.get("page"))

        self._view = dict(view)
        self._layer_dirty = True

        if fresh:
            self._shown_at = _now()

        focus = view.get("focus")
        key = (focus or {}).get("number")

        if key != self._focus_key or (focus and focus.get("image") and self._image is None):
            self._focus_key = key
            self._focus_at = _now()
            self._image = self._load_image(focus.get("image")) if focus and focus.get("image") else None

        if first or not self.isVisible():
            self._position()
            self.show()

        self._timer.start(_FRAME_MS)
        self.update()

    def _on_hide(self):
        self._timer.stop()
        self._view = None
        self._image = None
        self._focus_key = None
        self.hide()

    def _load_image(self, path):
        """The picture, read at the size it is shown, so a big photo loads quickly."""
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()

        if size.isValid() and size.width() > 0 and size.height() > 0:
            scale = min(200 / size.width(), 130 / size.height(), 1.0)
            reader.setScaledSize(QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale))))

        image = reader.read()
        return None if image.isNull() else image

    def _tick(self):
        self._scan = (self._scan + 0.9) % 6.0

        # Projected light wavers a little, and now and then dips.
        target = 0.97 + 0.03 * random.random()

        if random.random() < 0.012:
            target = 0.9

        self._flicker += (target - self._flicker) * 0.35

        # Full speed only while something is moving; the HUD shares this thread.
        wanted = _FRAME_MS if self._moving(_now()) else _STEADY_MS

        if self._timer.interval() != wanted:
            self._timer.setInterval(wanted)

        self.update()

    # ---- clicks ---------------------------------------------------------------------

    def _unlean(self, point):
        inverted, ok = self._lean().inverted()
        return inverted.map(point) if ok else point

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position()
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and self._press is not None:
            if (event.position() - self._press).manhattanLength() > 6:
                self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        press, self._press, self._drag_offset = self._press, None, None

        if press is None or (event.position() - press).manhattanLength() > 6:
            return

        point = self._unlean(event.position())

        # The arrows in the header first: they are never under a card.
        for which, rect in self._nav_rects.items():
            if rect.adjusted(-6, -6, 6, 6).contains(point):
                self.nav_clicked.emit(which)
                return

        for number, rect in self._card_rects.items():
            if rect.contains(point):
                self.card_clicked.emit(number)
                return

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    # ---- drawing --------------------------------------------------------------------

    def _lean(self):
        """The projection, turned a few degrees about its upright centre line."""
        centre = QPointF(_WIDTH / 2, _HEIGHT / 2)
        lean = QTransform()
        lean.translate(centre.x(), centre.y())
        lean.rotate(_LEAN_DEGREES, Qt.Axis.YAxis)
        lean.translate(-centre.x(), -centre.y())
        return lean

    def _tint(self, colour, alpha):
        colour = QColor(colour)
        colour.setAlpha(max(0, min(255, int(alpha))))
        return colour

    def card_rect(self, index):
        column, row = index % _COLUMNS, index // _COLUMNS
        width = (_WIDTH - 2 * _MARGIN - (_COLUMNS - 1) * _CARD_GAP) / _COLUMNS
        return QRectF(_MARGIN + column * (width + _CARD_GAP), _HEADER + row * (_CARD_H + _CARD_GAP), width, _CARD_H)

    def _moving(self, now):
        """Cards still materialising, or the detail slab still rising."""
        cards = len((self._view or {}).get("cards") or [])
        appearing = now - self._shown_at < cards * _APPEAR_EACH + _APPEAR_FOR
        rising = bool((self._view or {}).get("focus")) and now - self._focus_at < _SLAB_RISE
        return appearing or rising

    def paintEvent(self, event):
        """The still layer, leaned, with the moving light on top.

        Drawing every card, glyph and scan line through the lean each frame
        cost more than a frame and slowed the HUD's reactor, which runs on
        the same thread. Now the cards are drawn, leaned, into an image
        only when they change or move; each frame copies that image and adds
        the scan lines and the arrows' pulse.
        """
        if not self._view:
            return

        now = _now()

        moving = self._moving(now)

        # Once more after the movement ends, so the last frame is the settled one.
        if self._layer_dirty or self._layer is None or moving or self._layer_moving:
            self._compose(now)
            self._layer_moving = moving

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._flicker)

        # The layer is drawn already leaned, so this is a plain copy.
        painter.drawImage(QPointF(0, 0), self._layer)
        self._paint_scanlines(painter)

        painter.setTransform(self._lean())

        for centre, direction, colour in self._arrows:
            self._paint_arrow(painter, centre, direction, colour)

        painter.end()

    def _compose(self, now):
        """Draw everything that holds still into the layer image, leaned."""
        ratio = max(1.0, self.devicePixelRatioF())
        size = QSize(int(_WIDTH * ratio), int(_HEIGHT * ratio))

        if self._layer is None or self._layer.size() != size:
            self._layer = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
            self._layer.setDevicePixelRatio(ratio)

        self._layer.fill(0)
        painter = QPainter(self._layer)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setTransform(self._lean())

        self._paint_frame(painter)
        self._paint_header(painter)

        self._card_rects = {}
        focus = self._view.get("focus") or None

        for index, card in enumerate(self._view.get("cards") or []):
            rect = self.card_rect(index)
            self._card_rects[card["number"]] = rect
            appear = (now - self._shown_at - index * _APPEAR_EACH) / _APPEAR_FOR
            self._paint_card(painter, rect, card, max(0.0, min(1.0, appear)),
                             lit=bool(focus and focus.get("number") == card["number"]))

        if not self._view.get("cards"):
            painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            painter.setPen(self._tint(_DIM, 230))
            painter.drawText(QRectF(0, _HEADER, _WIDTH, 200), Qt.AlignmentFlag.AlignCenter, "THIS FOLDER IS EMPTY")

        if focus:
            numbers = [card["number"] for card in self._view.get("cards") or []]
            row = numbers.index(focus.get("number")) // _COLUMNS if focus.get("number") in numbers else 0
            # Over the half the card is not in, so the card stays in sight.
            self._paint_slab(painter, focus, min(1.0, (now - self._focus_at) / _SLAB_RISE), from_top=row >= 3)

        self._paint_foot(painter)
        painter.end()
        self._layer_dirty = False

    def _paint_frame(self, painter):
        body = QRectF(self.rect()).adjusted(5, 5, -5, -5)
        path = QPainterPath()
        path.addRoundedRect(body, 16, 16)
        painter.fillPath(path, _BACKDROP)
        painter.setPen(QPen(self._tint(_ACCENT, 110), 1.4))
        painter.drawPath(path)

        painter.setPen(QPen(self._tint(_ACCENT, 170), 2.0))
        span = 20

        for x, y, dx, dy in ((body.left() + 10, body.top() + 10, 1, 1), (body.right() - 10, body.top() + 10, -1, 1),
                             (body.left() + 10, body.bottom() - 10, 1, -1), (body.right() - 10, body.bottom() - 10, -1, -1)):
            painter.drawLine(QPointF(x, y), QPointF(x + span * dx, y))
            painter.drawLine(QPointF(x, y), QPointF(x, y + span * dy))

    def _paint_header(self, painter):
        """FILES, where you are, and the arrows.

        A back arrow on the left while inside a folder; page arrows on the
        right, each drawn only when there is a page that way to go.
        """
        view = self._view
        title = QFont("Consolas", 12, QFont.Weight.Bold)
        title.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 118)
        small = QFont("Consolas", 7, QFont.Weight.Bold)
        title_m, small_m = QFontMetrics(title), QFontMetrics(small)
        middle = 29
        self._nav_rects = {}
        self._arrows = []

        trail_parts = view.get("trail") or ["JARVIS"]
        left = _MARGIN + 18

        if len(trail_parts) > 1:
            self._nav_rects["back"] = self._arrow(QPointF(left + _ARROW_R - 4, middle), -1, _FOLDER)
            left += 2 * _ARROW_R + 6

        painter.setFont(title)
        painter.setPen(self._tint(_ACCENT, 240))
        painter.drawText(QPointF(left, 34), "FILES")
        left += title_m.horizontalAdvance("FILES") + 14

        right = _WIDTH - _MARGIN - 18
        pages, page = view.get("pages", 1), view.get("page", 0)

        if pages > 1:
            # Room is kept for both arrows, so the page words never jump.
            next_centre = QPointF(right - _ARROW_R, middle)

            if page < pages - 1:
                self._nav_rects["next"] = self._arrow(next_centre, 1, _ACCENT)

            words = f"PAGE {page + 1} OF {pages}"
            words_right = right - 2 * _ARROW_R - 8
            painter.setFont(small)
            painter.setPen(self._tint(_TEXT, 230))
            painter.drawText(QPointF(words_right - small_m.horizontalAdvance(words), 33), words)

            previous_centre = QPointF(words_right - small_m.horizontalAdvance(words) - 8 - _ARROW_R, middle)

            if page > 0:
                self._nav_rects["previous"] = self._arrow(previous_centre, -1, _ACCENT)

            right = previous_centre.x() - _ARROW_R

        count = view.get("count", 0)
        trail = " / ".join(trail_parts).upper()
        items = f"{count} ITEM{'S' if count != 1 else ''}"
        room = right - 14 - left

        painter.setFont(small)
        painter.setPen(self._tint(_TEXT, 220))
        painter.drawText(QPointF(left, 33), fitting([f"{trail}  -  {items}", trail, trail.split(" / ")[-1]], small_m, room))

        painter.setPen(QPen(self._tint(_ACCENT, 70), 1.0))
        painter.drawLine(QPointF(_MARGIN, _HEADER - 10), QPointF(_WIDTH - _MARGIN, _HEADER - 10))

    def _arrow(self, centre, direction, colour):
        """Note an arrow to draw each frame, over the still layer. Returns its rect."""
        self._arrows.append((centre, direction, colour))
        return QRectF(centre.x() - _ARROW_R, centre.y() - _ARROW_R, 2 * _ARROW_R, 2 * _ARROW_R)

    def _paint_arrow(self, painter, centre, direction, colour):
        """A round arrow button, pulsing softly so it reads as something to press. Returns its rect."""
        pulse = 0.5 + 0.5 * math.sin(_now() * 3.2)
        ring = QRectF(centre.x() - _ARROW_R, centre.y() - _ARROW_R, 2 * _ARROW_R, 2 * _ARROW_R)

        painter.save()
        glow = QPainterPath()
        glow.addEllipse(ring.adjusted(-3, -3, 3, 3))
        painter.fillPath(glow, self._tint(colour, 18 + 22 * pulse))

        face = QPainterPath()
        face.addEllipse(ring)
        painter.fillPath(face, QColor(4, 12, 20, 230))
        painter.setPen(QPen(self._tint(colour, 150 + 80 * pulse), 1.4))
        painter.drawPath(face)

        span = _ARROW_R * 0.42
        tip = QPointF(centre.x() + direction * span * 0.7, centre.y())
        tail_x = centre.x() - direction * span * 0.5
        chevron = QPainterPath()
        chevron.moveTo(tail_x, centre.y() - span)
        chevron.lineTo(tip)
        chevron.lineTo(tail_x, centre.y() + span)
        painter.setPen(QPen(self._tint(colour, 245), 2.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(chevron)
        painter.restore()

        return ring

    def _paint_card(self, painter, rect, card, appear, lit):
        if appear <= 0.0:
            return

        eased = 1 - (1 - appear) ** 3
        painter.save()
        painter.setOpacity(eased)
        painter.translate((1 - eased) * 14, 0)

        colour = _FOLDER if card["kind"] == "folder" else _ACCENT
        shape = QPainterPath()
        shape.addRoundedRect(rect, 7, 7)
        painter.fillPath(shape, self._tint(colour, 58 if lit else 22))
        painter.setPen(QPen(self._tint(colour, 235 if lit else 90), 1.6 if lit else 1.0))
        painter.drawPath(shape)

        # The number: what you say.
        disc = QPointF(rect.left() + 26, rect.center().y())
        painter.setPen(QPen(self._tint(colour, 220), 1.6))
        painter.setBrush(self._tint(colour, 40))
        painter.drawEllipse(disc, 17, 17)
        number = str(card["number"])
        number_font = shrunk(number, "Consolas", 14, 8, 30)
        number_m = QFontMetrics(number_font)
        painter.setFont(number_font)
        painter.setPen(self._tint(_BRIGHT, 255))
        painter.drawText(QPointF(disc.x() - number_m.horizontalAdvance(number) / 2,
                                 disc.y() + number_m.ascent() / 2 - 2), number)

        # What kind of thing it is, drawn rather than read.
        self._paint_glyph(painter, QRectF(rect.left() + 50, rect.top() + 12, 22, rect.height() - 24), card, colour)

        text_left = rect.left() + 80
        room = rect.right() - 10 - text_left
        name_font = shrunk(card["name"], "Segoe UI", 10, 8, room, QFont.Weight.DemiBold)
        name_m = QFontMetrics(name_font)
        painter.setFont(name_font)
        painter.setPen(self._tint(_TEXT, 255))
        name_line = rect.top() + 7 + name_m.ascent()
        painter.drawText(QPointF(text_left, name_line), name_m.elidedText(card["name"], Qt.TextElideMode.ElideMiddle, int(room)))

        meta_font = QFont("Consolas", 7, QFont.Weight.Bold)
        meta_m = QFontMetrics(meta_font)
        painter.setFont(meta_font)
        painter.setPen(self._tint(_DIM, 235))
        meta = card["meta"]
        meta_line = min(rect.bottom() - 15, name_line + name_m.descent() + meta_m.ascent() + 2)
        painter.drawText(QPointF(text_left, meta_line), fitting([meta, meta.rsplit(" - ", 1)[0], meta.split(" - ")[0]], meta_m, room))

        # A file's size, as a bar: fuller for bigger.
        if card["kind"] == "file":
            bar = QRectF(text_left, rect.bottom() - 11, room, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._tint(colour, 40))
            painter.drawRoundedRect(bar, 1.5, 1.5)
            painter.setBrush(self._tint(colour, 200))
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(), max(3.0, bar.width() * card["share"]), bar.height()), 1.5, 1.5)

        # Materialising: a bright line passing down the card.
        if appear < 1.0:
            y = rect.top() + rect.height() * appear
            painter.setPen(QPen(self._tint(_BRIGHT, 200 * (1 - appear)), 1.4))
            painter.drawLine(QPointF(rect.left() + 4, y), QPointF(rect.right() - 4, y))

        painter.restore()

    def _paint_glyph(self, painter, box, card, colour):
        pen = QPen(self._tint(colour, 210), 1.3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        kind = card.get("type", "")
        x, y, w, h = box.left(), box.top(), box.width(), box.height()

        if card["kind"] == "folder":
            path = QPainterPath()
            path.moveTo(x, y + 4)
            path.lineTo(x + w * 0.4, y + 4)
            path.lineTo(x + w * 0.5, y + 8)
            path.lineTo(x + w, y + 8)
            path.lineTo(x + w, y + h)
            path.lineTo(x, y + h)
            path.closeSubpath()
            painter.drawPath(path)
            return

        page = QPainterPath()
        page.moveTo(x + 2, y)
        page.lineTo(x + w - 7, y)
        page.lineTo(x + w - 2, y + 5)
        page.lineTo(x + w - 2, y + h)
        page.lineTo(x + 2, y + h)
        page.closeSubpath()
        painter.drawPath(page)
        inner = QRectF(x + 5, y + 8, w - 10, h - 12)

        if kind == "image":
            painter.drawRect(inner)
            mountain = QPainterPath()
            mountain.moveTo(inner.left(), inner.bottom())
            mountain.lineTo(inner.left() + inner.width() * 0.4, inner.top() + inner.height() * 0.35)
            mountain.lineTo(inner.right(), inner.bottom())
            painter.drawPath(mountain)
        elif kind == "video":
            triangle = QPainterPath()
            triangle.moveTo(inner.left() + 2, inner.top())
            triangle.lineTo(inner.right(), inner.center().y())
            triangle.lineTo(inner.left() + 2, inner.bottom())
            triangle.closeSubpath()
            painter.drawPath(triangle)
        elif kind == "spreadsheet":
            for step in range(1, 3):
                painter.drawLine(QPointF(inner.left() + inner.width() * step / 3, inner.top()),
                                 QPointF(inner.left() + inner.width() * step / 3, inner.bottom()))
            for step in range(1, 4):
                painter.drawLine(QPointF(inner.left(), inner.top() + inner.height() * step / 4),
                                 QPointF(inner.right(), inner.top() + inner.height() * step / 4))
        elif kind == "code":
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, "</>")
        elif kind == "sound":
            for index in range(4):
                height = inner.height() * (0.35 + 0.65 * abs(math.sin(index * 1.7 + 0.6)))
                px = inner.left() + index * inner.width() / 3.5
                painter.drawLine(QPointF(px, inner.center().y() - height / 2), QPointF(px, inner.center().y() + height / 2))
        else:
            for index in range(3):
                py = inner.top() + 2 + index * inner.height() / 3
                painter.drawLine(QPointF(inner.left(), py), QPointF(inner.right() - (index == 2) * 4, py))

    def _paint_slab(self, painter, focus, rise, from_top=False):
        """The one asked about: its lines, its picture, or why there are none.

        It rises from the foot, or drops from the header when the card it
        is about sits in the lower rows.
        """
        eased = 1 - (1 - rise) ** 3
        height = 186

        if from_top:
            top = _HEADER - 4 - height + height * eased
        else:
            top = _HEIGHT - _FOOT - height * eased - 6
        slab = QRectF(_MARGIN - 4, top, _WIDTH - 2 * (_MARGIN - 4), height)
        colour = _FOLDER if focus.get("kind") == "folder" else _ACCENT

        painter.save()
        painter.setClipRect(QRectF(0, _HEADER - 4, _WIDTH, _HEIGHT - _FOOT - _HEADER + 4))

        shape = QPainterPath()
        shape.addRoundedRect(slab, 10, 10)
        painter.fillPath(shape, QColor(4, 10, 17, 238))
        painter.setPen(QPen(self._tint(colour, 220), 1.4))
        painter.drawPath(shape)

        label = QFont("Consolas", 8, QFont.Weight.Bold)
        label_m = QFontMetrics(label)
        painter.setFont(label)
        painter.setPen(self._tint(colour, 240))
        y = slab.top() + 8 + label_m.ascent()
        painter.drawText(QPointF(slab.left() + 16, y), f"{'FOLDER' if focus.get('kind') == 'folder' else 'FILE'} {focus.get('number')}")

        room = slab.width() - 32
        name_font = shrunk(focus.get("name", ""), "Segoe UI", 12, 9, room, QFont.Weight.DemiBold)
        name_m = QFontMetrics(name_font)
        painter.setFont(name_font)
        painter.setPen(self._tint(_TEXT, 255))
        y += label_m.descent() + name_m.ascent() + 2
        painter.drawText(QPointF(slab.left() + 16, y), name_m.elidedText(focus.get("name", ""), Qt.TextElideMode.ElideMiddle, int(room)))

        painter.setFont(label)
        painter.setPen(self._tint(_DIM, 235))
        y += name_m.descent() + label_m.ascent() + 2
        meta = focus.get("meta", "")
        painter.drawText(QPointF(slab.left() + 16, y), fitting([meta, meta.rsplit(" - ", 1)[0]], label_m, room))

        body_top = y + label_m.descent() + 10

        if self._image is not None:
            image = self._image
            target = QRectF(slab.left() + 16, body_top, image.width(), image.height())
            painter.drawImage(target, image)
            painter.setPen(QPen(self._tint(colour, 150), 1.0))
            painter.drawRect(target)
        elif focus.get("lines"):
            line_font = QFont("Segoe UI", 9)
            line_m = QFontMetrics(line_font)
            painter.setFont(line_font)
            y = body_top + line_m.ascent()

            indent = line_m.horizontalAdvance("> ") + 2

            for line in focus["lines"]:
                if y > slab.bottom() - 6:
                    break
                painter.setPen(self._tint(colour, 200))
                painter.drawText(QPointF(slab.left() + 16, y), ">")
                painter.setPen(self._tint(_TEXT, 245))
                painter.drawText(QPointF(slab.left() + 16 + indent, y),
                                 line_m.elidedText(line, Qt.TextElideMode.ElideRight, int(room - indent)))
                y += line_m.height() + 4
        elif focus.get("note"):
            painter.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            painter.setPen(self._tint(_DIM, 240))
            painter.drawText(QRectF(slab.left(), body_top, slab.width(), 60), Qt.AlignmentFlag.AlignCenter, focus["note"])

        painter.restore()

    def _paint_foot(self, painter):
        small = QFont("Consolas", 7, QFont.Weight.Bold)
        small_m = QFontMetrics(small)
        painter.setFont(small)
        painter.setPen(self._tint(_DIM, 200))

        hint = fitting(hints(self._view), small_m, _WIDTH - 2 * (_MARGIN + 18))
        painter.drawText(QPointF((_WIDTH - small_m.horizontalAdvance(hint)) / 2, _HEIGHT - 16), hint)

    def _paint_scanlines(self, painter):
        """Faint lines drifting down: one image, drawn a little lower each frame."""
        top, bottom = _HEADER - 8, _HEIGHT - 10

        if self._scan_lines is None:
            ratio = max(1.0, self.devicePixelRatioF())
            lines = QImage(int((_WIDTH - 24) * ratio), int((bottom - top + 6) * ratio), QImage.Format.Format_ARGB32_Premultiplied)
            lines.setDevicePixelRatio(ratio)
            lines.fill(0)
            drawing = QPainter(lines)
            drawing.setPen(QPen(self._tint(_ACCENT, 11), 1.0))
            y = 0.5

            while y < bottom - top + 6:
                drawing.drawLine(QPointF(0, y), QPointF(_WIDTH - 24, y))
                y += 6

            drawing.end()
            self._scan_lines = lines

        # Straight lines, clipped to the leaned body: at a lean of a few
        # degrees the difference is not visible, and a straight copy is cheap.
        painter.save()
        painter.setClipRegion(QRegion(self._lean().mapToPolygon(QRect(12, top, _WIDTH - 24, bottom - top))))
        painter.drawImage(QPointF(12, top - 6 + self._scan), self._scan_lines)
        painter.restore()
