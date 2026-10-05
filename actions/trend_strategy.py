"""A slow trend-follower on daily candles: the kind of strategy with the longest record behind it.

Time-series momentum -- holding a market the way its last few months have
gone -- has been found across decades and dozens of futures markets, gold
among them (Moskowitz, Ooi and Pedersen, "Time Series Momentum", 2012).
This is its classic trader's form, the breakout of a channel:

1. Buy when a day closes above the highest high of the last [entry_days]
   days; sell (unless only buys are allowed) when one closes below the
   lowest low.
2. The first stop is 2 ATR (20 days) from the entry, held at the broker.
3. Out on whichever comes first: the stop; or, with "channel" exits, a
   close beyond the opposite extreme of the last [entry_days] / 2 days;
   with "atr" exits, a stop trailing 3 ATR behind the best close, moved
   only forward.

Trades last weeks, so they are held over weekends -- the gap risk that
makes the intraday trader close on Fridays is small beside a stop of
several days' movement -- and holding costs financing, which the backtest
charges every day a trade is open.

Results are counted in R, the first stop's distance, as well as in dollars
for one unit: prices move a long way over the years a daily strategy needs
to be judged on, and R is what risking a set share of the account turns
into money. Nothing here calls a model; every decision is arithmetic on the
candles. Shared by the backtest (tools/trend_backtest.py) and, once it has
earned one, a trader.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from actions.gold_strategy import average_true_range


ATR_LENGTH = 20
FIRST_STOP_ATR = 2.0
TRAIL_ATR = 3.0

# Financing on a position held overnight, a share of its value a year, charged both ways: what holding a
# CFD for weeks costs. OANDA's own rate varies; the backtest takes it as a setting.
FINANCING_PERCENT = 5.0


@dataclass(frozen=True)
class Rules:
    entry_days: int = 55         # a close beyond the last [entry_days] days' extreme
    exit: str = "channel"        # "channel": a close beyond the last entry_days // 2 days' opposite extreme;
                                 # "atr": a stop trailing TRAIL_ATR behind the best close
    sides: str = "both"          # "both", or "long": buys only
    spread: float = 0.0
    financing: float = FINANCING_PERCENT

    @property
    def exit_days(self):
        return max(2, self.entry_days // 2)

    def key(self):
        """A short, stable name for these rules."""
        return f"trend-{self.entry_days}-{self.exit}" + ("-long" if self.sides == "long" else "")

    def name(self):
        exits = f"{self.exit_days}-day channel" if self.exit == "channel" else f"{TRAIL_ATR:g} ATR trail"
        return f"{self.entry_days:3d}-day breakout, out on a {exits:15} {'buys only' if self.sides == 'long' else 'both ways'}"


@dataclass
class Trade:
    side: str
    opened: object
    entry: float
    stop: float
    risk: float                  # the first stop's distance, R
    best: float
    closed: object = None
    exit: float = None
    result: float = None         # dollars for one unit, after the spread and financing
    financing: float = 0.0
    reason: str = ""

    @property
    def r(self):
        return self.result / self.risk if self.risk else 0.0


def signal(index, candles, rules):
    """"buy", "sell" or None for the day at [index]: a close beyond the extreme of the [entry_days] before it."""
    if index < rules.entry_days:
        return None

    window = candles[index - rules.entry_days:index]
    close = candles[index][4]

    if close > max(candle[2] for candle in window):
        return "buy"

    if rules.sides == "both" and close < min(candle[3] for candle in window):
        return "sell"

    return None


def _channel_exit(index, candles, trade, rules):
    """Whether the day at [index] closes beyond the opposite extreme of the [exit_days] before it."""
    if index < rules.exit_days:
        return False

    window = candles[index - rules.exit_days:index]
    close = candles[index][4]

    if trade.side == "buy":
        return close < min(candle[3] for candle in window)

    return close > max(candle[2] for candle in window)


def _close(trade, when, price, reason, rules):
    trade.closed, trade.exit, trade.reason = when, price, reason
    held = max(1, (when - trade.opened).days)
    trade.financing = trade.entry * rules.financing / 100 * held / 365
    moved = (price - trade.entry) if trade.side == "buy" else (trade.entry - price)
    trade.result = moved - trade.financing


def _day_ends(candle):
    """When a day's candle closes: the next day's start, as OANDA's daily candles run 17:00 to 17:00 New York."""
    return candle[0] + timedelta(days=1)


def backtest(candles, rules=Rules()):
    """Every trade [rules] would have closed over [candles] (daily, bid), oldest first.

    One trade at a time. A trade opens at the close of the day that signals
    (bought at the ask, sold at the bid). Each later day, the stop is
    checked first, against the day's range: a day that opens beyond the
    stop -- after a weekend, say -- is filled at its open, not at the stop,
    as a broker would. Then, at the close, a channel exit, or the trailing
    stop moved for the next day.
    """
    atrs = average_true_range(candles, ATR_LENGTH)
    trades = []
    trade = None

    for index, candle in enumerate(candles):
        _start, open_, high, low, close = candle

        if trade is not None:
            if trade.side == "buy" and low <= trade.stop:
                _close(trade, _day_ends(candle), min(open_, trade.stop), "stop", rules)
            elif trade.side == "sell" and high + rules.spread >= trade.stop:
                _close(trade, _day_ends(candle), max(open_ + rules.spread, trade.stop), "stop", rules)
            elif rules.exit == "channel" and _channel_exit(index, candles, trade, rules):
                _close(trade, _day_ends(candle), close if trade.side == "buy" else close + rules.spread, "channel", rules)
            elif rules.exit == "atr" and atrs[index]:
                if trade.side == "buy":
                    trade.best = max(trade.best, close)
                    trade.stop = max(trade.stop, trade.best - TRAIL_ATR * atrs[index])
                else:
                    trade.best = min(trade.best, close + rules.spread)
                    trade.stop = min(trade.stop, trade.best + TRAIL_ATR * atrs[index])

            if trade.closed is not None:
                trades.append(trade)
                trade = None

            continue

        side = signal(index, candles, rules)

        if side and atrs[index]:
            entry = close + rules.spread if side == "buy" else close
            distance = FIRST_STOP_ATR * atrs[index]
            stop = entry - distance if side == "buy" else entry + distance
            trade = Trade(side, _day_ends(candle), entry, stop, distance, entry)

    return trades


@dataclass
class Summary:
    trades: int = 0
    wins: int = 0
    net_r: float = 0.0
    net: float = 0.0
    worst_run: int = 0
    deepest_r: float = 0.0
    average_days: float = 0.0
    results: list = field(default_factory=list)

    @property
    def win_rate(self):
        return self.wins / self.trades if self.trades else 0.0


def summarise(trades):
    summary = Summary()
    run = 0
    peak = balance = 0.0
    days = 0

    for trade in trades:
        summary.trades += 1
        summary.net_r += trade.r
        summary.net += trade.result
        summary.results.append(trade.r)
        days += (trade.closed - trade.opened).days

        if trade.result > 0:
            summary.wins += 1
            run = 0
        else:
            run += 1
            summary.worst_run = max(summary.worst_run, run)

        balance += trade.r
        peak = max(peak, balance)
        summary.deepest_r = max(summary.deepest_r, peak - balance)

    summary.average_days = days / summary.trades if summary.trades else 0.0
    return summary


def parts(candles, trades, count):
    """Net R of [trades] by the one of [count] equal parts of [candles]' span each opened in."""
    first = candles[0][0]
    step = (candles[-1][0] - first) / count
    nets = [0.0] * count

    for trade in trades:
        nets[min(count - 1, max(0, int((trade.opened - first) / step)))] += trade.r

    return nets


# The versions tested: three channel lengths (a month, a quarter, about five months), the two exits, and
# buys only beside both ways -- gold has risen over most of the years tested, so selling it may only cost.
VARIANTS = [Rules(entry_days=days, exit=exit_, sides=sides)
            for days in (20, 55, 100) for exit_ in ("channel", "atr") for sides in ("both", "long")]

REGISTRY = {rules.key(): rules for rules in VARIANTS}
