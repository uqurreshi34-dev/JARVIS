"""The sensor panel: what the boards on the wifi can feel, docked to the HUD.

It sits on top of the HUD, the same width, and slides up out of it when a
board first reports -- no beam, because it is part of the HUD rather than
something projected from it. When every board has gone quiet it slides
back down and gets out of the way. A click folds it to its title strip
(still showing the room's temperature); another click opens it again.

One row per board, most recently heard first:

    ROOM    21.4 C  (with its last half hour as a line)   48% RH   PRESENT

Nothing here fetches anything. actions/sensors.py records each report and
hands known() to the panel through a signal; the panel keeps a short
history of temperatures for the trend line and works the ages out on its
own clock, so "seen 12s ago" counts on between reports.
"""

import math
import time
from collections import deque

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

import hud as _hud
from actions import sensors


_WIDTH = 460                # the HUD's own width, so the two read as one
_HEADER = 34                # the title strip: all that shows when folded
_ROW = 82
_MAX_ROWS = 3               # three rows is the HUD's own height; more are counted
_FOOT = 12
_HEADER_INSET = 38          # clear of the corner brackets
_GAP = -4                   # overlap the HUD's transparent margin, so they touch

# A board reports every 30 seconds. Past two and a half of those it has
# missed some and is shown dimmed; past four it counts as gone. With every
# board gone, the panel slides away.
_STALE_SECONDS = 75.0
_GONE_SECONDS = 120.0

# How long after a movement the room's presence mark still pulses.
_MOTION_PULSE_SECONDS = 4.0

_HISTORY = 60               # temperatures kept per board: half an hour at 30 s

_SLIDE = 0.2                # share of the remaining distance covered per frame
_FRAME_MS = 16

_DEGREE = "\u00b0"

_ACCENT = QColor(95, 165, 205)
_TEXT = QColor(205, 235, 250)
_DIM = QColor(120, 145, 165)
_HUMID = QColor(80, 215, 255)
_PRESENT = QColor(95, 255, 160)
_QUIET = QColor(255, 196, 80)
_GONE = QColor(255, 88, 88)


def _now():
    return time.monotonic()


def temperature_colour(celsius):
    """Blue when cold, cyan when comfortable, amber then red when hot."""
    stops = ((10.0, QColor(90, 140, 255)), (18.0, QColor(80, 215, 255)),
             (24.0, QColor(95, 255, 195)), (28.0, QColor(255, 196, 80)),
             (32.0, QColor(255, 88, 88)))

    if celsius <= stops[0][0]:
        return QColor(stops[0][1])

    for (low, first), (high, second) in zip(stops, stops[1:]):
        if celsius <= high:
            share = (celsius - low) / (high - low)
            return QColor(
                int(first.red() + (second.red() - first.red()) * share),
                int(first.green() + (second.green() - first.green()) * share),
                int(first.blue() + (second.blue() - first.blue()) * share),
            )

    return QColor(stops[-1][1])


def ago(seconds):
    """12s, 4m, 2h: short, because it sits under a name."""
    seconds = max(0, int(seconds))

    if seconds < 60:
        return f"{seconds}s"

    if seconds < 3600:
        return f"{seconds // 60}m"

    return f"{seconds // 3600}h"


def fitting(options, metrics, width):
    """The first of [options] that fits [width]; the last one cut short if none does.

    Longest first, so display scaling shortens the words ("SEEN 12s AGO",
    then "SEEN 12s", then "12s") rather than cutting them off mid-word.
    """
    for words in options:
        if metrics.horizontalAdvance(words) <= width:
            return words

    return metrics.elidedText(options[-1], Qt.TextElideMode.ElideRight, int(max(0, width)))


def shrunk(words, family, size, smallest, width, weight=QFont.Weight.Bold):
    """A font for [words] at [size], made smaller, to [smallest], until they fit."""
    while True:
        font = QFont(family, size, weight)

        if size <= smallest or QFontMetrics(font).horizontalAdvance(words) <= width:
            return font

        size -= 1


class SensorPanel(QWidget):
    """Frameless, docked above (or, with no room, below) the HUD."""

    # known() from actions/sensors.py, after each report.
    updated = pyqtSignal(dict)
    hide_requested = pyqtSignal()

    def __init__(self):
        super().__init__()

        self._anchor = None
        self._accent = QColor(_ACCENT)
        self._boards = {}           # name -> {"seen", "moved", "readings", "history"}
        self._folded = False
        self._below = False         # docked under the HUD, when there is no room above
        self._reveal = 0.0          # pixels showing, animated toward _target()
        self._phase = 0.0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_WIDTH, self._full_height())

        self.updated.connect(self._on_updated)
        self.hide_requested.connect(self._on_hide_requested)

        # Fast while sliding; once still, only often enough to count the
        # ages on and notice a board going quiet.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---------------------------------------------------------- docking

    def set_anchor(self, hud):
        self._anchor = hud
        hud.moved.connect(self._dock)
        hud.state_changed.connect(self._on_state)

    def _on_state(self, state):
        # The frame takes the HUD's colour, so the two change together.
        self._accent = QColor(_hud._PALETTE.get(state, _ACCENT))
        self.update()

    def _full_height(self):
        rows = max(1, min(_MAX_ROWS, len(self._boards)))
        return _HEADER + rows * _ROW + _FOOT

    def _dock(self):
        if self._anchor is None:
            return

        frame = self._anchor.frameGeometry()
        screen = self._anchor.screen().availableGeometry()
        height = self.height()

        # Above the HUD where it fits; a HUD dragged to the top of the
        # screen has the panel hang below it instead.
        self._below = frame.top() - height - _GAP < screen.top()

        y = frame.bottom() + 1 + _GAP if self._below else frame.top() - height - _GAP
        self.move(frame.left(), int(y))

    # ---------------------------------------------------------- state

    def _on_updated(self, known):
        now = _now()

        for name, entry in (known or {}).items():
            board = self._boards.setdefault(
                name, {"seen": now, "moved": None, "read": None, "readings": {},
                       "history": deque(maxlen=_HISTORY)})

            board["seen"] = now - float(entry.get("seen_ago") or 0.0)

            moved_ago = entry.get("moved_ago")
            board["moved"] = None if moved_ago is None else now - float(moved_ago)

            # A new reading goes on the trend line; a motion report, which
            # carries the old readings along with it, does not.
            readings = dict(entry.get("readings") or {})
            read_ago = entry.get("read_ago")
            read = None if read_ago is None else now - float(read_ago)

            if read is not None and "temperature" in readings and (
                    board.get("read") is None or read > board["read"] + 0.5):
                board["history"].append(readings["temperature"])
                board["read"] = read

            board["readings"] = readings

        if not self._showing() and self._alive():
            # A fresh start: whatever it was folded to last time, it opens.
            self._folded = False

        self._resize()
        self._wake()

    def _on_hide_requested(self):
        self._boards.clear()
        self._reveal = 0.0
        self._timer.stop()
        self.hide()

    def _alive(self, now=None):
        now = _now() if now is None else now
        return any(now - board["seen"] < _GONE_SECONDS for board in self._boards.values())

    def _showing(self):
        return self.isVisible() and self._reveal > 0.5

    def _target(self):
        if not self._alive():
            return 0.0

        return float(_HEADER if self._folded else self.height())

    def _resize(self):
        height = self._full_height()

        if height != self.height():
            self.setFixedSize(_WIDTH, height)

        self._dock()

    def _wake(self):
        if not self.isVisible() and self._target() > 0:
            self._dock()
            self.show()

        self._timer.start(_FRAME_MS)
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.08) % (2 * math.pi)

        target = self._target()
        gap = target - self._reveal

        if abs(gap) < 0.6:
            self._reveal = target
        else:
            self._reveal += gap * _SLIDE

        if self._reveal <= 0.0 and target <= 0.0:
            self._timer.stop()
            self.hide()
            return

        # Still and nothing pulsing: slow down to a few frames a second.
        still = self._reveal == target
        pulsing = any(board["moved"] is not None and _now() - board["moved"] < _MOTION_PULSE_SECONDS
                      for board in self._boards.values())
        interval = _FRAME_MS if not still or pulsing else 500

        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

        self.update()

    # ---------------------------------------------------------- clicks

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._alive():
            self._folded = not self._folded
            self._wake()

    # ---------------------------------------------------------- drawing

    def _ordered(self):
        now = _now()
        return sorted(self._boards.items(), key=lambda item: now - item[1]["seen"])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        shown = self._reveal
        height = self.height()

        # Slide: the whole panel is drawn, pushed back into the HUD by
        # however much has not come out yet, so it rises from the HUD's
        # edge rather than fading in on top of it. Folded, only the title
        # strip has come out.
        painter.setClipRect(QRectF(0, 0 if self._below else height - shown, self.width(), shown))

        if self._below:
            painter.translate(0, shown - height)
            header_top = height - _HEADER
        else:
            painter.translate(0, height - shown)
            header_top = 0

        self._paint_frame(painter)
        self._paint_header(painter, header_top)

        rows_top = _FOOT if self._below else _HEADER
        now = _now()

        for index, (name, board) in enumerate(self._ordered()[:_MAX_ROWS]):
            self._paint_row(painter, name, board, rows_top + index * _ROW, now)

        painter.end()

    def _tint(self, colour, alpha):
        colour = QColor(colour)
        colour.setAlpha(alpha)
        return colour

    def _paint_frame(self, painter):
        body = QRectF(self.rect().adjusted(5, 5, -5, -5))
        path = QPainterPath()
        path.addRoundedRect(body, 18, 18)

        painter.fillPath(path, QColor(7, 12, 19, 222))
        painter.setPen(QPen(self._tint(self._accent, 95), 1.5))
        painter.drawPath(path)

        painter.setPen(QPen(self._tint(self._accent, 150), 2.0))
        span = 18

        for x, y, dx, dy in (
            (body.left() + 10, body.top() + 10, 1, 1),
            (body.right() - 10, body.top() + 10, -1, 1),
            (body.left() + 10, body.bottom() - 10, 1, -1),
            (body.right() - 10, body.bottom() - 10, -1, -1),
        ):
            painter.drawLine(int(x), int(y), int(x + span * dx), int(y))
            painter.drawLine(int(x), int(y), int(x), int(y + span * dy))

    def header_text(self):
        """SENSORS, how many are live, and the latest room's temperature."""
        now = _now()
        live = [b for b in self._boards.values() if now - b["seen"] < _GONE_SECONDS]
        words = f"SENSORS  {len(live)} ONLINE"

        if len(self._boards) > _MAX_ROWS:
            words += f"  +{len(self._boards) - _MAX_ROWS}"

        return words

    def _paint_header(self, painter, top):
        font = QFont("Consolas", 9, QFont.Weight.Bold)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        line = top + _HEADER / 2 + metrics.ascent() / 2 - 1
        painter.setPen(self._tint(self._accent, 230))
        painter.drawText(QPointF(_HEADER_INSET, line), self.header_text())

        # Folded, the strip still says the thing most worth a glance.
        ordered = self._ordered()
        summary = None

        if ordered:
            name, board = ordered[0]
            temperature = board["readings"].get("temperature")
            if temperature is not None:
                summary = (name.upper(), f"{temperature:.1f}{_DEGREE}C")

        hint = "CLICK TO OPEN" if self._folded else "CLICK TO FOLD"
        small = QFont("Consolas", 7, QFont.Weight.Bold)
        small_metrics = QFontMetrics(small)

        right = self.width() - _HEADER_INSET
        left_edge = _HEADER_INSET + metrics.horizontalAdvance(self.header_text()) + 16
        hint_width = small_metrics.horizontalAdvance(hint) + 14

        # The reading matters more than the hint: short of room, the hint
        # goes first, then the room's name.
        options = [summary[0] + " " + summary[1], summary[1]] if summary else []
        room = right - left_edge
        show_hint = not options or metrics.horizontalAdvance(options[0]) <= room - hint_width

        if show_hint:
            painter.setFont(small)
            painter.setPen(self._tint(_DIM, 200))
            painter.drawText(QPointF(right - small_metrics.horizontalAdvance(hint), line), hint)
            right -= hint_width

        if options:
            words = fitting(options, metrics, right - left_edge)
            painter.setFont(font)
            painter.setPen(_TEXT)
            painter.drawText(QPointF(right - metrics.horizontalAdvance(words), line), words)

        # A rule under the title, above the rows (over them when docked below).
        painter.setPen(QPen(self._tint(self._accent, 70), 1.0))
        rule = top + (_HEADER - 2 if not self._below else 2)
        painter.drawLine(QPointF(22, rule), QPointF(self.width() - 22, rule))

    def row_state(self, board, now=None):
        now = _now() if now is None else now
        silent = now - board["seen"]

        if silent >= _GONE_SECONDS:
            return "OFFLINE"

        if silent >= _STALE_SECONDS:
            return "QUIET"

        return "LIVE"

    def presence(self, board, now=None):
        now = _now() if now is None else now

        if board["moved"] is None:
            return "NO MOTION"

        return "PRESENT" if now - board["moved"] < sensors.PRESENT_SECONDS else "EMPTY"

    def _paint_row(self, painter, name, board, top, now):
        state = self.row_state(board, now)
        dim = state != "LIVE"
        fade = 120 if dim else 255
        readings = board["readings"]

        # Columns: name, temperature (with its trend), humidity, presence.
        name_x, temp_x, humid_x, presence_x = 26, 136, 268, 344
        right = self.width() - 26

        # --- name and when it was last heard
        name_room = temp_x - name_x - 26
        big = shrunk(name.upper(), "Consolas", 10, 7, name_room)
        small = QFont("Consolas", 7, QFont.Weight.Bold)
        big_m, small_m = QFontMetrics(big), QFontMetrics(small)

        dot = {"LIVE": _PRESENT, "QUIET": _QUIET, "OFFLINE": _GONE}[state]
        pulse = 0.5 + 0.5 * math.sin(self._phase) if state == "LIVE" else 0.6
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._tint(dot, int(140 + 110 * pulse)))
        painter.drawEllipse(QPointF(name_x + 4, top + 22), 3.5, 3.5)

        painter.setFont(big)
        painter.setPen(self._tint(_TEXT, fade))
        label = fitting([name.upper()], big_m, name_room)
        name_line = top + 22 + big_m.ascent() / 2 - 1
        painter.drawText(QPointF(name_x + 14, name_line), label)

        painter.setFont(small)
        painter.setPen(self._tint(dot if dim else _DIM, 220))
        silent = ago(now - board["seen"])
        options = [f"{state} {silent}", state] if dim else [f"SEEN {silent} AGO", f"SEEN {silent}", silent]
        heard = fitting(options, small_m, temp_x - name_x - 12)
        painter.drawText(QPointF(name_x, max(top + 44, name_line + big_m.descent() + small_m.ascent() + 5)), heard)

        # --- temperature, coloured by how it feels, and its trend
        temperature = readings.get("temperature")
        value_font = QFont("Consolas", 16, QFont.Weight.Bold)
        value_m = QFontMetrics(value_font)

        if temperature is None:
            painter.setFont(small)
            painter.setPen(self._tint(_DIM, fade))
            painter.drawText(QPointF(temp_x, top + 28), "NO READING")
        else:
            colour = temperature_colour(temperature)
            words = f"{temperature:.1f}{_DEGREE}"
            painter.setFont(value_font)
            painter.setPen(self._tint(colour, fade))
            painter.drawText(QPointF(temp_x, top + 32), words)

            painter.setFont(small)
            painter.drawText(QPointF(temp_x + value_m.horizontalAdvance(words) + 2, top + 32), "C")

            self._paint_trend(painter, board["history"], QRectF(temp_x, top + 42, humid_x - temp_x - 22, 18),
                              self._tint(colour, fade))

        # --- humidity, as a ring filled to the percentage
        humidity = readings.get("humidity")
        centre = QPointF(humid_x + 22, top + 34)
        radius = 19.0

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._tint(_HUMID, 45), 3.0))
        painter.drawEllipse(centre, radius, radius)

        if humidity is not None:
            share = max(0.0, min(1.0, humidity / 100.0))
            painter.setPen(QPen(self._tint(_HUMID, fade), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            box = QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2)
            painter.drawArc(box, 90 * 16, int(-share * 360 * 16))

        painter.setFont(small)
        painter.setPen(self._tint(_TEXT, fade))
        words = f"{humidity:.0f}%" if humidity is not None else "--"
        painter.drawText(QPointF(centre.x() - small_m.horizontalAdvance(words) / 2, centre.y() + small_m.ascent() / 2 - 1), words)
        painter.setPen(self._tint(_DIM, fade))
        painter.drawText(QPointF(centre.x() - small_m.horizontalAdvance("RH") / 2, top + 68), "RH")

        # --- presence: rings that spread out from the room as someone moves
        where = self.presence(board, now)
        colour = _PRESENT if where == "PRESENT" else _DIM
        mark = QPointF(presence_x + 12, top + 30)

        if board["moved"] is not None and now - board["moved"] < _MOTION_PULSE_SECONDS:
            age = (now - board["moved"]) / _MOTION_PULSE_SECONDS
            for step in range(3):
                spread = (age * 1.5 + step / 3.0) % 1.0
                painter.setPen(QPen(self._tint(_PRESENT, int(200 * (1 - spread))), 1.4))
                painter.drawEllipse(mark, 4 + 14 * spread, 4 + 14 * spread)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._tint(colour, fade))
        painter.drawEllipse(mark, 4.0, 4.0)

        painter.setFont(small)
        painter.setPen(self._tint(colour, fade))
        room = right - presence_x
        painter.drawText(QPointF(presence_x, top + 56), small_m.elidedText(where, Qt.TextElideMode.ElideRight, room))

        if board["moved"] is not None:
            painter.setPen(self._tint(_DIM, fade))
            since = ago(now - board["moved"])
            moved = fitting([f"MOVED {since} AGO", f"MOVED {since}", f"{since} AGO"], small_m, room)
            painter.drawText(QPointF(presence_x, max(top + 68, top + 56 + small_m.height())), moved)

        # A faint rule between rows.
        painter.setPen(QPen(self._tint(self._accent, 28), 1.0))
        painter.drawLine(QPointF(26, top + _ROW - 1), QPointF(right, top + _ROW - 1))

    def _paint_trend(self, painter, history, box, colour):
        values = list(history)

        if len(values) < 2:
            return

        low, high = min(values), max(values)
        span = max(high - low, 0.5)     # half a degree fills it, so noise stays flat
        middle = (high + low) / 2

        points = []
        for index, value in enumerate(values):
            x = box.left() + box.width() * index / (len(values) - 1)
            y = box.center().y() - (value - middle) / span * box.height()
            points.append(QPointF(x, y))

        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(colour, 1.3))
        painter.drawPath(path)
