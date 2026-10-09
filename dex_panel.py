"""The arbitrage scan JARVIS projects: every pair's price on each exchange, the gap, and whether it would pay.

Built like the other panels -- frameless, translucent, anchored beside the HUD
with the beam crossing the gap, scrolling when there are more pairs than fit:

    the figures   ether's and bitcoin's price at this block, what gas costs
                  for one round trip, and the gap a round trip needs to pay
                  (the two pools' fees plus gas, as a share of the trade)
    the pairs     one row each: its price on each exchange, the gap between
                  them as a bar with the break-even marked on it (green when
                  the gap clears it), and the best round trip after fees and
                  gas, in dollars

Escape or Close puts it away; it can be dragged. Everything arriving from
elsewhere comes in through a signal: the scan runs on the command thread,
and a Qt widget may only be touched from its own.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget


_WIDTH = 760
_HEIGHT = 560
_MARGIN = 22
_BEAM_GAP = 74
_FRAME_MS = 33

_BACKDROP = QColor(8, 14, 21, 238)
_ACCENT = QColor(95, 200, 245)
_TEXT = QColor(226, 236, 245)
_MUTED = QColor(130, 148, 165)
_GRID = QColor(95, 200, 245, 34)
_GAIN = QColor(70, 205, 135)
_LOSS = QColor(235, 95, 95)
_AMBER = QColor(235, 185, 80)

_FIGURES = 118        # the height of the figures block above the rows
_HEADER = 26
_ROW = 44
_BEST = 180           # the width of the best-after-gas column
_RIGHT = 14           # kept clear on the right for the scrollbar

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


def _number(value):
    """A Decimal from the report's strings, or None."""
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def price_text(value):
    """A price as a trader reads it: whole dollars and cents for big prices, more places for small ones."""
    number = _number(value)

    if number is None:
        return "-"

    if number >= 1000:
        return f"{number:,.2f}"

    if number >= 1:
        return f"{number:,.4f}"

    return f"{number:.6g}"


def money_text(value):
    number = _number(value)

    if number is None:
        return "-"

    sign = "-" if number < 0 else "+"
    return f"{sign}${abs(number):,.2f}"


# A pool holding less than this many times the trade is shallow: the trade's own size moves its price, which
# is the loss shown, not the gap.
_SHALLOW_TIMES = 20


def _route(market, best, gap, report):
    """The line under a row's best figure: a shallow pool named first, as it explains a large loss; then
    where to buy and sell; or that the prices match."""
    trade = _number(report.get("requested_trade_size_usd")) or Decimal(0)
    depths = {name: _number(value) for name, value in (market.get("depth_usd") or {}).items()}
    shallow = min(((value, name) for name, value in depths.items() if value is not None), default=None)

    if shallow and trade and shallow[0] < trade * _SHALLOW_TIMES:
        return f"{shallow[1].split()[0]} pool only ${shallow[0]:,.0f} deep"

    if not gap:
        return "no gap"

    return f"buy {best['buy_base_on'].split()[0]}, sell {best['sell_base_on'].split()[0]}" if best.get("buy_base_on") else ""


def rows(report):
    """The table, one dict per pair: its prices on each exchange, gap, whether it clears break-even, and the
    best round trip after fees and gas -- what the panel draws, kept apart so it can be checked."""
    needed = _number(report.get("breakeven_gap_pct")) or Decimal(0)
    dexes = report.get("dexes") or []
    table = []

    for market in report.get("markets") or []:
        gap = _number(market.get("gap_pct"))
        best = market.get("best") or {}
        net = _number(best.get("estimated_net_profit_usd"))
        table.append({
            "pair": market["pair"],
            "prices": [price_text(market["prices"].get(name)) for name in dexes],
            "gap": gap,
            "clears": gap is not None and gap > needed,
            "net": net,
            "route": _route(market, best, gap, report),
        })

    return table


class _Scan(QWidget):
    """The figures and the pairs, drawn."""

    def __init__(self):
        super().__init__()
        self.report = None

    def show_report(self, report):
        self.report = report
        self.setFixedHeight(_FIGURES + _HEADER + _ROW * len(report.get("markets") or []) + 60)
        self.update()

    def paintEvent(self, event):
        if not self.report:
            return

        report = self.report
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        needed = _number(report.get("breakeven_gap_pct")) or Decimal(0)

        # ---- the figures ----
        figures = [
            ("ETHER", f"${price_text(report.get('estimated_eth_usd'))}"),
            ("BITCOIN", f"${price_text(report.get('estimated_btc_usd'))}"),
            ("GAS, ONE ROUND TRIP", money_text(report.get("estimated_gas_cost_usd")).lstrip("+")),
            ("A GAP MUST BEAT", f"{needed:.2f}%"),
        ]
        column = width / len(figures)

        for index, (label, value) in enumerate(figures):
            x = index * column
            painter.setPen(_MUTED)
            painter.setFont(_font(8, bold=True))
            painter.drawText(QRectF(x, 4, column - 8, 16), Qt.AlignmentFlag.AlignLeft, label)
            painter.setPen(_AMBER if index == 3 else _TEXT)
            painter.setFont(_font(15, bold=True))
            painter.drawText(QRectF(x, 20, column - 8, 28), Qt.AlignmentFlag.AlignLeft, value)

        fee = _number(report.get("round_trip_fee_pct")) or Decimal(0)
        painter.setPen(_MUTED)
        painter.setFont(_font(9))
        painter.drawText(
            QRectF(0, 56, width, 52), Qt.TextFlag.TextWordWrap,
            f"Buying on one exchange and selling on the other pays only when their prices differ by more than"
            f" the two swaps' fees ({fee:.2f}%) plus gas (the network's fee for running the swaps) as a share of"
            f" the ${_number(report.get('requested_trade_size_usd')) or 0:,.0f} traded. Green gaps clear it.")

        # ---- the pairs ----
        dexes = report.get("dexes") or []
        top = _FIGURES
        columns = [("PAIR", 0, 110)] + [(name.split()[0].upper(), 110 + 120 * index, 120)
                                       for index, name in enumerate(dexes)]
        bar_left = 110 + 120 * len(dexes) + 8
        best_left = width - _RIGHT - _BEST
        bar_width = max(80.0, best_left - bar_left - 10)
        columns += [("GAP", bar_left, bar_width), ("BEST AFTER GAS", best_left, _BEST)]
        painter.setFont(_font(8, bold=True))
        painter.setPen(_MUTED)

        for label, x, span in columns:
            painter.drawText(QRectF(x, top, span, _HEADER - 6), Qt.AlignmentFlag.AlignLeft, label)

        table = rows(report)
        widest = max([row["gap"] for row in table if row["gap"] is not None] + [needed * 2, Decimal("0.01")])

        for index, row in enumerate(table):
            y = top + _HEADER + index * _ROW
            painter.setPen(QPen(_GRID, 1))
            painter.drawLine(QPointF(0, y), QPointF(width, y))
            middle = QRectF(0, y, width, _ROW)

            painter.setPen(_TEXT)
            painter.setFont(_font(10, bold=True))
            painter.drawText(QRectF(0, y, 110, _ROW), Qt.AlignmentFlag.AlignVCenter, row["pair"])
            painter.setFont(_font(10, mono=True))

            for column_index, price in enumerate(row["prices"]):
                painter.drawText(QRectF(110 + 120 * column_index, y, 116, _ROW), Qt.AlignmentFlag.AlignVCenter,
                                 price)

            # The gap as a bar, the break-even marked across it.
            track = QRectF(bar_left, middle.center().y() - 5, bar_width - 70, 10)
            painter.fillRect(track, QColor(95, 200, 245, 22))

            if row["gap"] is not None:
                filled = track.width() * float(min(row["gap"], widest) / widest)
                painter.fillRect(QRectF(track.left(), track.top(), max(filled, 1.5), track.height()),
                                 _GAIN if row["clears"] else _LOSS)
                mark = track.left() + track.width() * float(needed / widest)
                painter.setPen(QPen(_AMBER, 2))
                painter.drawLine(QPointF(mark, track.top() - 4), QPointF(mark, track.bottom() + 4))
                gap_text = f"{row['gap']:.3f}%"
            else:
                gap_text = "one only"

            painter.setPen(_GAIN if row["clears"] else _MUTED)
            painter.setFont(_font(9, mono=True))
            painter.drawText(QRectF(track.right() + 6, y, 64, _ROW), Qt.AlignmentFlag.AlignVCenter, gap_text)

            net = row["net"]
            painter.setPen(_MUTED if net is None else _GAIN if net > 0 else _LOSS)
            painter.setFont(_font(10, bold=True, mono=True))
            painter.drawText(QRectF(best_left, y + 4, _BEST, 18), Qt.AlignmentFlag.AlignLeft, money_text(net))
            painter.setPen(_MUTED)
            painter.setFont(_font(8))
            painter.drawText(QRectF(best_left, y + 22, _BEST, 16), Qt.AlignmentFlag.AlignLeft, row["route"])

        footer = top + _HEADER + len(table) * _ROW + 14
        painter.setPen(_MUTED)
        painter.setFont(_font(8))
        painter.drawText(QRectF(0, footer, width, 36), Qt.TextFlag.TextWordWrap,
                         "Paper only: read from public pools at one block. No wallet, nothing signed, nothing"
                         " traded. Prices move every block, and other bots compete for the same gaps.")
        painter.end()


class DexPanel(QWidget):
    """The DEX scan, projected beside the HUD."""

    show_scan = pyqtSignal(dict)
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

        self._title = QLabel("ARBITRAGE SCAN  /  ETHEREUM")
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

        self._scan = _Scan()
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setWidget(self._scan)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 16, _MARGIN - 8, 18)
        layout.addLayout(heading)
        layout.addSpacing(8)
        layout.addWidget(self._scroll, 1)

        self.show_scan.connect(self._on_scan)
        self.hide_requested.connect(self._on_hide)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._tick)

    def set_anchor(self, widget):
        self._anchor = widget

    def _on_scan(self, report):
        try:
            read = datetime.fromisoformat(report["generated_at"]).astimezone().strftime("%H:%M:%S")
        except (KeyError, ValueError):
            read = "-"

        self._subtitle.setText(f"{' and '.join(report.get('dexes') or [])}  -  block {report.get('snapshot_block')}"
                               f"  -  read {read}  -  ${_number(report.get('requested_trade_size_usd')) or 0:,.0f}"
                               " a trade")
        self._scan.show_report(report)
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
        if event.key() == Qt.Key.Key_Escape:
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
