"""The house hologram: your floor plan in 3D, beamed out beside the HUD.

Floors stack one above the other, walls rise out of them when it appears,
and each room is tinted by its temperature (sensor_panel's colours, so the
two panels agree). A pin stands where each board sits, glowing green while
it reports, amber when it has missed some, red once it is gone; movement
sends rings out across the floor from the pin that saw it. A camera is a
cone of light out through its wall, towards what house.json says it looks
at. A scan line sweeps the floors. Beside the plan, every room is listed
with its readings; tap a room, on the plan or in the list, to light it.

Drawn the way the file hologram learnt to be: everything that holds still
is drawn once into an image, only when the view changes, while it rises,
or once a second so ages move on. Each frame copies that image and adds
only what moves (the rings, the sweep, the pins' pulse), so the HUD's
reactor keeps its pace on the same thread.

Nothing here reads a file or a sensor. actions/house.py sends the view.
"""

import math
import random
import time

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                         QPolygonF)
from PyQt6.QtWidgets import QWidget

from sensor_panel import fitting, temperature_colour


_WIDTH = 660
_HEIGHT = 480
_BEAM_GAP = 74              # room for the beam to cross to the HUD

_MARGIN = 20
_HEADER = 54
_FOOT = 32
_LIST_W = 196               # the room list, on the side nearest the HUD

# The view of the plan: turned a little, looked down on, with perspective.
_YAW = math.radians(-26)
_ELEVATION = math.radians(44)
_DISTANCE = 2.9
_FLOOR_GAP = 1.0            # between stacked floors, as a share of the plan's size
_WALL = 0.075               # wall height, the same
_PIN = 0.13                 # how high a pin stands over its floor
_CONE_REACH = 2.2           # metres a camera's cone reaches out
_CONE_SPREAD = math.radians(28)

_RISE_FOR = 0.7             # walls rising out of the floor, when it appears
_FLOOR_EACH = 0.18          # one floor after another
_RIPPLE_FOR = 4.0           # rings after a movement
_SWEEP_EVERY = 5.5          # the scan line crossing the floors

_FRAME_MS = 33              # while something moves
_STEADY_MS = 50             # the sweep and pulses alone
_COMPOSE_EVERY = 1.0        # ages and states move on

_ACCENT = QColor(95, 200, 245)
_BRIGHT = QColor(205, 240, 255)
_TEXT = QColor(215, 238, 250)
_DIM = QColor(120, 150, 172)
_GLASS = QColor(140, 225, 255)
_LIVE = QColor(95, 255, 160)
_QUIET = QColor(255, 196, 80)
_GONE = QColor(255, 88, 88)
_WAITING = QColor(120, 150, 172)
_BACKDROP = QColor(6, 14, 22, 214)

_DEGREE = "\u00b0"

_STATE_COLOUR = {"live": _LIVE, "quiet": _QUIET, "offline": _GONE, "waiting": _WAITING}


def _now():
    return time.monotonic()


def _tint(colour, alpha):
    colour = QColor(colour)
    colour.setAlpha(max(0, min(255, int(alpha))))
    return colour


def hints(view):
    """The foot's SAY: line, longest first, named from this house's own rooms."""
    rooms = (view or {}).get("rooms") or []
    with_boards = [room["name"] for room in rooms if room["boards"]]
    first = (with_boards or [room["name"] for room in rooms] or ["my room"])[0]
    other = next((room["name"] for room in rooms if room["name"] != first), None)

    def spoken(name):
        return name.upper() if name.casefold().startswith(("my ", "the ")) else f"THE {name.upper()}"

    lines = [f"WHAT'S HAPPENING IN {spoken(first)}"]

    if other:
        lines.append(f"SHOW ME {spoken(other)}")

    return [
        "SAY: " + " - ".join(lines + ["CLOSE THE HOUSE"]),
        "SAY: " + " - ".join(lines[:1] + ["CLOSE THE HOUSE"]),
        "SAY: CLOSE THE HOUSE",
    ]


def room_readings(view, room):
    """(temperature, humidity, state, moved_at) for a room, from its boards; None where unknown."""
    boards = [view["boards"].get(board["name"]) for board in room["boards"]]
    boards = [board for board in boards if board]

    if not boards:
        return None, None, None, None

    order = ("live", "quiet", "offline", "waiting")
    best = min(boards, key=lambda board: order.index(board["state"]))
    temperatures = [b["readings"]["temperature"] for b in boards if "temperature" in b["readings"]]
    humidities = [b["readings"]["humidity"] for b in boards if "humidity" in b["readings"]]
    moved = [b["moved_at"] for b in boards if b["moved_at"] is not None]

    return (sum(temperatures) / len(temperatures) if temperatures else None,
            sum(humidities) / len(humidities) if humidities else None,
            best["state"], max(moved) if moved else None)


def camera_reach(room, camera):
    """The corners of a camera's cone on the floor, in metres: apex, both edges, the tip."""
    x, y, w, h = room["x"], room["y"], room["w"], room["h"]
    at = camera["at"]
    apex, normal = {"north": ((x + at * w, y), (0, -1)), "south": ((x + at * w, y + h), (0, 1)),
                    "west": ((x, y + at * h), (-1, 0)), "east": ((x + w, y + at * h), (1, 0))}[camera["wall"]]
    angle = math.atan2(normal[1], normal[0])
    edges = [(apex[0] + _CONE_REACH * math.cos(angle + turn), apex[1] + _CONE_REACH * math.sin(angle + turn))
             for turn in (-_CONE_SPREAD, _CONE_SPREAD)]
    tip = (apex[0] + _CONE_REACH * normal[0], apex[1] + _CONE_REACH * normal[1])
    return [apex] + edges + [tip]


class _Projection:
    """Metres on a floor, to a point on the panel."""

    def __init__(self, rooms, box):
        xs = [room["x"] for room in rooms] + [room["x"] + room["w"] for room in rooms]
        ys = [room["y"] for room in rooms] + [room["y"] + room["h"] for room in rooms]
        self.bounds = (min(xs), min(ys), max(xs), max(ys))
        self.centre = ((self.bounds[0] + self.bounds[2]) / 2, (self.bounds[1] + self.bounds[3]) / 2)
        self.span = max(self.bounds[2] - self.bounds[0], self.bounds[3] - self.bounds[1], 1.0)

        floors = sorted({room["floor"] for room in rooms})
        middle = (len(floors) - 1) / 2
        self.levels = {floor: (index - middle) * _FLOOR_GAP for index, floor in enumerate(floors)}

        # Fit everything that is drawn: the floors, the pins over them, and
        # the cameras' cones reaching out through the walls.
        corners = []
        pad = 0.3

        for floor in floors:
            for x in (self.bounds[0] - pad, self.bounds[2] + pad):
                for y in (self.bounds[1] - pad, self.bounds[3] + pad):
                    for z in (0.0, _PIN + 0.04):
                        corners.append(self._raw(x, y, self.levels[floor] + z))

        for room in rooms:
            for camera in room["cameras"]:
                for x, y in camera_reach(room, camera):
                    corners.append(self._raw(x, y, self.levels[room["floor"]]))

        low_x, high_x = min(p[0] for p in corners), max(p[0] for p in corners)
        low_y, high_y = min(p[1] for p in corners), max(p[1] for p in corners)
        self.scale = min(box.width() / max(1e-6, high_x - low_x), box.height() / max(1e-6, high_y - low_y))
        self.offset = (box.center().x() - (low_x + high_x) / 2 * self.scale,
                       box.center().y() - (low_y + high_y) / 2 * self.scale)

    def _raw(self, x, y, z):
        nx = (x - self.centre[0]) / self.span
        ny = (y - self.centre[1]) / self.span

        # Turned about the vertical, then looked down on.
        tx = nx * math.cos(_YAW) - ny * math.sin(_YAW)
        ty = nx * math.sin(_YAW) + ny * math.cos(_YAW)
        down = ty * math.sin(_ELEVATION) - z * math.cos(_ELEVATION)
        depth = _DISTANCE - (ty * math.cos(_ELEVATION) + z * math.sin(_ELEVATION))
        return tx / depth, down / depth, depth

    def point(self, x, y, floor, z=0.0):
        raw = self._raw(x, y, self.levels.get(floor, 0.0) + z)
        return QPointF(self.offset[0] + raw[0] * self.scale, self.offset[1] + raw[1] * self.scale)

    def depth(self, x, y, floor, z=0.0):
        return self._raw(x, y, self.levels.get(floor, 0.0) + z)[2]


class HousePanel(QWidget):
    """The projection. Fed a view by actions/house.py through show_view."""

    show_view = pyqtSignal(dict)
    hide_view = pyqtSignal()
    room_clicked = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()

        self._anchor = None
        self._view = None
        self._projection = None
        self._shown_at = 0.0
        self._flicker = 1.0
        self._drag_offset = None
        self._press = None
        self._room_shapes = []      # (floor, name, QPolygonF), for taps on the plan
        self._row_rects = {}        # name -> QRectF, for taps on the list
        self._layer = None
        self._layer_dirty = True
        self._layer_moving = False
        self._composed_at = 0.0

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
            x = max(screen.left() + 12, frame.left() - _WIDTH - _BEAM_GAP)
            y = frame.center().y() - _HEIGHT // 2
            y = max(screen.top() + 12, min(y, screen.bottom() - _HEIGHT - 12))
            self.move(int(x), int(y))
            return

        self.move(screen.left() + (screen.width() - _WIDTH) // 2, screen.top() + (screen.height() - _HEIGHT) // 2)

    # ---- what to show ---------------------------------------------------------------

    def _on_view(self, view):
        first = self._view is None or self._view.get("place") != view.get("place")
        self._view = dict(view)
        self._layer_dirty = True

        if first:
            self._shown_at = _now()

        rooms = view.get("rooms") or []
        self._projection = _Projection(rooms, self.plan_box()) if rooms else None

        if not self.isVisible():
            self._position()
            self.show()

        self._timer.start(_FRAME_MS)
        self.update()

    def _on_hide(self):
        self._timer.stop()
        self._view = None
        self.hide()

    def plan_box(self):
        return QRectF(_MARGIN, _HEADER + 4, _WIDTH - 2 * _MARGIN - _LIST_W - 12, _HEIGHT - _HEADER - _FOOT - 10)

    def list_box(self):
        return QRectF(_WIDTH - _MARGIN - _LIST_W, _HEADER + 6, _LIST_W, _HEIGHT - _HEADER - _FOOT - 14)

    def _moving(self, now):
        rising = now - self._shown_at < _RISE_FOR + _FLOOR_EACH * len((self._view or {}).get("floors") or [0])
        return rising

    def _rippling(self, now):
        return any(board["moved_at"] is not None and now - board["moved_at"] < _RIPPLE_FOR
                   for board in ((self._view or {}).get("boards") or {}).values())

    def _tick(self):
        now = _now()
        target = 0.97 + 0.03 * random.random()

        if random.random() < 0.01:
            target = 0.9

        self._flicker += (target - self._flicker) * 0.35
        wanted = _FRAME_MS if self._moving(now) or self._rippling(now) else _STEADY_MS

        if self._timer.interval() != wanted:
            self._timer.setInterval(wanted)

        self.update()

    # ---- taps -----------------------------------------------------------------------

    def room_at(self, point):
        """The room under [point]: the list first, then the plan's upper floors before the lower."""
        for name, rect in self._row_rects.items():
            if rect.contains(point):
                return name

        for _floor, name, shape in sorted(self._room_shapes, key=lambda item: -item[0]):
            if shape.containsPoint(point, Qt.FillRule.OddEvenFill):
                return name

        return None

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

        name = self.room_at(event.position())

        if name:
            self.room_clicked.emit(name)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    # ---- drawing ----------------------------------------------------------------------

    def paintEvent(self, event):
        if not self._view:
            return

        now = _now()
        moving = self._moving(now)

        if (self._layer_dirty or self._layer is None or moving or self._layer_moving
                or now - self._composed_at >= _COMPOSE_EVERY):
            self._compose(now)
            self._layer_moving = moving

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._flicker)
        painter.drawImage(QPointF(0, 0), self._layer)

        if self._projection is not None and not moving:
            self._paint_live(painter, now)

        painter.end()

    def _compose(self, now):
        ratio = max(1.0, self.devicePixelRatioF())
        size = QSize(int(_WIDTH * ratio), int(_HEIGHT * ratio))

        if self._layer is None or self._layer.size() != size:
            self._layer = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
            self._layer.setDevicePixelRatio(ratio)

        self._layer.fill(0)
        painter = QPainter(self._layer)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        self._paint_frame(painter)
        self._paint_header(painter)

        if self._projection is not None:
            self._paint_plan(painter, now)
        else:
            painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            painter.setPen(_tint(_DIM, 230))
            painter.drawText(self.plan_box(), Qt.AlignmentFlag.AlignCenter, "NO ROOMS IN HOUSE.JSON")

        self._paint_list(painter, now)
        self._paint_foot(painter)
        painter.end()

        self._layer_dirty = False
        self._composed_at = now

    def _paint_frame(self, painter):
        body = QRectF(self.rect()).adjusted(5, 5, -5, -5)
        path = QPainterPath()
        path.addRoundedRect(body, 16, 16)
        painter.fillPath(path, _BACKDROP)
        painter.setPen(QPen(_tint(_ACCENT, 110), 1.4))
        painter.drawPath(path)

        painter.setPen(QPen(_tint(_ACCENT, 170), 2.0))
        span = 20

        for x, y, dx, dy in ((body.left() + 10, body.top() + 10, 1, 1), (body.right() - 10, body.top() + 10, -1, 1),
                             (body.left() + 10, body.bottom() - 10, 1, -1), (body.right() - 10, body.bottom() - 10, -1, -1)):
            painter.drawLine(QPointF(x, y), QPointF(x + span * dx, y))
            painter.drawLine(QPointF(x, y), QPointF(x, y + span * dy))

        # The list's own panel, a shade lighter.
        box = self.list_box().adjusted(-6, -4, 4, 2)
        side = QPainterPath()
        side.addRoundedRect(box, 10, 10)
        painter.fillPath(side, QColor(10, 22, 34, 150))
        painter.setPen(QPen(_tint(_ACCENT, 45), 1.0))
        painter.drawPath(side)

    def _paint_header(self, painter):
        view = self._view
        title = QFont("Consolas", 12, QFont.Weight.Bold)
        title.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 118)
        small = QFont("Consolas", 7, QFont.Weight.Bold)
        title_m, small_m = QFontMetrics(title), QFontMetrics(small)

        painter.setFont(title)
        painter.setPen(_tint(_ACCENT, 240))
        left = _MARGIN + 18
        painter.drawText(QPointF(left, 34), "HOUSE")
        left += title_m.horizontalAdvance("HOUSE") + 14

        boards = [board for board in (view.get("boards") or {}).values() if not board["camera"]]
        online = sum(1 for board in boards if board["state"] in ("live", "quiet"))
        floors = len(view.get("floors") or [])
        rooms = len(view.get("rooms") or [])

        chip = f"{online} ONLINE" if online else "NO SENSORS ONLINE"
        chip_w = small_m.horizontalAdvance(chip) + 22
        right = _WIDTH - _MARGIN - 18
        chip_h = small_m.height() + 6
        chip_rect = QRectF(right - chip_w, 29 - chip_h / 2, chip_w, chip_h)
        shape = QPainterPath()
        shape.addRoundedRect(chip_rect, chip_h / 2, chip_h / 2)
        colour = _LIVE if online else _DIM
        painter.fillPath(shape, _tint(colour, 30))
        painter.setPen(QPen(_tint(colour, 170), 1.0))
        painter.drawPath(shape)
        painter.setBrush(_tint(colour, 240))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(chip_rect.left() + 9, chip_rect.center().y()), 2.6, 2.6)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setFont(small)
        painter.setPen(_tint(_TEXT, 235))
        painter.drawText(QPointF(chip_rect.left() + 16, chip_rect.center().y() + small_m.ascent() / 2 - 1), chip)

        words = f"{view.get('title', 'HOME')}  -  {rooms} ROOMS" + (f"  -  {floors} FLOORS" if floors > 1 else "")
        painter.setPen(_tint(_TEXT, 220))
        painter.drawText(QPointF(left, 33), fitting([words, view.get("title", "HOME")], small_m,
                                                   chip_rect.left() - 14 - left))

        painter.setPen(QPen(_tint(_ACCENT, 70), 1.0))
        painter.drawLine(QPointF(_MARGIN, _HEADER - 8), QPointF(_WIDTH - _MARGIN, _HEADER - 8))

    # ---- the plan -----------------------------------------------------------------------

    def _rise(self, now, floor_index):
        """How far a floor's walls have risen, 0 to 1."""
        share = (now - self._shown_at - floor_index * _FLOOR_EACH) / _RISE_FOR
        share = max(0.0, min(1.0, share))
        return 1 - (1 - share) ** 3

    def _polygon(self, points):
        return QPolygonF(points)

    def _paint_plan(self, painter, now):
        view, projection = self._view, self._projection
        focus = view.get("focus")
        rooms = view["rooms"]
        floors = sorted(projection.levels)
        focus_floor = next((room["floor"] for room in rooms if room["name"] == focus), None)
        self._room_shapes = []

        self._paint_risers(painter, rooms, floors)

        for index, floor in enumerate(floors):
            rise = self._rise(now, index)

            if rise <= 0:
                continue

            # A floor that is not the focused one steps back.
            presence = 1.0 if focus_floor is None or floor == focus_floor else 0.42
            painter.save()
            painter.setOpacity(presence * min(1.0, rise * 1.6))

            on_floor = [room for room in rooms if room["floor"] == floor]
            self._paint_footprint(painter, on_floor, floor)

            for room in on_floor:
                self._paint_floor(painter, room, room["name"] == focus)

            self._paint_walls(painter, on_floor, floor, rise, focus)

            for room in on_floor:
                self._paint_cameras(painter, room)

            for room in on_floor:
                self._paint_pins(painter, room, rise)

            for room in on_floor:
                self._paint_label(painter, room, room["name"] == focus)

            painter.restore()

    def _paint_risers(self, painter, rooms, floors):
        """Faint uprights at the corners, joining each floor to the one above."""
        if len(floors) < 2:
            return

        projection = self._projection
        low_x, low_y, high_x, high_y = projection.bounds
        painter.setPen(QPen(_tint(_ACCENT, 50), 1.0, Qt.PenStyle.DotLine))

        for lower, upper in zip(floors, floors[1:]):
            for x, y in ((low_x, low_y), (high_x, low_y), (high_x, high_y), (low_x, high_y)):
                painter.drawLine(projection.point(x, y, lower), projection.point(x, y, upper))

    def _paint_footprint(self, painter, rooms, floor):
        """The floor's plate: a faint glow under all its rooms."""
        projection = self._projection
        low_x = min(room["x"] for room in rooms) - 0.25
        low_y = min(room["y"] for room in rooms) - 0.25
        high_x = max(room["x"] + room["w"] for room in rooms) + 0.25
        high_y = max(room["y"] + room["h"] for room in rooms) + 0.25

        plate = self._polygon([projection.point(low_x, low_y, floor), projection.point(high_x, low_y, floor),
                               projection.point(high_x, high_y, floor), projection.point(low_x, high_y, floor)])
        path = QPainterPath()
        path.addPolygon(plate)
        path.closeSubpath()
        painter.fillPath(path, _tint(_ACCENT, 12))
        painter.setPen(QPen(_tint(_ACCENT, 60), 1.0, Qt.PenStyle.DashLine))
        painter.drawPath(path)

        # Floor number, at the plate's near left corner.
        painter.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
        painter.setPen(_tint(_ACCENT, 150))
        label = "GROUND" if floor == 0 else f"FLOOR {floor}"
        corners = [plate.at(index) for index in range(4)]
        left = min(corners, key=lambda point: point.x())
        width = painter.fontMetrics().horizontalAdvance(label)
        painter.drawText(QPointF(max(8.0, left.x() - width - 8), left.y() + 4), label)

    def _room_polygon(self, room, z=0.0):
        projection, floor = self._projection, room["floor"]
        x, y, w, h = room["x"], room["y"], room["w"], room["h"]
        return self._polygon([projection.point(x, y, floor, z), projection.point(x + w, y, floor, z),
                              projection.point(x + w, y + h, floor, z), projection.point(x, y + h, floor, z)])

    def _paint_floor(self, painter, room, lit):
        shape = self._room_polygon(room)
        self._room_shapes.append((room["floor"], room["name"], shape))
        temperature, _humidity, state, _moved = room_readings(self._view, room)

        path = QPainterPath()
        path.addPolygon(shape)
        path.closeSubpath()

        if temperature is not None and state in ("live", "quiet"):
            colour = temperature_colour(temperature)
            painter.fillPath(path, _tint(colour, 78 if lit else 50))
        elif room["boards"]:
            painter.fillPath(path, _tint(_ACCENT, 34 if lit else 20))
        else:
            painter.fillPath(path, _tint(_ACCENT, 26 if lit else 9))

        if lit:
            painter.setPen(QPen(_tint(_BRIGHT, 235), 2.0))
            painter.drawPath(path)

    def _wall_pieces(self, room):
        """Each wall of a room as (start, end, side, kind) along it, with doors left open."""
        x, y, w, h = room["x"], room["y"], room["w"], room["h"]
        sides = {"north": ((x, y), (x + w, y)), "east": ((x + w, y), (x + w, y + h)),
                 "south": ((x, y + h), (x + w, y + h)), "west": ((x, y), (x, y + h))}
        pieces = []

        for side, (start, end) in sides.items():
            length = math.dist(start, end)
            openings = sorted(
                (max(0.0, feature["at"] * length - feature["width"] / 2),
                 min(length, feature["at"] * length + feature["width"] / 2), feature["kind"])
                for feature in room["features"] if feature["wall"] == side)
            cursor = 0.0

            def along(distance):
                share = distance / length if length else 0.0
                return (start[0] + (end[0] - start[0]) * share, start[1] + (end[1] - start[1]) * share)

            for low, high, kind in openings:
                if low > cursor:
                    pieces.append((along(cursor), along(low), "wall"))

                if kind == "window":
                    pieces.append((along(low), along(high), "window"))

                cursor = max(cursor, high)

            if cursor < length:
                pieces.append((along(cursor), end, "wall"))

        return pieces

    def _paint_walls(self, painter, rooms, floor, rise, focus):
        projection = self._projection
        height = _WALL * rise
        pieces = []

        for room in rooms:
            for start, end, kind in self._wall_pieces(room):
                middle = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
                pieces.append((projection.depth(middle[0], middle[1], floor, height / 2), start, end, kind,
                               room["name"] == focus))

        # Far walls first, so the near ones stand in front of them.
        for _depth, start, end, kind, lit in sorted(pieces, key=lambda piece: -piece[0]):
            base_a, base_b = projection.point(*start, floor), projection.point(*end, floor)
            top_a, top_b = projection.point(*start, floor, height), projection.point(*end, floor, height)
            quad = QPainterPath()
            quad.addPolygon(self._polygon([base_a, base_b, top_b, top_a]))
            quad.closeSubpath()

            if kind == "window":
                painter.fillPath(quad, _tint(_GLASS, 70 if lit else 46))
                mid_a = projection.point(*start, floor, height * 0.55)
                mid_b = projection.point(*end, floor, height * 0.55)
                painter.setPen(QPen(_tint(_GLASS, 230), 1.6))
                painter.drawLine(mid_a, mid_b)
                painter.drawLine(top_a, top_b)
            else:
                painter.fillPath(quad, _tint(_ACCENT, 44 if lit else 24))
                painter.setPen(QPen(_tint(_BRIGHT if lit else _ACCENT, 240 if lit else 190), 1.5 if lit else 1.2))
                painter.drawLine(top_a, top_b)
                painter.setPen(QPen(_tint(_ACCENT, 90), 1.0))
                painter.drawLine(base_a, base_b)

    def _board_spot(self, room, board):
        return room["x"] + board["at"][0] * room["w"], room["y"] + board["at"][1] * room["h"]

    def _paint_pins(self, painter, room, rise):
        projection = self._projection

        for board in room["boards"]:
            seen = self._view["boards"].get(board["name"])
            colour = _STATE_COLOUR.get(seen["state"] if seen else "waiting", _WAITING)
            x, y = self._board_spot(room, board)
            base = projection.point(x, y, room["floor"])
            top = projection.point(x, y, room["floor"], _PIN * rise)

            painter.setPen(QPen(_tint(colour, 90), 1.0))
            painter.drawEllipse(base, 5, 2.6)
            painter.setPen(QPen(_tint(colour, 200), 1.3))
            painter.drawLine(base, top)

            diamond = QPainterPath()
            diamond.moveTo(top.x(), top.y() - 5)
            diamond.lineTo(top.x() + 4, top.y())
            diamond.lineTo(top.x(), top.y() + 5)
            diamond.lineTo(top.x() - 4, top.y())
            diamond.closeSubpath()
            painter.fillPath(diamond, _tint(colour, 245))

    def _camera_cone(self, room, camera):
        apex, _left, _right, tip = camera_reach(room, camera)
        angle = math.atan2(tip[1] - apex[1], tip[0] - apex[0])
        return apex, tip, angle

    def _paint_cameras(self, painter, room):
        projection = self._projection

        for camera in room["cameras"]:
            seen = self._view["boards"].get(camera["name"])
            online = bool(seen and seen["state"] in ("live", "quiet"))
            apex, tip, angle = self._camera_cone(room, camera)
            reach, spread = _CONE_REACH, _CONE_SPREAD

            points = [projection.point(*apex, room["floor"], _WALL * 0.6)]

            for step in range(9):
                turn = angle - spread + 2 * spread * step / 8
                points.append(projection.point(apex[0] + reach * math.cos(turn), apex[1] + reach * math.sin(turn),
                                               room["floor"]))

            cone = QPainterPath()
            cone.addPolygon(self._polygon(points))
            cone.closeSubpath()
            start, end = points[0], projection.point(*tip, room["floor"])
            glow = QLinearGradient(start, end)
            glow.setColorAt(0.0, _tint(_GLASS, 110 if online else 45))
            glow.setColorAt(1.0, _tint(_GLASS, 0))
            painter.fillPath(cone, glow)

            painter.setBrush(_tint(_LIVE if online else _WAITING, 240))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(start, 3.2, 3.2)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            label = (camera["looks"] or "camera").upper()
            painter.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
            metrics = painter.fontMetrics()
            painter.setPen(_tint(_GLASS, 220 if online else 140))
            painter.drawText(QPointF(end.x() - metrics.horizontalAdvance(label) / 2, end.y()), label)
            state = "CAM ONLINE" if online else "CAM NOT CONNECTED"
            painter.setPen(_tint(_DIM, 200))
            painter.drawText(QPointF(end.x() - metrics.horizontalAdvance(state) / 2, end.y() + 11), state)

    def _paint_label(self, painter, room, lit):
        """The room's name and readings, standing upright over its middle."""
        projection = self._projection
        centre = projection.point(room["x"] + room["w"] / 2, room["y"] + room["h"] / 2, room["floor"])
        corners = self._room_polygon(room).boundingRect()
        room_w = max(40.0, corners.width() * 0.9)
        temperature, humidity, state, _moved = room_readings(self._view, room)

        name_font = QFont("Consolas", 8, QFont.Weight.Bold)
        name_m = QFontMetrics(name_font)
        name = fitting([room["name"].upper(), room["name"].upper()[:10]], name_m, room_w)

        lines = [(name, name_font, _tint(_BRIGHT if lit else _TEXT, 245 if (lit or room["boards"]) else 150))]

        if temperature is not None and state in ("live", "quiet"):
            value_font = QFont("Consolas", 11, QFont.Weight.Bold)
            lines.append((f"{temperature:.1f}{_DEGREE}", value_font, temperature_colour(temperature)))

            if humidity is not None:
                lines.append((f"{humidity:.0f}% RH", QFont("Consolas", 7, QFont.Weight.Bold), _tint(_DIM, 230)))
        elif state == "offline":
            lines.append(("OFFLINE", QFont("Consolas", 7, QFont.Weight.Bold), _tint(_GONE, 230)))
        elif state == "waiting":
            lines.append(("AWAITING SENSOR", QFont("Consolas", 7, QFont.Weight.Bold), _tint(_WAITING, 220)))

        heights = [QFontMetrics(font).height() for _text, font, _colour in lines]
        y = centre.y() - sum(heights) / 2

        for (text, font, colour), line_h in zip(lines, heights):
            metrics = QFontMetrics(font)
            painter.setFont(font)
            painter.setPen(colour)
            painter.drawText(QPointF(centre.x() - metrics.horizontalAdvance(text) / 2, y + metrics.ascent()), text)
            y += line_h

    # ---- what moves, each frame ------------------------------------------------------------

    def _paint_live(self, painter, now):
        view, projection = self._view, self._projection
        focus = view.get("focus")
        focus_floor = next((room["floor"] for room in view["rooms"] if room["name"] == focus), None)

        # The scan line, crossing each floor from north to south.
        low_x, low_y, high_x, high_y = projection.bounds
        share = (now % _SWEEP_EVERY) / _SWEEP_EVERY

        for floor in projection.levels:
            if focus_floor is not None and floor != focus_floor:
                continue

            for trail, alpha in ((0.0, 120), (0.025, 55), (0.05, 22)):
                y = low_y + (high_y - low_y) * max(0.0, share - trail)
                painter.setPen(QPen(_tint(_ACCENT, alpha), 1.4))
                painter.drawLine(projection.point(low_x, y, floor), projection.point(high_x, y, floor))

        pulse = 0.5 + 0.5 * math.sin(now * 3.0)

        for room in view["rooms"]:
            for board in room["boards"]:
                seen = view["boards"].get(board["name"])

                if not seen or seen["state"] not in ("live", "quiet"):
                    continue

                x, y = self._board_spot(room, board)
                top = projection.point(x, y, room["floor"], _PIN)
                colour = _STATE_COLOUR[seen["state"]]
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tint(colour, 40 + 50 * pulse))
                painter.drawEllipse(top, 7 + 3 * pulse, 7 + 3 * pulse)
                painter.setBrush(Qt.BrushStyle.NoBrush)

                # Movement: rings across the floor from the pin.
                if seen["moved_at"] is not None and now - seen["moved_at"] < _RIPPLE_FOR:
                    age = (now - seen["moved_at"]) / _RIPPLE_FOR

                    for offset in (0.0, 0.33, 0.66):
                        phase = (age * 2.2 + offset) % 1.0
                        radius = 0.15 + 1.4 * phase
                        ring = [projection.point(x + radius * math.cos(turn), y + radius * math.sin(turn), room["floor"])
                                for turn in (2 * math.pi * step / 28 for step in range(28))]
                        painter.setPen(QPen(_tint(_LIVE, 200 * (1 - phase) * (1 - age)), 1.6))
                        painter.drawPolygon(self._polygon(ring))

    # ---- the list ------------------------------------------------------------------------------

    def listed(self):
        """Rooms with sensors first, then the rest, then boards not on the plan."""
        rooms = self._view.get("rooms") or []
        ordered = sorted(rooms, key=lambda room: (not room["boards"], -room["floor"], room["name"].casefold()))
        return ordered

    def _paint_list(self, painter, now):
        box = self.list_box()
        view = self._view
        focus = view.get("focus")
        self._row_rects = {}

        small = QFont("Consolas", 7, QFont.Weight.Bold)
        name_font = QFont("Consolas", 9, QFont.Weight.Bold)
        value_font = QFont("Consolas", 10, QFont.Weight.Bold)
        small_m, name_m, value_m = QFontMetrics(small), QFontMetrics(name_font), QFontMetrics(value_font)

        painter.setFont(small)
        painter.setPen(_tint(_ACCENT, 200))
        painter.drawText(QPointF(box.left() + 6, box.top() + small_m.ascent() + 2), "ROOMS")
        top = box.top() + small_m.height() + 8
        bottom = box.bottom() - small_m.height() - 8
        rise = self._rise(now, 0)
        hidden = 0

        # Heights from the fonts, so display scaling grows the rows rather
        # than piling the lines on each other.
        plain_h = 6 + name_m.height() + value_m.height() + 6
        focus_h = plain_h + small_m.height() + 2

        for index, room in enumerate(self.listed()):
            lit = room["name"] == focus
            height = focus_h if lit else plain_h

            if top + height > bottom:
                hidden += 1
                continue

            rect = QRectF(box.left(), top, box.width(), height - 5)
            self._row_rects[room["name"]] = rect
            appear = max(0.0, min(1.0, rise * 1.4 - index * 0.08))
            temperature, humidity, state, moved = room_readings(view, room)

            painter.save()
            painter.setOpacity(appear)
            painter.translate((1 - appear) * 12, 0)

            shape = QPainterPath()
            shape.addRoundedRect(rect, 7, 7)
            colour = temperature_colour(temperature) if temperature is not None and state in ("live", "quiet") else _ACCENT
            painter.fillPath(shape, _tint(colour, 48 if lit else (22 if room["boards"] else 10)))
            painter.setPen(QPen(_tint(_BRIGHT if lit else colour, 230 if lit else 80), 1.4 if lit else 1.0))
            painter.drawPath(shape)

            # A bar down the left in the room's colour.
            painter.fillRect(QRectF(rect.left() + 5, rect.top() + 7, 2.5, rect.height() - 14),
                             _tint(colour, 220 if room["boards"] else 60))

            text_left = rect.left() + 14
            dot_x = rect.right() - 11

            if state:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tint(_STATE_COLOUR[state], 240))
                painter.drawEllipse(QPointF(dot_x, rect.top() + 12), 3.2, 3.2)
                painter.setBrush(Qt.BrushStyle.NoBrush)

            painter.setFont(name_font)
            painter.setPen(_tint(_TEXT, 240 if room["boards"] else 150))
            name = fitting([room["name"].upper()], name_m, rect.width() - 34)
            painter.drawText(QPointF(text_left, rect.top() + 6 + name_m.ascent()), name)

            second = rect.top() + 4 + name_m.height() + value_m.ascent()

            if temperature is not None and state in ("live", "quiet"):
                reading = f"{temperature:.1f}{_DEGREE}C"
                painter.setFont(value_font)
                painter.setPen(temperature_colour(temperature))
                painter.drawText(QPointF(text_left, second), reading)

                if humidity is not None:
                    painter.setFont(small)
                    painter.setPen(_tint(_DIM, 230))
                    painter.drawText(QPointF(text_left + value_m.horizontalAdvance(reading) + 8, second),
                                     f"{humidity:.0f}% RH")
            else:
                words = {"offline": "SENSOR OFFLINE", "waiting": "AWAITING SENSOR"}.get(state, "NO SENSOR")
                if room["cameras"] and not room["boards"]:
                    words = "CAMERA ONLY"
                painter.setFont(small)
                painter.setPen(_tint(_STATE_COLOUR.get(state, _DIM), 210))
                painter.drawText(QPointF(text_left, second), words)

            if lit:
                painter.setFont(small)
                painter.setPen(_tint(_DIM, 230))
                details = []

                if moved is not None:
                    minutes = int((now - moved) // 60)
                    details.append("MOVEMENT NOW" if minutes < 1 else f"MOVEMENT {minutes}M AGO")

                if room["cameras"]:
                    details.append(f"{len(room['cameras'])} CAMERA{'S' if len(room['cameras']) != 1 else ''}")

                floor = "GROUND FLOOR" if room["floor"] == 0 else f"FLOOR {room['floor']}"
                details.append(floor)
                options = [" - ".join(details[:count]) for count in range(len(details), 0, -1)]
                painter.drawText(QPointF(text_left, rect.top() + 4 + name_m.height() + value_m.height() + small_m.ascent()),
                                 fitting(options, small_m, rect.width() - 22))

            painter.restore()
            top += height

        unplaced = view.get("unplaced") or []
        notes = []

        if hidden:
            notes.append(f"+{hidden} MORE")

        if unplaced:
            notes.append(f"{len(unplaced)} NOT ON THE PLAN")

        # The legend, or what is missing from the list.
        painter.setFont(small)
        y = box.bottom() - 4

        if notes:
            painter.setPen(_tint(_QUIET, 220))
            painter.drawText(QPointF(box.left() + 6, y), fitting([" - ".join(notes)] + notes, small_m, box.width() - 10))
            return

        x = box.left() + 6

        for label, colour in (("LIVE", _LIVE), ("QUIET", _QUIET), ("OFFLINE", _GONE)):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_tint(colour, 230))
            painter.drawEllipse(QPointF(x + 3, y - small_m.ascent() / 2 + 1), 2.8, 2.8)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_tint(_DIM, 220))
            painter.drawText(QPointF(x + 10, y), label)
            x += 10 + small_m.horizontalAdvance(label) + 12

    def _paint_foot(self, painter):
        small = QFont("Consolas", 7, QFont.Weight.Bold)
        small_m = QFontMetrics(small)
        painter.setFont(small)
        painter.setPen(_tint(_DIM, 200))
        hint = fitting(hints(self._view), small_m, _WIDTH - 2 * (_MARGIN + 18))
        painter.drawText(QPointF((_WIDTH - small_m.horizontalAdvance(hint)) / 2, _HEIGHT - 16), hint)
