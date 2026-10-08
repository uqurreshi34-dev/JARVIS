"""The trade plan JARVIS projects for a market: the chart, the lines, and what has to happen before a trade.

Built like the other panels -- frameless, translucent, anchored beside the
HUD with the beam crossing the gap -- as one scrollable page:

    the verdict   WAIT, BUY, SELL or SKIP, and the sentence behind it
    the chart     the last candles, support and resistance drawn with their
                  prices and how often price turned there, your own lines in
                  amber, the price now, and to the right each plan's path:
                  1 break, 2 retest, 3 go, with its stop and target marked
    RSI           beneath, on the same candles, with 30, 50 and 70 drawn and
                  what each zone means
    the plans     buy and sell, step by step, each step ticked when the chart
                  has done it, then entry, stop, target and reward : risk with
                  the arithmetic written out
    the working   how the lines were found and what ATR and RSI are

Escape or Close puts it away; the wheel scrolls. Everything arrives through a
signal: this is driven from the command thread, and a Qt widget may only be
touched from its own.
"""

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPicture, QPolygonF
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget


_WIDTH = 780
_HEIGHT = 640
_MARGIN = 22

_BACKDROP = QColor(8, 14, 21, 238)
_ACCENT = QColor(95, 200, 245)
_TEXT = QColor(226, 236, 245)
_MUTED = QColor(130, 148, 165)
_GRID = QColor(95, 200, 245, 34)
_GAIN = QColor(70, 205, 135)
_LOSS = QColor(235, 95, 95)
_AMBER = QColor(235, 185, 80)

_VERDICT_COLOURS = {"wait": _AMBER, "buy": _GAIN, "sell": _LOSS, "skip": _MUTED}
_SIDE_COLOURS = {"buy": _GAIN, "sell": _LOSS}
_STAGE_WORDS = {"break": "WAITING FOR THE BREAK", "retest": "BROKEN - WAITING FOR THE RETEST",
                "confirm": "RETESTED - WAITING FOR CONFIRMATION", "ready": "CONFIRMED"}

_BEAM_GAP = 74
_FRAME_MS = 33

_CHART = 320
_RSI = 120
_AXIS = 62          # price labels, left of the candles
_AHEAD = 230        # room right of the candles: the price tag, then each plan's path
_LABEL = 130

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
"""


def _font(size, bold=False, mono=False):
    font = QFont("Consolas" if mono else "Segoe UI", size)
    font.setBold(bold)
    return font


def _price(value, decimals):
    return f"{value:,.{decimals}f}"


def _tint(colour, alpha):
    shade = QColor(colour)
    shade.setAlpha(alpha)
    return shade


class _Page(QWidget):
    """The whole plan, top to bottom."""

    def __init__(self):
        super().__init__()
        self.plan = None

    def show_plan(self, plan):
        self.plan = plan
        self._fit()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)

        if event.size().width() != event.oldSize().width():
            self._fit()

    def _fit(self):
        """The height every section needs at this width, found by laying the page out without showing it."""
        if not self.plan:
            return

        picture = QPicture()
        painter = QPainter(picture)
        height = self._draw(painter, self.width() - 6)
        painter.end()
        self.setFixedHeight(int(height) + 30)

    def paintEvent(self, event):
        if not self.plan:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(painter, self.width() - 6)
        painter.end()

    # ---- the page ----

    def _draw(self, painter, width):
        plan, y = self.plan, 0
        colour = _VERDICT_COLOURS.get(plan["verdict"], _MUTED)

        word = plan["verdict"].upper()
        painter.setFont(_font(12, bold=True, mono=True))
        badge = QRectF(0, y, QFontMetrics(painter.font()).horizontalAdvance(word) + 24, 30)
        path = QPainterPath()
        path.addRoundedRect(badge, 5, 5)
        painter.fillPath(path, _tint(colour, 45))
        painter.setPen(QPen(colour, 1.4))
        painter.drawPath(path)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, word)
        y = max(y + 34, self._wrapped(painter, badge.width() + 14, y + 4, width - badge.width() - 14,
                                      plan["headline"], _font(10, bold=True), _TEXT)) + 6
        y = self._note(painter, y, width, "Rules, not a forecast: this says what has to happen before a trade, and"
                                          " where -- never that it will.")

        y = self._section(painter, y + 8, width, f"{plan['instrument'].upper()}, {plan['timeframe'].upper()} CANDLES"
                                                 f" -- the lines, the price now, and each plan's path")
        y = self._chart(painter, QRectF(_AXIS, y, width - _AXIS - _AHEAD, _CHART), width)

        y = self._section(painter, y + 8, width, "RSI (14) -- momentum, on the same candles")
        y = self._rsi(painter, QRectF(_AXIS, y, width - _AXIS - _AHEAD, _RSI), width)

        for side in ("buy", "sell"):
            y = self._side(painter, y + 12, width, side)

        y = self._section(painter, y + 10, width, "THE WORKING")
        return self._working(painter, y, width)

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

    def _note(self, painter, y, width, text, x=0):
        return self._wrapped(painter, x, y, width - x, text, _font(8), _MUTED) + 4

    # ---- the chart ----

    def _range(self):
        """The prices the chart must hold: the candles, and every plan's stop and target near them."""
        plan = self.plan
        lows = [candle[3] for candle in plan["candles"]]
        highs = [candle[2] for candle in plan["candles"]]
        low, high = min(lows), max(highs)
        reach = (high - low) * 0.6

        for side in ("buy", "sell"):
            found = plan.get(side)

            if found:
                for value in (found["line"], found["stop"], found["target"]):
                    if low - reach <= value <= high + reach:
                        low, high = min(low, value), max(high, value)

        pad = (high - low) * 0.04 or 1.0
        return low - pad, high + pad

    def _chart(self, painter, rect, width):
        plan = self.plan
        candles = plan["candles"]
        low, high = self._range()
        span = high - low
        step = rect.width() / len(candles)

        def level(value):
            return rect.bottom() - (value - low) / span * rect.height()

        def middle(index):
            return rect.left() + (index + 0.5) * step

        # Price grid.
        painter.setFont(_font(7, mono=True))

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            value = low + span * fraction
            y = level(value)
            painter.setPen(QPen(_GRID, 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(0, y - 7, _AXIS - 6, 14),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             _price(value, plan["decimals"]))

        # The lines: every one faint, the support and resistance either side of the price strong.
        for line in plan["levels"]:
            if not low <= line["price"] <= high:
                continue

            y = level(line["price"])
            strong = line["price"] in (plan.get("support"), plan.get("resistance"))
            colour = _AMBER if line["yours"] else (_LOSS if line["price"] > plan["price"] else _GAIN)
            painter.setPen(QPen(_tint(colour, 220 if strong else 90), 1.6 if strong else 1,
                                Qt.PenStyle.SolidLine if strong else Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right() + _AHEAD - 8, y))

            if strong or line["yours"]:
                name = ("yours" if line["yours"] else
                        "RESISTANCE" if line["price"] > plan["price"] else "SUPPORT")
                touches = f", {line['touches']} swing{'s' if line['touches'] != 1 else ''}" if line["touches"] else ""
                painter.setFont(_font(8, bold=True))
                text = f"{name} {_price(line['price'], plan['decimals'])}{touches}"
                tag = QRectF(rect.left() + 4, y - 16 if line["price"] > plan["price"] else y + 2,
                             QFontMetrics(painter.font()).horizontalAdvance(text) + 10, 14)
                painter.fillRect(tag, _tint(_BACKDROP, 200))
                painter.setPen(QPen(colour))
                painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, text)

        # The candles.
        for index, (_when, opened, top, bottom, closed, _label) in enumerate(candles):
            colour = _GAIN if closed >= opened else _LOSS
            x = middle(index)
            painter.setPen(QPen(colour, 1))
            painter.drawLine(QPointF(x, level(top)), QPointF(x, level(bottom)))
            body = QRectF(x - step * 0.32, level(max(opened, closed)), step * 0.64,
                          max(1.0, abs(level(opened) - level(closed))))
            painter.fillRect(body, colour)

        # The price now.
        now_y = level(plan["price"])
        painter.setPen(QPen(_ACCENT, 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(rect.left(), now_y), QPointF(rect.right(), now_y))
        painter.setFont(_font(8, bold=True, mono=True))
        tag = QRectF(rect.right() + 2, now_y - 8, 62, 16)
        painter.fillRect(tag, _ACCENT)
        painter.setPen(QPen(QColor(8, 14, 21)))
        painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, _price(plan["price"], plan["decimals"]))

        # Each plan's path: what has to happen, in order.
        for side in ("buy", "sell"):
            self._path(painter, side, rect, level, now_y)

        painter.setPen(QPen(_MUTED))
        painter.setFont(_font(7, mono=True))
        painter.drawText(QPointF(rect.left(), rect.bottom() + 12), candles[0][5])
        painter.drawText(QPointF(rect.right() - 70, rect.bottom() + 12), candles[-1][5])

        y = rect.bottom() + 20
        return self._note(painter, y, width,
                          f"Green dashes: the buy plan. Red: the sell plan. 1 break, 2 retest, 3 go, to the target"
                          f" (T); x marks the stop. Next {plan['timeframe']} candle closes {plan['next_close']} UK.")

    def _path(self, painter, side, rect, level, now_y):
        found = self.plan.get(side)

        if not found:
            return

        colour = _SIDE_COLOURS[side]
        sign = 1 if side == "buy" else -1
        line_y = level(found["line"])
        nudge = sign * 12
        left = rect.right() + 68
        right = rect.right() + _AHEAD - 58
        stage = found["stage"]

        if stage == "break":
            points = [(left, now_y), (left + 28, line_y - nudge), (left + 56, line_y), (right, level(found["target"]))]
            marks = [(1, "1"), (2, "2"), (3, "3")]
        elif stage == "retest":
            points = [(left, now_y), (left + 40, line_y), (right, level(found["target"]))]
            marks = [(1, "2"), (2, "3")]
        elif stage == "confirm":
            points = [(left, now_y), (right, level(found["target"]))]
            marks = [(0, "3")]
        else:
            points = [(left, now_y), (right, level(found["target"]))]
            marks = [(0, "go" if stage == "ready" else "")]

        polyline = QPainterPath(QPointF(*points[0]))

        for point in points[1:]:
            polyline.lineTo(QPointF(*point))

        painter.setPen(QPen(_tint(colour, 220), 1.6, Qt.PenStyle.DashLine))
        painter.drawPath(polyline)

        # An arrowhead at the target.
        tip, before = QPointF(*points[-1]), QPointF(*points[-2])
        dx, dy = tip.x() - before.x(), tip.y() - before.y()
        length = max((dx * dx + dy * dy) ** 0.5, 1.0)
        ux, uy = dx / length, dy / length
        head = QPolygonF([tip, QPointF(tip.x() - 9 * ux + 4 * uy, tip.y() - 9 * uy - 4 * ux),
                          QPointF(tip.x() - 9 * ux - 4 * uy, tip.y() - 9 * uy + 4 * ux)])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPolygon(head)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.setFont(_font(7, bold=True, mono=True))

        for index, text in marks:
            if not text:
                continue

            point = QPointF(*points[index])
            circle = QRectF(point.x() - 7, point.y() - 7, 14 + max(0, len(text) - 1) * 6, 14)
            painter.setPen(QPen(colour, 1))
            painter.setBrush(_tint(_BACKDROP, 230))
            painter.drawRoundedRect(circle, 7, 7)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, text)

        # Target and stop, marked at the right.
        painter.setFont(_font(7, bold=True, mono=True))
        target_y, stop_y = level(found["target"]), level(found["stop"])
        painter.setPen(QPen(colour, 1))
        d = self._short_places()
        painter.drawText(QPointF(right + 10, target_y + 4), f"T {found['target']:,.{d}f}")
        painter.drawLine(QPointF(right + 2, stop_y - 3), QPointF(right + 8, stop_y + 3))
        painter.drawLine(QPointF(right + 2, stop_y + 3), QPointF(right + 8, stop_y - 3))
        painter.drawText(QPointF(right + 11, stop_y + 4), f"{found['stop']:,.{d}f}")

    def _short_places(self):
        """Decimal places for a cramped label: none on a price in the thousands, the market's own below."""
        return 0 if self.plan["price"] >= 1000 else self.plan["decimals"]

    # ---- RSI ----

    def _rsi(self, painter, rect, width):
        plan = self.plan
        values = plan["rsis"]
        step = rect.width() / len(values)

        def level(value):
            return rect.bottom() - value / 100 * rect.height()

        painter.fillRect(QRectF(rect.left(), level(100), rect.width(), level(plan["rsi_high"]) - level(100)),
                         _tint(_LOSS, 28))
        painter.fillRect(QRectF(rect.left(), level(plan["rsi_low"]), rect.width(), level(0) - level(plan["rsi_low"])),
                         _tint(_GAIN, 28))
        painter.setFont(_font(7, mono=True))

        for value, words in ((plan["rsi_high"], "overbought"), (50, "middle"), (plan["rsi_low"], "oversold")):
            y = level(value)
            painter.setPen(QPen(_GRID if value != 50 else _tint(_ACCENT, 90), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(0, y - 7, _AXIS - 6, 14),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:g}")
            painter.drawText(QPointF(rect.right() + 6, y + 4), words)

        path, started = QPainterPath(), False

        for index, value in enumerate(values):
            if value is None:
                continue

            point = QPointF(rect.left() + (index + 0.5) * step, level(value))

            if started:
                path.lineTo(point)
            else:
                path.moveTo(point)
                started = True

        painter.setPen(QPen(_ACCENT, 1.6))
        painter.drawPath(path)

        y = rect.bottom() + 8

        if plan["rsi"] is not None:
            y = self._wrapped(painter, 0, y, width, f"RSI now {plan['rsi']:.0f}: {plan['rsi_zone']}.",
                              _font(9, bold=True), _TEXT) + 2

        return self._note(painter, y, width, f"Buying wants RSI above 50 but under {plan['rsi_high']}; selling,"
                                             f" under 50 but over {plan['rsi_low']}. Past those, the move may"
                                             f" already be spent.")

    # ---- a plan ----

    def _side(self, painter, y, width, side):
        found = self.plan.get(side)
        colour = _SIDE_COLOURS[side]
        y = self._section(painter, y, width, f"{side.upper()} PLAN")

        gone = (found or {}).get("passed") or self.plan.get(f"{side}_passed")

        if gone:
            d = self.plan["decimals"]
            verdict = ("worth taking" if gone["worth"] and gone["rsi_ok"]
                       else "the rules would have skipped it" + (f" (RSI {gone['rsi']:.0f})" if not gone["rsi_ok"]
                                                                 and gone["rsi"] is not None else
                                                                 f" ({gone['ratio']:.2f} : 1)"))
            y = self._note(painter, y, width,
                           f"Already been and gone: a {side} setup at {gone['line']:,.{d}f} confirmed on the candle"
                           f" that closed {gone['when']} UK -- entry {gone['entry']:,.{d}f}, stop {gone['stop']:,.{d}f},"
                           f" target {gone['target']:,.{d}f}, {gone['ratio']:.2f} : 1; {verdict}. It is not chased:"
                           f" the plan below is for the next one.") + 4

        if not found:
            where = "above" if side == "buy" else "below"
            return self._note(painter, y, width, f"No line {where} the price in the candles read:"
                                                 f" {self.plan['instrument']} is at its"
                                                 f" {'highest' if side == 'buy' else 'lowest'} for the period, with no"
                                                 f" breakout line to trade.")

        painter.setFont(_font(8, bold=True, mono=True))
        stage = _STAGE_WORDS.get(found["stage"], found["stage"].upper())
        chip = QRectF(0, y, QFontMetrics(painter.font()).horizontalAdvance(stage) + 16, 18)
        painter.fillRect(chip, _tint(colour, 40))
        painter.setPen(QPen(colour))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, stage)
        y += 26

        for number, step in enumerate(found["steps"], 1):
            centre = QPointF(9, y + 9)
            painter.setPen(QPen(colour, 1.4))
            painter.setBrush(colour if step["done"] else Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 8, 8)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(8, 14, 21) if step["done"] else colour))
            painter.setFont(_font(8, bold=True, mono=True))
            painter.drawText(QRectF(1, y + 1, 16, 16), Qt.AlignmentFlag.AlignCenter, str(number))
            painter.setFont(_font(9, bold=True))
            painter.setPen(QPen(_TEXT if step["done"] else _MUTED))
            painter.drawText(QRectF(26, y, _LABEL - 30, 18), Qt.AlignmentFlag.AlignVCenter, step["name"])
            text = ("done: " if step["done"] else "") + step["text"]
            y = max(y + 22, self._wrapped(painter, _LABEL, y + 1, width - _LABEL, text, _font(9),
                                          _TEXT if not step["done"] else _MUTED) + 4)

        verdict = ("worth taking" if found["worth"]
                   else f"under {self.plan['settings']['min_reward']:g} : 1, skip it")
        rows = [
            ("Entry", found["entry_working"]),
            ("Stop", found["stop_working"]),
            ("Target", found["target_working"]),
            ("Reward : risk", f"(target - entry) / (entry - stop) = {found['ratio_working']}: {verdict}"),
        ]

        if found.get("rsi") is not None:
            rows.append(("RSI then", f"{found['rsi']:.0f} -- " + ("in range" if found["rsi_ok"] else "out of range")))

        y += 4

        for label, text in rows:
            painter.setFont(_font(9, mono=True))
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(0, y, _LABEL, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
            y = max(y + 20, self._wrapped(painter, _LABEL, y + 2, width - _LABEL, text, _font(9, mono=True),
                                          _TEXT)) + 2

        return y

    def _working(self, painter, y, width):
        chosen = self.plan["settings"]
        rows = [
            ("Lines", f"A swing high is a candle higher than the {chosen['swing_strength']} either side of it; a swing"
                      f" low, lower. Swings within {chosen['merge_atr']:g} ATR of each other are one line, a line"
                      f" needs {chosen['min_touches']} of them, and the swings count how often price turned there."
                      f" Your own lines (amber) come from trade-plan.json, under {self.plan['symbol']}."),
            ("ATR (14)", f"{_price(self.plan['atr'], self.plan['decimals'])}: how far {self.plan['instrument']}"
                         f" typically moves in one {self.plan['timeframe']}"
                         f" candle. The stop sits {chosen['stop_atr']:g} ATR beyond the retest, so ordinary noise"
                         f" does not reach it."),
            ("Target", f"{chosen['target_buffer_atr']:g} ATR short of the next line: price often turns just before a"
                       f" line, so the target does not ask it to touch."),
            ("Runaways", f"A break older than {chosen['retest_candles']} candles with no retest is not chased."),
        ]

        for label, text in rows:
            painter.setFont(_font(9, mono=True))
            painter.setPen(QPen(_MUTED))
            painter.drawText(QRectF(0, y, _LABEL, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
            y = max(y + 20, self._wrapped(painter, _LABEL, y + 2, width - _LABEL, text, _font(9), _TEXT)) + 2

        return y


class TradePlanPanel(QWidget):
    """The plan, scrollable, beside the HUD."""

    show_plan = pyqtSignal(dict)
    hide_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._anchor = None
        self._drag_offset = None
        self._sweep = 0.0

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
        self._close = QPushButton("Close")
        self._close.clicked.connect(self._close_panel)

        heading = QHBoxLayout()
        words = QVBoxLayout()
        words.addWidget(self._title)
        words.addWidget(self._subtitle)
        heading.addLayout(words, 1)
        heading.addWidget(self._close)

        self._page = _Page()
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 16, _MARGIN - 8, 18)
        layout.addLayout(heading)
        layout.addSpacing(8)
        layout.addWidget(self._scroll, 1)

        self.show_plan.connect(self._on_plan)
        self.hide_requested.connect(self._on_hide)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        self._anchor = widget

    def _on_plan(self, plan):
        self._title.setText(f"{plan['instrument'].upper()}  /  {plan['timeframe'].upper()} PLAN")
        price = _price(plan["price"], plan["decimals"]) + ("" if plan["live"] else " (market closed: last close)")
        self._subtitle.setText(f"{plan['symbol']}  -  price {price}  -  ATR {_price(plan['atr'], plan['decimals'])}"
                               f"  -  candles to {plan['read']} UK"
                               f"  -  next close {plan['next_close']} UK")

        current = self._scroll.takeWidget()

        if current is not None and current is not self._page:
            current.setParent(None)

        self._page.setFixedWidth(self._scroll.viewport().width() or _WIDTH - 2 * _MARGIN)
        self._page.show_plan(plan)
        self._scroll.setWidget(self._page)
        self._scroll.verticalScrollBar().setValue(0)

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

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self._close_panel()
        else:
            super().keyPressEvent(event)

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
        painter.setPen(QPen(_tint(_ACCENT, 95), 1.5))
        painter.drawPath(path)
        painter.setPen(QPen(_tint(_ACCENT, 24), 1))
        y = body.top() + body.height() * self._sweep
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
