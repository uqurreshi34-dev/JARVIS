"""Lines on the chart and the room a trade has before one (actions/chart_lines.py), and the gold backtest's room
rule (actions/gold_strategy.py) built on them.

Checked:

- room: the first line in a trade's way, less the buffer; none that way is
  None; a trade starting on top of a line has negative room;
- fifteen-minute candles build into four-hour ones as OANDA's day has them
  (from 17:00 New York, summer and winter), and the
  backtest's lines use only four-hour candles finished by then;
- a line lasts while price has not closed through it, however old its highs;
  once crossed, it stays for [lookback] candles, for the retest, then goes;
- the room rule: a sell aimed through a floor that has held is not taken,
  the same sell with nothing in its way is, and rules without it are
  unchanged; the rule has its own key for gold-trader.json.

    python tools/test_chart_lines.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import chart_lines, gold_strategy as gs  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


lines = [{"price": 4100.0}, {"price": 4180.0}]
space, line = chart_lines.room("sell", 4124.0, lines, 6.0)
check(space == 18.0 and line["price"] == 4100.0, "a sell's room: down to the floor, less the buffer")
space, line = chart_lines.room("buy", 4124.0, lines, 6.0)
check(space == 50.0 and line["price"] == 4180.0, "a buy's room: up to the ceiling, less the buffer")
check(chart_lines.room("buy", 4200.0, lines, 6.0) == (None, None), "nothing in the way: no line, no limit")
check(chart_lines.room("sell", 4103.0, lines, 6.0)[0] < 0, "selling on top of a floor: negative room")

# ---- how long a line lasts ------------------------------------------------------------------------------
BEGIN = datetime(2026, 8, 3, 1, tzinfo=timezone.utc)


def flat_with(spikes, crossed=None, count=100):
    """Candles flat at 100, highs of 130 at [spikes], and one close at 140 at [crossed]."""
    rows = []

    for index in range(count):
        high = 130.0 if index in spikes else 101.0
        close = 140.0 if index == crossed else 100.0
        rows.append((BEGIN + timedelta(hours=4 * index), 100.0, max(high, close + 1), 99.0, close))

    return rows


def ceiling(candles):
    found = chart_lines.levels(candles, 2.0, 3, lookback=60)
    return next((line for line in found if line["kind"] == "high" and abs(line["price"] - 130) < 1), None)


check(ceiling(flat_with((10, 20))) is not None,
      "highs 80 and 90 candles ago that price never closed above: still the ceiling, though both are older than"
      " the lookback")
check(ceiling(flat_with((10, 20), crossed=30)) is None, "closed through 70 candles ago: it has had its time")
check(ceiling(flat_with((10, 20), crossed=70)) is not None,
      "closed through 30 candles ago: kept, broken, for the retest")
check(ceiling(flat_with((70, 80))) is not None and chart_lines.levels(flat_with((10, 20), crossed=30), 2.0, 3) != [],
      "recent highs are a line as ever, and with no lookback nothing is dropped")


# ---- four-hour candles, finished ones only ------------------------------------------------------------

# 01:00 UTC is 21:00 in New York in summer: a four-hour candle of OANDA's day starts there.
START = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)


def fifteen(rows):
    """Fifteen-minute candles from (open, high, low, close) rows."""
    return [(START + timedelta(minutes=15 * index), *row) for index, row in enumerate(rows)]


def ranging(blocks, floor=4100.0, ceiling=4180.0):
    """[blocks] four-hour candles of fifteen-minute ones, swinging between [floor] and [ceiling] every five."""
    rows = []

    for block in range(blocks):
        phase = block % 10
        middle = floor + (ceiling - floor) * (phase if phase <= 5 else 10 - phase) / 5

        for _quarter in range(16):
            rows.append((middle, middle + 1, middle - 1, middle))

    return rows


candles = fifteen(ranging(60))
blocks = gs.four_hour(candles)
check(len(blocks) == 60 and all(gs.new_york_time(block[0]).hour in (17, 21, 1, 5, 9, 13) for block in blocks)
      and blocks[0][2] == max(candle[2] for candle in candles[:16]),
      "fifteen-minute candles build four-hour ones as OANDA's day has them, from 17:00 New York")
winter = datetime(2026, 12, 1, 22, tzinfo=timezone.utc)
check(gs.line_block(winter + timedelta(hours=3, minutes=45)) == winter and gs.line_block(winter) == winter,
      "and in winter, when 17:00 New York is 22:00 UTC")
book = gs.LineBook(candles)
lines, atr = book.at(candles[-1][0] + timedelta(minutes=15))
prices = [round(line["price"]) for line in lines]
check(any(abs(price - 4099) <= 2 for price in prices) and any(abs(price - 4181) <= 2 for price in prices) and atr,
      f"the floor and the ceiling are lines ({prices})")
early, _atr = book.at(candles[0][0] + timedelta(hours=3))
check(early == [], "before a four-hour candle has finished, nothing is known")

# ---- the room rule in the backtest -----------------------------------------------------------------------

# Price sits just above the floor; a sell signal there asks it to break the floor to reach its $30 target.
# Backtest trades count once closed, so price then falls $40 and the target is reached.
falling = [(4110.0 - step * 5, 4111.0 - step * 5, 4104.0 - step * 5, 4105.0 - step * 5) for step in range(8)]
rows = ranging(60) + [(4110.0, 4111.0, 4109.0, 4110.0)] * 8 + falling
candles = fifteen(rows)
selling = len(ranging(60)) + 7
real_signal = gs.signal
gs.signal = lambda index, *args, **kwargs: "sell" if index == selling else None
everywhere = (0, 24)

plain = gs.Rules(setup="pullback", trend_filter=True, session=everywhere)
roomy = gs.Rules(setup="pullback", trend_filter=True, session=everywhere, room=2.0)
check(len(gs.backtest(candles, plain)) == 1, "without the room rule, the sell into the floor is taken")
check(gs.backtest(candles, roomy) == [], "with it, a sell with the floor $10 below and a $30 target is not")
high = fifteen(ranging(60) + [(4170.0, 4171.0, 4169.0, 4170.0)] * 8
               + [(open_ + 60, high_ + 60, low + 60, close + 60) for open_, high_, low, close in falling])
check(len(gs.backtest(high, roomy)) == 1, "the same sell near the ceiling, the floor far below: taken")
gs.signal = real_signal
enough, room, line = gs.room_for(roomy, "sell", 4110.0, 10.0, lines, atr)
check(not enough and room < 2 and abs(line["price"] - 4099) <= 2, f"and the room is said in R ({room:.2f} R)")
check(roomy.key() == "pullback-1:3-room2" and roomy.key() in gs.REGISTRY and "room=2R" in roomy.name(),
      "the rule has its own key, for gold-trader.json, and its own row in the backtest")
check(plain.key() == "pullback-1:3", "rules without it keep their key")

sys.exit(1 if failures else 0)
