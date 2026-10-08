"""The trader board JARVIS projects: who was examined, who held up, and the working behind every verdict.

Two views in one scrollable page, built like the other panels -- frameless,
translucent, anchored beside the HUD with the beam crossing the gap:

    the board     every trader examined, consistent records first: verdict,
                  how they trade and what, profit over the period, win
                  rate, profit factor, deepest fall, a sparkline of the
                  profit curve and one bar per part of the period, green or
                  red, so consistency is seen at a glance
    one trader    how they trade (held for how long, which way, how they
                  build and cut positions) and the markets they trade; the
                  profit curve with its deepest fall shaded, the parts as
                  bars, every closed trade as a dot, the calculations
                  written out with their numbers, and open positions with
                  their leverage and how close each is to liquidation, with
                  a key to the colours

Click a row (or Up, Down and Enter) to open a trader; Back, Escape or
Backspace returns; Left and Right step between traders; the wheel scrolls.
Everything arriving from elsewhere comes in through a signal: this is
driven from the command thread, and a Qt widget may only be touched from
its own.
"""

from datetime import datetime, timezone

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPicture
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget


_WIDTH = 760
_HEIGHT = 600
_MARGIN = 22

_BACKDROP = QColor(8, 14, 21, 238)
_ACCENT = QColor(95, 200, 245)
_TEXT = QColor(226, 236, 245)
_MUTED = QColor(130, 148, 165)
_GRID = QColor(95, 200, 245, 34)
_GAIN = QColor(70, 205, 135)
_LOSS = QColor(235, 95, 95)
_AMBER = QColor(235, 185, 80)

_VERDICT_COLOURS = {"consistent": _GAIN, "one big trade": _AMBER, "inconsistent": _LOSS, "too few": _MUTED}

# How far price is from liquidating a position: the bar is full at this distance, and its colour.
_LIQUIDATION_FULL = 0.5
_SAFE_DISTANCE = 0.25
_CLOSE_DISTANCE = 0.1

_DAY_MS = 86_400_000

_BEAM_GAP = 74
_FRAME_MS = 33
_ROW = 88
_FIGURES = 58         # the height of a row's name, badge and figures; the profile line sits beneath

_CONTROLS = """
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(95, 200, 245, 90); border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QPushButton {
    background: rgba(95, 200, 245, 30); border: 1px solid rgba(95, 200, 245, 110); border-radius: 4px;
    color: #e2ecf5; padding: 3px 10px;
}
QPushButton:hover { background: rgba(95, 200, 245, 60); }
QPushButton:disabled { color: #5d6b78; border-color: rgba(95, 200, 245, 40); }
"""


def _money(value):
    sign = "-" if value < 0 else ""
    value = abs(value)

    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:.2f}m"

    if value >= 10_000:
        return f"{sign}${value / 1000:.1f}k"

    return f"{sign}${value:,.0f}"


def _name(trader):
    if trader.get("name"):
        return trader["name"]

    address = trader.get("address", "")
    return f"{address[:6]}...{address[-4:]}" if len(address) > 12 else address


def _date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d %b")


def _liquidation_colour(distance):
    return _LOSS if distance < _CLOSE_DISTANCE else _AMBER if distance < _SAFE_DISTANCE else _GAIN


def _profile(trader):
    """A row's one-line summary: how long they hold, which way, their wins and losses, and the markets they trade most."""
    habits = [name for name, _detail in (trader.get("style") or [])[:3]]
    markets = [name for name, _count, _net in (trader.get("coins") or [])[:3]]
    return "  -  ".join(habits + ([" ".join(markets)] if markets else []))


def _font(size, bold=False, mono=False):
    font = QFont("Consolas" if mono else "Segoe UI", size)
    font.setBold(bold)
    return font


def _badge(painter, x, y, verdict):
    """The verdict as a small coloured tag; returns its width."""
    colour = _VERDICT_COLOURS.get(verdict, _MUTED)
    painter.setFont(_font(8, bold=True))
    width = QFontMetrics(painter.font()).horizontalAdvance(verdict.upper()) + 14
    rect = QRectF(x, y, width, 18)
    fill = QColor(colour)
    fill.setAlpha(40)
    path = QPainterPath()
    path.addRoundedRect(rect, 4, 4)
    painter.fillPath(path, fill)
    painter.setPen(QPen(colour, 1))
    painter.drawPath(path)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, verdict.upper())
    return width


def _sparkline(painter, rect, curve):
    if len(curve) < 2:
        return

    values = [point[1] for point in curve]
    low, high = min(values + [0.0]), max(values + [0.0])
    span = (high - low) or 1.0
    step = rect.width() / (len(values) - 1)
    path = QPainterPath()

    for index, value in enumerate(values):
        point = QPointF(rect.left() + index * step, rect.bottom() - (value - low) / span * rect.height())
        path.moveTo(point) if index == 0 else path.lineTo(point)

    zero = rect.bottom() - (0 - low) / span * rect.height()
    painter.setPen(QPen(_GRID, 1, Qt.PenStyle.DashLine))
    painter.drawLine(QPointF(rect.left(), zero), QPointF(rect.right(), zero))
    painter.setPen(QPen(_GAIN if values[-1] >= 0 else _LOSS, 1.6))
    painter.drawPath(path)


def _part_bars(painter, rect, parts):
    """One small bar per part, up for a gain and down for a loss: consistency at a glance."""
    if not parts:
        return

    biggest = max(abs(value) for value in parts) or 1.0
    width = rect.width() / len(parts)
    middle = rect.center().y()
    painter.setPen(QPen(_GRID, 1))
    painter.drawLine(QPointF(rect.left(), middle), QPointF(rect.right(), middle))

    for index, value in enumerate(parts):
        height = abs(value) / biggest * (rect.height() / 2 - 1)
        top = middle - height if value > 0 else middle
        bar = QRectF(rect.left() + index * width + 2, top, width - 4, max(height, 1.5))
        painter.fillRect(bar, _GAIN if value > 0 else _LOSS)


class _Board(QWidget):
    """Every trader examined, one row each."""

    picked = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.board = None
        self.selected = 0
        self.setMouseTracking(True)

    def show_board(self, board, selected=0):
        self.board = board
        self.selected = selected
        self.setFixedHeight(max(1, 46 + _ROW * len(board["traders"]) + 100))
        self.update()

    def row_top(self, index):
        return 46 + index * _ROW

    def mousePressEvent(self, event):
        if not self.board:
            return

        index = int((event.position().y() - 46) // _ROW)

        if 0 <= index < len(self.board["traders"]):
            self.picked.emit(index)

    def paintEvent(self, event):
        if not self.board:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()

        painter.setPen(QPen(_MUTED))
        painter.setFont(_font(8, bold=True))

        for x, text in ((8, "#  TRADER"), (width - 448, f"MADE {self.board['days']}D"), (width - 378, "WIN %"), (width - 318, "PROFIT F."),
                        (width - 238, "DEEPEST FALL"), (width - 140, "CURVE"), (width - 52, "PARTS")):
            painter.drawText(int(x), 30, text)

        for index, trader in enumerate(self.board["traders"]):
            top = self.row_top(index)
            rect = QRectF(0, top, width - 4, _ROW - 6)

            if index == self.selected:
                glow = QColor(_ACCENT)
                glow.setAlpha(26)
                path = QPainterPath()
                path.addRoundedRect(rect, 6, 6)
                painter.fillPath(path, glow)

            painter.setPen(QPen(_GRID, 1))
            painter.drawLine(QPointF(0, rect.bottom() + 3), QPointF(width - 4, rect.bottom() + 3))

            painter.setPen(QPen(_ACCENT))
            painter.setFont(_font(12, bold=True, mono=True))
            painter.drawText(QRectF(6, top, 30, 26), Qt.AlignmentFlag.AlignVCenter, str(index + 1))

            painter.setPen(QPen(_TEXT))
            painter.setFont(_font(10, bold=True))
            name = QFontMetrics(painter.font()).elidedText(_name(trader), Qt.TextElideMode.ElideRight, width - 520)
            painter.drawText(QRectF(36, top + 2, width - 480, 22), Qt.AlignmentFlag.AlignVCenter, name)
            _badge(painter, 36, top + 30, trader["verdict"])
            painter.setPen(QPen(_MUTED))
            painter.setFont(_font(8))
            room = width - 48
            painter.drawText(QRectF(36, top + _FIGURES, room, 18), Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(painter.font()).elidedText(_profile(trader), Qt.TextElideMode.ElideRight,
                                                                     int(room)))

            made = trader.get("period_pnl", trader["net"])
            painter.setFont(_font(10, mono=True))
            painter.setPen(QPen(_GAIN if made >= 0 else _LOSS))
            painter.drawText(QRectF(width - 452, top, 70, _FIGURES), Qt.AlignmentFlag.AlignVCenter, _money(made))
            painter.setPen(QPen(_TEXT))
            painter.drawText(QRectF(width - 378, top, 56, _FIGURES), Qt.AlignmentFlag.AlignVCenter,
                             f"{trader['win_rate']:.0%}")
            factor = trader.get("profit_factor")
            painter.drawText(QRectF(width - 318, top, 70, _FIGURES), Qt.AlignmentFlag.AlignVCenter,
                             f"{factor:.2f}" if factor else "no losses")
            painter.setPen(QPen(_LOSS if trader.get("dip_share", 0) > 0.3 else _TEXT))
            painter.drawText(QRectF(width - 238, top, 96, _FIGURES), Qt.AlignmentFlag.AlignVCenter,
                             f"{_money(-trader['dip'])} {trader.get('dip_share', 0):.0%}")

            _sparkline(painter, QRectF(width - 140, top + 10, 78, _FIGURES - 20), trader.get("curve") or [])
            _part_bars(painter, QRectF(width - 52, top + 10, 44, _FIGURES - 20), trader.get("parts") or [])

        painter.setPen(QPen(_MUTED))
        painter.setFont(_font(8))
        foot = self.row_top(len(self.board["traders"])) + 10
        painter.drawText(QRectF(8, foot, width - 16, 84), Qt.TextFlag.TextWordWrap,
                         f"Consistent: made money in every one of the {self.board['parts']} parts of the last "
                         f"{self.board['days']} days, over at least {self.board['min_trades']} closed trades. Made: the account's "
                         f"profit over the period, open positions included. Under each name: how long they hold, "
                         f"which way they lean, and the markets they trade most. "
                         f"Deepest fall: the furthest profit dropped from its best, and as a share of the account "
                         f"then. A past record only: it says nothing certain about what comes next.")
        painter.end()


class _Trader(QWidget):
    """One trader's record, with the working."""

    CHART = 190
    PARTS = 130
    DOTS = 120
    LABEL = 150

    def __init__(self):
        super().__init__()
        self.trader = None
        self.board = None

    def show_trader(self, board, trader):
        self.board, self.trader = board, trader
        self._fit()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)

        if event.size().width() != event.oldSize().width():
            self._fit()

    def _fit(self):
        """The height every section needs at this width, found by laying the page out without showing it."""
        if not self.trader:
            return

        picture = QPicture()
        painter = QPainter(picture)
        height = self._draw(painter, self.width() - 6)
        painter.end()
        self.setFixedHeight(int(height) + 30)

    def paintEvent(self, event):
        if not self.trader:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(painter, self.width() - 6)
        painter.end()

    def _draw(self, painter, width):
        """Every section, top to bottom; returns where the page ends."""
        trader, board, y = self.trader, self.board, 0

        painter.setPen(QPen(_TEXT))
        painter.setFont(_font(13, bold=True))
        painter.drawText(QRectF(0, y, width, 26), Qt.AlignmentFlag.AlignVCenter, _name(trader))
        painter.setFont(_font(9))
        painter.setPen(QPen(_MUTED))
        painter.drawText(QRectF(0, y + 24, width, 18), Qt.AlignmentFlag.AlignVCenter,
                         f"{trader['address']}   account {_money(trader['account'])}")
        badge = _badge(painter, 0, y + 46, trader["verdict"])
        painter.setPen(QPen(_TEXT))
        painter.setFont(_font(9))
        painter.drawText(QRectF(badge + 10, y + 46, width, 18), Qt.AlignmentFlag.AlignVCenter, trader["reason"])
        y += 76

        y = self._section(painter, y, width, "HOW THEY TRADE -- read from what they did")
        y = self._style(painter, y, width)

        y = self._section(painter, y + 10, width, "WHAT THEY TRADE -- share of closed trades, and what each made")
        y = self._markets(painter, y, width)

        y = self._section(painter, y + 10, width,
                          f"PROFIT OVER {board['days']} DAYS -- shaded: every fall from the best so far")
        y = self._curve(painter, QRectF(48, y, width - 56, self.CHART - 20), width)

        y = self._section(painter, y + 10, width, f"EACH PART OF THE PERIOD -- all {board['parts']} must be green")
        self._parts(painter, QRectF(48, y, width - 56, self.PARTS - 30))
        y = self._note(painter, y + self.PARTS - 8, width,
                       "From the account's own profit history, so every part is covered, open positions included.")

        y = self._section(painter, y + 10, width, "EVERY CLOSED TRADE -- size of dot: size of the result")
        self._dots(painter, QRectF(48, y, width - 56, self.DOTS - 20))
        y += self.DOTS - 10
        start = board["updated"] - board["days"] * _DAY_MS

        if trader.get("covered_from", start) > start + _DAY_MS:
            y = self._note(painter, y, width, f"Trades from {_date(trader['covered_from'])} on: Hyperliquid keeps only"
                                              f" an account's most recent fills.")

        y = self._section(painter, y + 10, width, "THE WORKING")
        y = self._working(painter, y, width)

        y = self._section(painter, y + 10, width, "OPEN NOW -- leverage, and how far price is from liquidating each")
        return self._positions(painter, y, width)

    def _section(self, painter, y, width, title):
        painter.setPen(QPen(_ACCENT))
        painter.setFont(_font(8, bold=True, mono=True))
        painter.drawText(QRectF(0, y, width, 16), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(QPen(_GRID, 1))
        painter.drawLine(QPointF(0, y + 19), QPointF(width, y + 19))
        return y + 28

    def _wrapped(self, painter, x, y, width, text, font, colour):
        """[text] wrapped to [width]; returns where it ends."""
        painter.setFont(font)
        painter.setPen(QPen(colour))
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop)
        height = QFontMetrics(font).boundingRect(QRect(0, 0, int(width), 10_000), flags, text).height()
        painter.drawText(QRectF(x, y, width, height), flags, text)
        return y + height

    def _note(self, painter, y, width, text):
        return self._wrapped(painter, 0, y, width, text, _font(8), _MUTED) + 4

    def _style(self, painter, y, width):
        habits = self.trader.get("style") or []

        if not habits:
            return self._note(painter, y, width, "Too little trading in the period to read a style.")

        for name, detail in habits:
            painter.setFont(_font(9, bold=True))
            painter.setPen(QPen(_ACCENT))
            painter.drawText(QRectF(0, y, self.LABEL, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             name)
            y = max(y + 20, self._wrapped(painter, self.LABEL, y + 2, width - self.LABEL, detail, _font(9), _TEXT)) + 4

        return self._note(painter, y + 2, width, "Behaviour, not their plan: the signals behind each trade are not"
                                                 " public, and the same habits can come from different reasons.")

    def _markets(self, painter, y, width):
        coins = self.trader.get("coins") or []
        trades = self.trader.get("trades") or 0

        if not coins or not trades:
            return self._note(painter, y, width, "No closed trades among the fills Hyperliquid keeps.")

        bar_left, bar_width = 110, width - 110 - 250

        for name, count, net in coins:
            share = count / trades
            painter.setFont(_font(9, bold=True, mono=True))
            painter.setPen(QPen(_TEXT))
            painter.drawText(QRectF(0, y, bar_left - 8, 20), Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(painter.font()).elidedText(name, Qt.TextElideMode.ElideRight, bar_left - 8))
            track = QRectF(bar_left, y + 5, bar_width, 10)
            painter.setPen(QPen(_GRID, 1))
            painter.drawRect(track)
            painter.fillRect(QRectF(track.left(), track.top(), track.width() * share, track.height()), _ACCENT)
            painter.setFont(_font(9, mono=True))
            painter.drawText(QRectF(track.right() + 10, y, 130, 20), Qt.AlignmentFlag.AlignVCenter,
                             f"{share:>4.0%}  {count} trades")
            painter.setPen(QPen(_GAIN if net >= 0 else _LOSS))
            painter.drawText(QRectF(track.right() + 140, y, 100, 20),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, _money(net))
            y += 22

        shown = sum(count for _name_, count, _net in coins)

        if shown < trades:
            y = self._note(painter, y + 2, width, f"{trades - shown} more trades in other markets.")

        return y

    def _axis(self, painter, rect, low, high):
        painter.setFont(_font(7, mono=True))

        for fraction in (0.0, 0.5, 1.0):
            value = low + (high - low) * fraction
            level = rect.bottom() - fraction * rect.height()
            painter.setPen(QPen(_GRID, 1))
            painter.drawLine(QPointF(rect.left(), level), QPointF(rect.right(), level))
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(rect.left() - 48, level - 7, 44, 14),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, _money(value))

    def _curve(self, painter, rect, width):
        """The profit curve; the deepest fall is marked on it and explained beneath, never written across it."""
        curve = self.trader.get("curve") or []

        if len(curve) < 2:
            painter.setPen(QPen(_MUTED))
            painter.setFont(_font(9))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No profit history for the period.")
            return rect.bottom() + 10

        start, end = curve[0][0], curve[-1][0]
        values = [point[1] for point in curve]
        low, high = min(values + [0.0]), max(values + [0.0])
        span, length = (high - low) or 1.0, (end - start) or 1

        def at(when, value):
            return QPointF(rect.left() + (when - start) / length * rect.width(),
                           rect.bottom() - (value - low) / span * rect.height())

        self._axis(painter, rect, low, high)

        # Shade the gap under the running best: every fall from a peak.
        peak, shade = None, QPainterPath()
        deepest, deepest_at = 0.0, None

        for index, (when, value) in enumerate(curve):
            prior_peak = peak
            peak = value if peak is None else max(peak, value)

            if peak - value > deepest:
                deepest, deepest_at = peak - value, (when, value, peak)

            if index and prior_peak > min(value, curve[index - 1][1]):
                (then, before) = curve[index - 1]
                quad = QPainterPath(at(then, prior_peak))

                if value <= prior_peak:
                    # Still below the old best all the way along this stretch.
                    quad.lineTo(at(when, prior_peak))
                    quad.lineTo(at(when, value))
                else:
                    # Climbs back past it part-way: shade only up to where it crosses.
                    crossing = then + (prior_peak - before) / (value - before) * (when - then)
                    quad.lineTo(at(crossing, prior_peak))

                quad.lineTo(at(then, before))
                quad.closeSubpath()
                shade.addPath(quad)

        fill = QColor(_LOSS)
        fill.setAlpha(55)
        painter.fillPath(shade, fill)

        line = QPainterPath(at(*curve[0]))

        for when, value in curve[1:]:
            line.lineTo(at(when, value))

        painter.setPen(QPen(_GAIN if values[-1] >= 0 else _LOSS, 2))
        painter.drawPath(line)

        if deepest_at:
            when, value, peak = deepest_at
            top, bottom = at(when, peak), at(when, value)
            painter.setPen(QPen(_LOSS, 1.2, Qt.PenStyle.DashLine))
            painter.drawLine(top, bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_LOSS)
            painter.drawEllipse(top, 2.5, 2.5)
            painter.drawEllipse(bottom, 2.5, 2.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.setPen(QPen(_MUTED))
        painter.setFont(_font(7, mono=True))
        painter.drawText(QPointF(rect.left(), rect.bottom() + 12), _date(start))
        painter.drawText(QPointF(rect.right() - 36, rect.bottom() + 12), _date(end))

        y = rect.bottom() + 20

        if not deepest_at:
            return self._note(painter, y, width, "Profit never fell from its best in the period.")

        share = self.trader.get("dip_share", 0)
        when = (f", {_date(self.trader['dip_from'])} to {_date(self.trader['dip_to'])}"
                if self.trader.get("dip_from") and self.trader.get("dip_to") else "")
        text = (f"Deepest fall (dashed red line): {_money(-self.trader.get('dip', deepest))}{when},"
                f" {share:.0%} of the account at its best.")

        if share > 1:
            text += " More than the account held then, so money was paid in during the fall."

        y = self._wrapped(painter, 0, y, width, text, _font(9, bold=True), _LOSS)
        return y + 4

    def _parts(self, painter, rect):
        parts = self.trader.get("parts") or []

        if not parts:
            return

        biggest = max(abs(value) for value in parts) or 1.0
        self._axis(painter, rect, -biggest, biggest)
        middle = rect.center().y()
        width = rect.width() / len(parts)
        start = self.board["updated"] - self.board["days"] * _DAY_MS
        step = self.board["days"] * _DAY_MS / len(parts)
        painter.setFont(_font(8, bold=True, mono=True))

        for index, value in enumerate(parts):
            height = abs(value) / biggest * rect.height() / 2
            bar = QRectF(rect.left() + index * width + width * 0.2, middle - height if value > 0 else middle,
                         width * 0.6, max(height, 1.5))
            painter.fillRect(bar, _GAIN if value > 0 else _LOSS)
            painter.setPen(QPen(_TEXT))
            label_y = bar.top() - 4 if value > 0 else bar.bottom() + 12
            painter.drawText(QRectF(bar.left() - 20, label_y - 12, bar.width() + 40, 14),
                             Qt.AlignmentFlag.AlignCenter, _money(value))
            painter.setPen(QPen(_MUTED))
            painter.setFont(_font(7, mono=True))
            painter.drawText(QRectF(bar.left() - 20, rect.bottom() + 2, bar.width() + 40, 14),
                             Qt.AlignmentFlag.AlignCenter,
                             f"{_date(start + index * step)}-{_date(start + (index + 1) * step)}")
            painter.setFont(_font(8, bold=True, mono=True))

    def _dots(self, painter, rect):
        points = self.trader.get("trade_points") or []

        if not points:
            return

        start = self.board["updated"] - self.board["days"] * _DAY_MS
        length = self.board["days"] * _DAY_MS
        biggest = max(abs(value) for _when, value in points) or 1.0
        self._axis(painter, rect, -biggest, biggest)

        for when, value in points:
            x = rect.left() + (when - start) / length * rect.width()
            y = rect.center().y() - value / biggest * rect.height() / 2
            radius = 2 + 5 * (abs(value) / biggest) ** 0.5
            colour = QColor(_GAIN if value > 0 else _LOSS)
            colour.setAlpha(170)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(x, y), radius, radius)

        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _working(self, painter, y, width):
        trader = self.trader
        factor = trader.get("profit_factor")
        lines = [
            ("Made in the period", f"{_money(trader.get('period_pnl', trader['net']))} by the account's profit"
                                   f" history, open positions included"),
            ("Win rate", f"wins / trades = {trader['wins']} / {trader['trades']} = {trader['win_rate']:.0%}"),
            ("Profit factor", (f"won / lost = {_money(trader['gross_won'])} / {_money(trader['gross_lost'])}"
                               f" = {factor:.2f}") if factor else "no losing trades in the period"),
            ("Closed trades net", f"won - lost = {_money(trader['gross_won'])} - {_money(trader['gross_lost'])}"
                                  f" = {_money(trader['net'])} (after {_money(trader['fees'])} fees)"),
            ("Win : loss size", (f"average win / average loss = {_money(trader['average_win'])} /"
                                 f" {_money(trader['average_loss'])} = "
                                 f"{trader['average_win'] / trader['average_loss']:.2f}")
             if trader["average_loss"] else "no losses to compare"),
            ("Best trade's share", (f"best / closed net = {_money(trader['best'])} / {_money(trader['net'])} ="
                                    f" {trader['best_share']:.0%}") if trader.get("best_share") else "no net profit"),
            ("Parts", "  ".join(f"{index + 1}: {_money(value)}" for index, value in enumerate(trader["parts"]))),
        ]

        for label, text in lines:
            painter.setFont(_font(9, mono=True))
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(0, y, self.LABEL, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             label)
            y = max(y + 20, self._wrapped(painter, self.LABEL, y + 2, width - self.LABEL, text,
                                          _font(9, mono=True), _TEXT)) + 2

        return y

    def _positions(self, painter, y, width):
        positions = self.trader.get("positions") or []
        painter.setFont(_font(9, mono=True))

        if not positions:
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(0, y, width, 20), Qt.AlignmentFlag.AlignVCenter, "No open positions.")
            return y + 24

        columns = (("MARKET", 0), ("SIDE", 90), ("VALUE", 145), ("LEV.", 220), ("OPEN P/L", 270),
                   ("TO LIQUIDATION", 360))
        painter.setPen(QPen(_MUTED))
        painter.setFont(_font(8, bold=True))

        for title, x in columns:
            painter.drawText(QPointF(x, y + 12), title)

        y += 20
        painter.setFont(_font(9, mono=True))

        for position in positions:
            painter.setPen(QPen(_TEXT))
            painter.drawText(QPointF(0, y + 14), QFontMetrics(painter.font()).elidedText(
                position["coin"], Qt.TextElideMode.ElideRight, 84))
            painter.setPen(QPen(_GAIN if position["side"] == "long" else _LOSS))
            painter.drawText(QPointF(90, y + 14), position["side"])
            painter.setPen(QPen(_TEXT))
            painter.drawText(QPointF(145, y + 14), _money(position["value"]))
            painter.drawText(QPointF(220, y + 14), f"{position['leverage']:g}x" if position.get("leverage") else "-")
            painter.setPen(QPen(_GAIN if position["unrealised"] >= 0 else _LOSS))
            painter.drawText(QPointF(270, y + 14), _money(position["unrealised"]))

            distance = position.get("to_liquidation")
            bar = QRectF(360, y + 4, width - 440, 12)
            painter.setPen(QPen(_GRID, 1))
            painter.drawRect(bar)

            if distance is not None:
                shown = min(distance, _LIQUIDATION_FULL) / _LIQUIDATION_FULL
                painter.fillRect(QRectF(bar.left(), bar.top(), bar.width() * shown, bar.height()),
                                 _liquidation_colour(distance))
                painter.setPen(QPen(_TEXT))
                painter.drawText(QPointF(bar.right() + 8, y + 14), f"{distance:.0%}")
            else:
                painter.setPen(QPen(_GAIN))
                painter.drawText(QRectF(bar.left(), bar.top() - 3, bar.width(), bar.height() + 6),
                                 Qt.AlignmentFlag.AlignCenter, "no liquidation price")
                painter.drawText(QPointF(bar.right() + 8, y + 14), "covered")

            y += 26

        y = self._note(painter, y + 4, width,
                       f"OPEN P/L: profit (green) or loss (red) on the position right now, not yet taken. TO"
                       f" LIQUIDATION: how far price must move against the position before the exchange closes it"
                       f" by force; a longer bar is safer. Green: over {_SAFE_DISTANCE:.0%} away; amber:"
                       f" {_CLOSE_DISTANCE:.0%} to {_SAFE_DISTANCE:.0%}; red: under {_CLOSE_DISTANCE:.0%}. Covered:"
                       f" Hyperliquid reports no liquidation price for it, which it does when the account's margin"
                       f" covers the position.")
        return y


class TradersPanel(QWidget):
    """The board and its traders, scrollable and navigable."""

    show_board = pyqtSignal(dict)
    hide_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._anchor = None
        self._drag_offset = None
        self._sweep = 0.0
        self._board = None
        self._index = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)
        self.setStyleSheet(_CONTROLS)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._title = QLabel()
        self._title.setStyleSheet("color: #5fc8f5; font: bold 11pt Consolas; letter-spacing: 2px;")
        self._subtitle = QLabel()
        self._subtitle.setStyleSheet("color: #8294a5; font: 8pt 'Segoe UI';")

        self._back = QPushButton("Back")
        self._previous = QPushButton("<")
        self._next = QPushButton(">")
        self._close = QPushButton("Close")

        self._back.clicked.connect(self._to_board)
        self._previous.clicked.connect(lambda: self._step(-1))
        self._next.clicked.connect(lambda: self._step(1))
        self._close.clicked.connect(self._close_panel)

        heading = QHBoxLayout()
        words = QVBoxLayout()
        words.addWidget(self._title)
        words.addWidget(self._subtitle)
        heading.addLayout(words, 1)

        for button in (self._back, self._previous, self._next, self._close):
            heading.addWidget(button)

        self._list = _Board()
        self._list.picked.connect(self._open)
        self._detail = _Trader()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 16, _MARGIN - 8, 18)
        layout.addLayout(heading)
        layout.addSpacing(8)
        layout.addWidget(self._scroll, 1)

        self.show_board.connect(self._on_board)
        self.hide_requested.connect(self._on_hide)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        self._anchor = widget

    # ---- what arrives ----

    def _on_board(self, board):
        self._board = board
        updated = datetime.fromtimestamp(board.get("updated", 0) / 1000, tz=timezone.utc).strftime("%d %b %H:%M UTC")
        self._title.setText(f"TRADER WATCH  /  {board.get('source', '').upper()}")
        good = sum(1 for trader in board["traders"] if trader["verdict"] == "consistent")
        self._subtitle.setText(f"{len(board['traders'])} examined, {good} consistent  -  last {board['days']} days in"
                               f" {board['parts']} parts  -  read {updated}")
        focus = board.get("focus")

        if focus:
            self._open(focus - 1)
        else:
            self._to_board()

        self._position()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._animate.start(_FRAME_MS)

    def _on_hide(self):
        self._animate.stop()
        self.hide()

    def _close_panel(self):
        self._on_hide()
        self.closed.emit()

    # ---- navigation ----

    def _to_board(self):
        self._index = None
        self._list.show_board(self._board, self._list.selected if self._board else 0)
        self._swap(self._list)
        self._buttons()

    def _open(self, index):
        if not self._board or not 0 <= index < len(self._board["traders"]):
            return

        self._index = index
        self._list.selected = index
        self._detail.show_trader(self._board, self._board["traders"][index])
        self._swap(self._detail)
        self._buttons()

    def _step(self, by):
        if self._board is None:
            return

        if self._index is None:
            count = len(self._board["traders"])
            self._list.selected = max(0, min(count - 1, self._list.selected + by))
            self._list.update()
            self._scroll.ensureVisible(0, self._list.row_top(self._list.selected) + _ROW // 2, 0, _ROW)
            return

        self._open(max(0, min(len(self._board["traders"]) - 1, self._index + by)))

    def _swap(self, widget):
        current = self._scroll.takeWidget()

        if current is not None and current is not widget:
            current.setParent(None)

        widget.setFixedWidth(self._scroll.viewport().width() or _WIDTH - 2 * _MARGIN)
        self._scroll.setWidget(widget)
        self._scroll.verticalScrollBar().setValue(0)

    def _buttons(self):
        detail = self._index is not None
        self._back.setEnabled(detail)
        count = len(self._board["traders"]) if self._board else 0
        self._previous.setEnabled(detail and self._index > 0)
        self._next.setEnabled(detail and self._index < count - 1)

    def keyPressEvent(self, event):
        key = event.key()

        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self._to_board() if self._index is not None else self._close_panel()
        elif key == Qt.Key.Key_Up and self._index is None:
            self._step(-1)
        elif key == Qt.Key.Key_Down and self._index is None:
            self._step(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._index is None and self._board:
            self._open(self._list.selected)
        elif key == Qt.Key.Key_Left and self._index is not None:
            self._step(-1)
        elif key == Qt.Key.Key_Right and self._index is not None:
            self._step(1)
        else:
            super().keyPressEvent(event)

    # ---- placement and drawing ----

    def _position(self):
        screen = self.screen().availableGeometry()

        if self._anchor is not None and self._anchor.isVisible():
            frame = self._anchor.frameGeometry()
            x = max(screen.left() + 12, frame.left() - _WIDTH - _BEAM_GAP)
            y = max(screen.top() + 12, min(frame.center().y() - _HEIGHT // 2, screen.bottom() - _HEIGHT - 12))
            self.move(int(x), int(y))
            return

        self.move(screen.left() + (screen.width() - _WIDTH) // 2, screen.top() + (screen.height() - _HEIGHT) // 2)

    def _tick(self):
        self._sweep = (self._sweep + 0.004) % 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(5, 5, -5, -5)
        path = QPainterPath()
        path.addRoundedRect(body, 18, 18)
        painter.fillPath(path, _BACKDROP)
        edge = QColor(_ACCENT)
        edge.setAlpha(95)
        painter.setPen(QPen(edge, 1.5))
        painter.drawPath(path)

        line = QColor(_ACCENT)
        line.setAlpha(24)
        y = body.top() + body.height() * self._sweep
        painter.setPen(QPen(line, 1))
        painter.drawLine(QPointF(body.left(), y), QPointF(body.right(), y))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
