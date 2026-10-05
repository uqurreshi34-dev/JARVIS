"""The gold strategy backtest (tools/gold_backtest.py), on made-up prices.

No network: the price files are built here in Dukascopy's own format.
Checked:

- a day's compressed one-minute file is read back to the prices written,
  in dollars, at the right minutes; an empty day is no prices;
- the address counts months from 0, as Dukascopy does;
- one-minute candles build into fifteen-minute ones on the clock;
- UK time: GMT in winter, BST from the last Sunday of March to the last of
  October, so 10:00 to 14:00 UK is the right hours of UTC either side;
- the Bollinger bands and Wilder's RSI against values worked out by hand;
- a buy: the touch, two green candles and RSI back above 30 open it at the
  close plus the spread; a sell likewise; nothing opens outside the hours,
  on a red confirmation, without RSI turning, or while squeezed;
- a trade closes at its target or its stop, the stop when a candle
  reaches both, and a sell's exits allow for the spread;
- one trade at a time, and the summary counts wins, the worst run of
  losses and the deepest dip.

    python tools/test_gold_backtest.py
"""

import lzma
import math
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import gold_strategy as gb  # noqa: E402
from tools import gold_backtest as cli  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


# ---- the price files -----------------------------------------------------------------------------

day = date(2026, 10, 2)
rows = [(0, 4169.550, 4164.230, 4163.570, 4171.050, 1.5), (60, 4164.230, 4165.000, 4164.000, 4165.500, 0.5)]
raw = lzma.compress(b"".join(struct.pack(">5if", seconds, *(round(price * 1000) for price in prices[:4]), prices[4])
                             for seconds, *prices in rows))
candles = gb.decode_day(day, raw)
check(candles[0] == (utc(2026, 10, 2, 0, 0), 4169.55, 4171.05, 4163.57, 4164.23)
      and candles[1][0] == utc(2026, 10, 2, 0, 1),
      f"a day's file reads back as dollars, at its minutes ({candles[0]})")
check(gb.decode_day(day, b"") == [], "an empty day is no prices")
check(gb._day_url(date(2026, 1, 5)).endswith("/XAUUSD/2026/00/05/BID_candles_min_1.bi5"),
      "the address counts months from 0, as Dukascopy does")

minutes = [(utc(2026, 10, 2, 10, minute), 100 + minute, 101 + minute, 99 + minute, 100.5 + minute)
           for minute in range(0, 31)]
built = gb.fifteen_minute(minutes)
check(len(built) == 3 and built[0] == (utc(2026, 10, 2, 10, 0), 100, 115, 99, 114.5)
      and built[1][0] == utc(2026, 10, 2, 10, 15),
      "one-minute candles build into fifteen-minute ones on the clock: first open, highest, lowest, last close")

# ---- UK time ------------------------------------------------------------------------------------

check(gb._last_sunday(2026, 3) == date(2026, 3, 29) and gb._last_sunday(2026, 10) == date(2026, 10, 25),
      "the clocks change on the last Sundays of March and October")
check(gb.uk_time(utc(2026, 7, 1, 9, 0)).hour == 10 and gb.uk_time(utc(2026, 12, 1, 10, 0)).hour == 10,
      "UK time is UTC+1 in summer and UTC in winter")
check(gb.in_session(utc(2026, 7, 1, 9, 15)) and not gb.in_session(utc(2026, 7, 1, 13, 15))
      and gb.in_session(utc(2026, 12, 1, 13, 45)) and not gb.in_session(utc(2026, 12, 1, 9, 45)),
      "10:00 to 14:00 UK is the right hours of UTC either side of the clock change")

check(gb.new_york_time(utc(2026, 10, 5, 12, 0)).hour == 8 and gb.new_york_time(utc(2026, 12, 1, 13, 0)).hour == 8
      and gb.new_york_time(utc(2026, 11, 1, 5, 59)).hour == 1 and gb.new_york_time(utc(2026, 11, 1, 6, 0)).hour == 1
      and gb.new_york_time(utc(2026, 3, 8, 7, 0)).hour == 3,
      "New York time: UTC-4 from the second Sunday of March to the first of November, UTC-5 otherwise")
check(gb.in_session(utc(2026, 10, 5, 12, 15), (8, 12), "new_york") and not gb.in_session(utc(2026, 10, 5, 16, 15), (8, 12), "new_york")
      and gb.in_session(utc(2026, 10, 26, 12, 15), (8, 12), "new_york") and not gb.in_session(utc(2026, 10, 26, 11, 15), (8, 12), "new_york")
      and not gb.in_session(utc(2026, 10, 26, 16, 15), (8, 12), "new_york")
      and gb.in_session(utc(2026, 11, 2, 13, 15), (8, 12), "new_york") and not gb.in_session(utc(2026, 11, 2, 12, 15), (8, 12), "new_york"),
      "8:00 to 12:00 New York is 13:00 to 17:00 UK, 12:00 to 16:00 UK in the week between the clock changes")

# ---- the indicators -----------------------------------------------------------------------------

flat = gb.bollinger([5.0] * 20)
check(flat[18] is None and flat[19] == (5.0, 5.0, 5.0), "bands appear after twenty closes; flat prices, no width")
bands = gb.bollinger([float(value) for value in range(1, 21)], length=20)
check(abs(bands[19][1] - 10.5) < 1e-9 and abs(bands[19][2] - (10.5 + 2 * 5.766281297335398)) < 1e-9,
      "the bands are the average plus and minus two standard deviations of the closes")

rising = gb.rsi([float(value) for value in range(30)])
falling = gb.rsi([float(30 - value) for value in range(30)])
check(rising[13] is None and rising[14] == 100.0 and falling[20] == 0.0, "RSI: 100 only rising, 0 only falling")
mixed = gb.rsi([10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11.0])
check(abs(mixed[14] - 50.0) < 1e-9 and mixed[15] > 50.0, "and 50 when the gains and losses are equal")


# ---- the strategy ---------------------------------------------------------------------------------

def series(closes, start=utc(2026, 7, 1, 6, 0)):
    """Fifteen-minute candles that open at the previous close, each with a little wick."""
    made, previous = [], closes[0]

    for index, close in enumerate(closes):
        made.append((start + timedelta(minutes=15 * index), previous, max(previous, close) + 0.2,
                     min(previous, close) - 0.2, close))
        previous = close

    return made


def candle_at(made, index, open_, high, low, close):
    made[index] = (made[index][0], open_, high, low, close)


base = [4000.0 + (1 if index % 2 else -1) for index in range(40)]
falls = [4000.0 - 6 * step for step in range(1, 9)]
prices = base + falls + [3954.0, 3972.0] + [3972.0 + step for step in range(1, 60)]
made = series(prices)
touch = len(base) + len(falls)
candle_at(made, touch, 3950.0, 3954.5, 3930.0, 3954.0)       # candle 1: a long wick through the lower band, green
candle_at(made, touch + 1, 3954.0, 3972.5, 3953.5, 3972.0)   # candle 2: green, and strongly
confirm_close = made[touch + 1][0] + timedelta(minutes=15)

rules = gb.Rules(squeeze_filter=False, session=(0, 24))
bands, rsis, squeezes, atrs, averages, fast = gb.indicators(made)
check(made[touch][3] <= bands[touch][0] and min(rsis[touch - 2:touch + 1]) < 30 <= rsis[touch + 1],
      "the made-up prices touch the band, and RSI dips under 30 and comes back")
check(gb.signal(touch + 1, made, bands, rsis, squeezes, rules) == "buy", "a touch, two green candles, RSI back: buy")

red = list(made)
candle_at(red, touch + 1, 3954.0, 3954.5, 3950.0, 3951.0)
check(gb.signal(touch + 1, red, *gb.indicators(red)[:3], rules) is None, "a red confirmation candle: nothing")

check(gb.signal(touch + 1, made, bands, [50.0] * len(rsis), squeezes, rules) is None,
      "RSI never under 30: nothing, as written")
check(gb.signal(touch + 1, made, bands, [50.0] * len(rsis), squeezes, gb.Rules(rsi="not_extreme", squeeze_filter=False)) == "buy",
      "though 'not extreme' takes it")

check(gb.signal(touch + 1, made, bands, rsis, [True] * len(made), gb.Rules(session=(0, 24))) is None,
      "bands squeezed: nothing")
check(gb.signal(touch + 1, made, bands, rsis, squeezes, gb.Rules(touch="close", squeeze_filter=False)) is None,
      "a wick alone is not a close beyond the band")

trades = gb.backtest(made, rules)
first = trades[0] if trades else None
check(first is not None and first.side == "buy" and first.opened == confirm_close
      and abs(first.entry - (3972.0 + gb.SPREAD)) < 1e-9 and abs(first.stop - (first.entry - 10)) < 1e-9
      and abs(first.target - (first.entry + 30)) < 1e-9,
      "the buy opens at the confirmation's close plus the spread, stop $10 below, target $30 above")
check(first is not None and first.reason == "target" and abs(first.result - 30.0) < 1e-9,
      f"and the rise afterwards takes it to its target, $30 ({first.result if first else None})")

check(gb.backtest(made, gb.Rules(squeeze_filter=False, session=(20, 22))) == [],
      "outside the hours: no trade")

# ---- closing trades --------------------------------------------------------------------------------

start = utc(2026, 7, 1, 10, 0)
buy = gb.Trade("buy", start, 100.0, 90.0, 130.0)
check(not gb._settle(buy, (start, 100, 129.9, 90.1, 120), gb.Rules()), "neither reached: still open")
check(gb._settle(buy, (start, 100, 131, 89, 120), gb.Rules()) and buy.reason == "stop" and buy.result == -10.0,
      "both reached in one candle: the stop, not the target")

sell = gb.Trade("sell", start, 100.0, 110.0, 70.0)
check(not gb._settle(sell, (start, 100, 105.0, 70.0, 90), gb.Rules(spread=0.8)),
      "a sell's target needs the ask there: the bid at 70 is not enough")
check(gb._settle(sell, (start, 100, 105.0, 69.0, 90), gb.Rules(spread=0.8)) and sell.reason == "target" and sell.result == 30.0,
      "and the bid at 69, the ask at 69.80, is")
sell = gb.Trade("sell", start, 100.0, 110.0, 70.0)
check(gb._settle(sell, (start, 100, 109.3, 95, 100), gb.Rules(spread=0.8)) and sell.reason == "stop",
      "a sell is stopped by the ask: the bid at 109.30 is the ask at 110.10")

summary = gb.summarise([gb.Trade("buy", start, 0, 0, 0, result=value) for value in (30, -10, -10, -10, 30, -10)])
check(summary.trades == 6 and summary.wins == 2 and summary.net == 20 and summary.worst_run == 3
      and summary.deepest == 30, "the summary: trades, wins, net, worst run of losses, deepest dip")

check(len(gb.VARIANTS) == 52 and len(gb.REGISTRY) == 52 and "range30-1:3" in gb.REGISTRY and "retest60-atr-1:3" in gb.REGISTRY and "pullback-atr-1:3" in gb.REGISTRY and "orb-1:3" in gb.REGISTRY and "orb-atr-1:2-no-trend" in gb.REGISTRY and
      {"pullback-1:1.5", "pullback-1:2", "pullback-1:3", "pullback-1:3-be", "pullback-1:4", "pullback-1:4-be"}
      <= set(gb.REGISTRY) and gb.AS_WRITTEN in gb.VARIANTS and gb.CHOSEN in gb.VARIANTS
      and gb.REGISTRY["bounce-1:3-be"] == gb.CHOSEN, "every combination of the open choices is run")

# ---- exits from the bands -------------------------------------------------------------------------

banded = gb.backtest(made, gb.Rules(squeeze_filter=False, session=(0, 24), exits="bands"))
first_banded = banded[0] if banded else None
check(first_banded is not None and first_banded.side == "buy" and first_banded.target is None
      and abs(first_banded.stop - (3930.0 - gb.WICK_BUFFER)) < 1e-9,
      "exits from the bands: the stop just past candle 1's wick, the target the middle band")
check(first_banded is not None and first_banded.reason == "target"
      and abs(first_banded.exit - gb.bollinger([candle[4] for candle in made])[made.index(
          next(candle for candle in made if candle[0] + timedelta(minutes=15) == first_banded.closed)) - 1][1]) < 1e-9,
      "and it closes at the middle band as it stood when that candle began")

aiming = gb.Trade("buy", start, 100.0, 95.0, None)
check(not gb._settle(aiming, (start, 100, 104, 96, 103), gb.Rules(), middle=105.0)
      and gb._settle(aiming, (start, 103, 106, 101, 105), gb.Rules(), middle=105.0)
      and aiming.exit == 105.0 and aiming.result == 5.0, "a trade aiming for the middle band closes when it gets there")

opened = [gb.Trade("buy", made[0][0] + timedelta(hours=hours), 0, 0, 0, result=value)
          for hours, value in ((1, 10.0), (2, -5.0), (20, 7.0))]
check(cli._halves(made, opened) == (5.0, 7.0), "each result counted in the half of the period it opened in")

# ---- the trend filter and the trailing exit -------------------------------------------------------

steady = [(utc(2026, 7, 1) + timedelta(minutes=15 * index), 100.0, 103.0, 99.0, 101.0) for index in range(20)]
atr = gb.average_true_range(steady)
check(atr[12] is None and abs(atr[13] - 4.0) < 1e-9 and abs(atr[19] - 4.0) < 1e-9,
      "ATR: the true range (here $4, the previous close inside every candle) averaged over 14 candles")
gappy = steady[:14] + [(steady[14][0], 110.0, 111.0, 109.0, 110.0)]
check(abs(gb.average_true_range(gappy)[14] - (4.0 * 13 + 10.0) / 14) < 1e-9,
      "a gap counts: the range from the previous close, $10, not just the candle's own $2")
average = gb.moving_average([10.0] * 200 + [20.0])
check(average[198] is None and average[199] == 10.0 and abs(average[200] - (10.0 + (2 / 201) * 10.0)) < 1e-9,
      "the 200-candle average starts as the plain average, then follows each close")

check(gb.signal(touch + 1, made, bands, rsis, squeezes, gb.Rules(squeeze_filter=False, trend_filter=True),
                [made[touch + 1][4] + 50.0] * len(made)) is None,
      "trend filter: a buy below the 200-candle average is not taken")
check(gb.signal(touch + 1, made, bands, rsis, squeezes, gb.Rules(squeeze_filter=False, trend_filter=True),
                [made[touch + 1][4] - 50.0] * len(made)) == "buy",
      "and above it, it is")
check(gb.signal(touch + 1, made, bands, rsis, squeezes, gb.Rules(squeeze_filter=False, trend_filter=True),
                [None] * len(made)) is None,
      "and with no average yet, nothing")

trailing = gb.Trade("buy", start, 100.0, 94.0, None, trailing=True, risk=6.0, best=100.0)
check(not gb._settle(trailing, (start, 100, 104, 99, 103), gb.Rules(), middle=101.0, atr=2.0) and trailing.stop == 94.0,
      "trailing: short of 1 R, the stop stays, and the middle band is no target")
check(not gb._settle(trailing, (start, 103, 106.5, 102, 106), gb.Rules(), atr=2.0) and trailing.stop == 102.5,
      "past 1 R the stop leaves the entry behind: 2 ATR under the best price, $106.50 - $4")
check(not gb._settle(trailing, (start, 106, 106.2, 103, 104), gb.Rules(), atr=2.0) and trailing.stop == 102.5,
      "and never moves back when the price does")
check(gb._settle(trailing, (start, 104, 104.5, 102, 103), gb.Rules(), atr=2.0) and trailing.reason == "stop"
      and abs(trailing.result - 2.5) < 1e-9, "the trailing stop takes the profit: $2.50 here")

even = gb.Trade("buy", start, 100.0, 90.0, 130.0, breakeven=True, risk=10.0)
check(not gb._settle(even, (start, 100, 109, 95, 108), gb.Rules()) and even.stop == 90.0,
      "breakeven: $9 up of a $10 risk, the stop stays")
check(not gb._settle(even, (start, 108, 110.5, 104, 109), gb.Rules()) and even.stop == 100.0,
      "$10 up: the stop is the entry")
check(gb._settle(even, (start, 109, 109.5, 99, 100), gb.Rules()) and even.result == 0.0 and even.reason == "stop",
      "and a fall back closes it at no loss")
check(gb.Rules(target=20.0, breakeven=True).name().startswith("rsi=turned      exits=1:2+BE"),
      "the targets are named as their ratio to the stop")

to_entry = gb.Trade("buy", start, 100.0, 94.0, None, trailing=True, risk=6.0, best=100.0)
gb._settle(to_entry, (start, 100, 106, 99, 105), gb.Rules(), atr=4.0)
check(to_entry.stop == 100.0, "at 1 R with a wide ATR the stop goes to the entry: it can no longer lose")

short = gb.Trade("sell", start, 100.0, 106.0, None, trailing=True, risk=6.0, best=100.0)
gb._settle(short, (start, 100, 101, 92.2, 93), gb.Rules(spread=0.8), atr=2.0)
check(abs(short.best - 93.0) < 1e-9 and abs(short.stop - 97.0) < 1e-9,
      "a sell trails from the ask: best $93.00 (bid $92.20 plus the spread), stop 2 ATR above, $97")

# The rise after the buy, then a fall: the trailing stop rides the rise and is caught by the fall.
falling = made + [(made[-1][0] + timedelta(minutes=15 * step), made[-1][4] - 3 * (step - 1), made[-1][4] - 3 * (step - 1) + 0.2,
                   made[-1][4] - 3 * step - 0.2, made[-1][4] - 3 * step) for step in range(1, 30)]
trailed = gb.backtest(falling, gb.Rules(squeeze_filter=False, session=(0, 24), exits="trail"))
check(trailed and trailed[0].trailing and abs(trailed[0].risk - gb.TRAIL_FIRST_STOP * atrs[touch + 1]) < 1e-9
      and trailed[0].reason == "stop" and trailed[0].result > 20,
      f"a trailing trade starts 1.5 ATR from its entry, rides the rise and keeps most of it "
      f"({trailed[0].result if trailed else None})")

# ---- the other setups, ATR exits, several together ------------------------------------------------

def plain(start, prices):
    return series(prices, start=start)


uptrend = [4000.0]
for step in range(259):
    uptrend.append(uptrend[-1] + (1.5 if step % 2 else -1.0))   # up, with dips: RSI in the middle
dip = plain(utc(2026, 7, 1, 0, 0), uptrend + [uptrend[-1] - 2.0, uptrend[-1] + 2.0])
w = gb.indicators(dip)
last = len(dip) - 1
pullback = gb.Rules(setup="pullback", trend_filter=True, exits="atr", breakeven=True)
candle_at(dip, last - 1, dip[last - 1][1], dip[last - 1][1] + 0.1, w[5][last - 1] - 0.5, dip[last - 1][4])
candle_at(dip, last, dip[last - 1][4], uptrend[-1] + 2.2, dip[last - 1][4] - 0.1, uptrend[-1] + 2.0)
w = gb.indicators(dip)
check(w[4][last] < dip[last][4] and w[5][last - 1] > w[4][last - 1] and dip[last - 1][3] <= w[5][last - 1],
      "made-up prices: a steady uptrend, candle 1 dipping to the 20-candle average")
check(40 <= w[1][last] <= 70 and gb.signal(last, dip, w[0], w[1], w[2], pullback, w[4], w[5]) == "buy",
      f"a pullback: candle 2 closes green above candle 1 and the average, in the uptrend -- buy (RSI {w[1][last]:.0f})")
check(gb.signal(last, dip, w[0], w[1], w[2], pullback, [99999.0] * len(dip), w[5]) is None,
      "but not below the 200-candle average")

flat = [4000.0 + (0.5 if step % 2 else -0.5) for step in range(300)]
burst = plain(utc(2026, 7, 1, 0, 0), flat + [4000.0, 4003.2])
wb = gb.indicators(burst)
breakout = gb.Rules(setup="breakout", trend_filter=False, exits="atr", ratio=2.0, breakeven=True)
check(wb[2][len(burst) - 2] and gb.signal(len(burst) - 1, burst, wb[0], wb[1], wb[2], breakout, wb[4], wb[5]) == "buy",
      "a breakout: squeezed on candle 1, candle 2 closes green above the upper band -- buy")
check(gb.signal(len(burst) - 1, burst, wb[0], wb[1], [False] * len(burst), breakout, wb[4], wb[5]) is None,
      "and with no squeeze before it, a big candle is not a breakout")

atr_rules = gb.Rules(exits="atr", ratio=3.0, breakeven=True)
check(gb.exits_for(atr_rules, 100.0, "buy", 4.0) == (94.0, 118.0, 6.0) and gb.exits_for(atr_rules, 100.0, "sell", 4.0)
      == (106.0, 82.0, 6.0) and gb.exits_for(atr_rules, 100.0, "buy", None) is None,
      "ATR exits: stop 1.5 ATR away, target three times the stop; none without an ATR")
check(gb.exits_for(gb.CHOSEN, 100.0, "buy", 4.0) == (90.0, 130.0, 10.0), "fixed exits: $10 and $30, whatever the ATR")

alone = gb.backtest(made, gb.Rules(squeeze_filter=False, session=(0, 24)))
both = gb.backtest(made, [gb.Rules(setup="pullback", session=(0, 24)), gb.Rules(squeeze_filter=False, session=(0, 24))])
check(len(both) >= len(alone) and all(trade.rules is not None for trade in both),
      "several setups run together, one trade at a time, each trade knowing the rules that opened it")
check(gb.CHOSEN.key() == "bounce-1:3-be" and gb.Rules(setup="pullback", trend_filter=True, exits="atr", ratio=2.0,
                                                       breakeven=True).key() == "pullback-atr-1:2-be",
      "each version has a short, stable key")
check(cli.worth_trading(gb.Summary(trades=40), 1.0, 2.0) and not cli.worth_trading(gb.Summary(trades=20), 1.0, 2.0)
      and not cli.worth_trading(gb.Summary(trades=40), -1.0, 2.0),
      "worth trading: at least 30 trades and money made in both halves")

# ---- OANDA's own prices ---------------------------------------------------------------------------

import contextlib  # noqa: E402
import io  # noqa: E402

from actions import oanda  # noqa: E402

walk = series([4000.0 + 5 * math.sin(step / 7.0) for step in range(800)], start=utc(2026, 9, 1, 0, 0))


class History:
    def history(self, start, end, name="XAU_USD"):
        History.asked = name
        # The ask $0.50 above the bid in the trading window, $3 at night.
        return walk, [candle[4] + (0.5 if gb.in_session(candle[0] + timedelta(minutes=15)) else 3.0) for candle in walk]


# What the trader is set to trade, as gold-trader.json would say: the pullback, without breakeven.
cli.traded_setups = lambda: [gb.REGISTRY["pullback-1:3"]]
real_client = oanda.Client
oanda.Client = History
printed = io.StringIO()

try:
    with contextlib.redirect_stdout(printed):
        code = cli.main(["--source", "oanda", "--days", "30"])
finally:
    oanda.Client = real_client

output = printed.getvalue()
marked = [line for line in output.splitlines() if line.startswith("+ ")]
check(len(marked) == 1 and marked[0].rstrip().endswith("pullback-1:3")
      and "What the trader trades (pullback-1:3)" in output,
      "the rows marked + are what the trader is set to trade, and those are what is compared with several open")
check(code == 0 and "your OANDA demo account" in output and "OANDA gold, 800 fifteen-minute candles" in output
      and "Spread $0.50" in output,
      "with OANDA, its own candles and the spread as it was in the trading window, not at night")

oanda.Client = History
printed = io.StringIO()

try:
    with contextlib.redirect_stdout(printed):
        code = cli.main(["--source", "oanda", "--days", "30", "--hours", "10", "16"])
        refused = cli.main(["--source", "oanda", "--hours", "16", "10"])
finally:
    oanda.Client = real_client

output = printed.getvalue()
oanda.Client = History
zoned = io.StringIO()

try:
    with contextlib.redirect_stdout(zoned):
        zone_code = cli.main(["--source", "oanda", "--days", "30", "--zone", "new_york", "--hours", "8", "12"])
finally:
    oanda.Client = real_client

check(zone_code == 0 and "candles closing 8:00 to 12:00 New York time" in zoned.getvalue(),
      "hours can be set on New York's clock (--zone new_york)")
oanda.Client = History
silver = io.StringIO()

try:
    with contextlib.redirect_stdout(silver):
        silver_code = cli.main(["--source", "oanda", "--days", "30", "--instrument", "xag_usd"])
        no_feed = cli.main(["--source", "dukascopy", "--instrument", "XAG_USD"])
        bad_name = cli.main(["--source", "oanda", "--instrument", "silver"])
finally:
    oanda.Client = real_client

rows = [line for line in silver.getvalue().splitlines() if line[:2] in ("  ", "+ ", "* ") and "exits=" in line]
check(silver_code == 0 and History.asked == "XAG_USD" and "OANDA XAG_USD" in silver.getvalue() and rows
      and all("exits=ATR" in line or "exits=trail" in line for line in rows) and not any(line.startswith("+ ") for line in rows),
      "silver: OANDA's XAG_USD prices, only the exits sized by ATR, and nothing marked as the gold trader's")
check(no_feed == 1 and bad_name == 1, "and refused without OANDA's prices, or with a name OANDA would not know")
check(code == 0 and "candles closing 10:00 to 16:00 UK time" in output and refused == 1
      and "one trade at a time and with up to 3 open at once" in output,
      "other hours can be tested (--hours 10 16), and backwards hours are refused")

# ---- fetching -------------------------------------------------------------------------------------

import os  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402


class Answer:
    def __init__(self, status, content=b""):
        self.status_code, self.content, self.ok = status, content, 200 <= status < 300


class Feed:
    """Dukascopy as it behaves: busy now and then, one day refused for good."""

    def __init__(self, refusals, refused_for_good=()):
        self.refusals, self.refused_for_good, self.asked = dict(refusals), set(refused_for_good), []
        self.headers = {}

    def get(self, url, timeout=0):
        self.asked.append(url)
        year, month, day_of_month = url.split("/XAUUSD/")[1].split("/")[:3]
        wanted = date(int(year), int(month) + 1, int(day_of_month))

        if wanted in self.refused_for_good:
            return Answer(503)

        if self.refusals.get(wanted, 0) > 0:
            self.refusals[wanted] -= 1
            return Answer(503)

        return Answer(200, raw)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


slept = []
gb.time = types.SimpleNamespace(sleep=slept.append, monotonic=lambda: 0.0)
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
said = []
first_day, last_day = date(2026, 9, 1), date(2026, 9, 30)
weekdays = [first_day + timedelta(days=step) for step in range(30) if (first_day + timedelta(days=step)).weekday() != 5]

feed = Feed({date(2026, 9, 2): 2}, refused_for_good={date(2026, 9, 3)})
sys.modules["requests"] = types.SimpleNamespace(Session=lambda: feed)
got = gb.minute_candles(first_day, last_day, progress=said.append)
check(len(got) == 2 * (len(weekdays) - 1) and slept and sum(url.count("/2026/08/02/") for url in feed.asked) == 3,
      "a day refused twice is asked again, after a wait, and arrives")
check(any(f"0 days already here, {len(weekdays)} to download" in line and "Ctrl+C is safe" in line for line in said)
      and any(f"prices: {len(weekdays)} of {len(weekdays)} days" in line for line in said),
      "it says how much there is to download, that stopping is safe, and how far it has got")
check(any("1 of" in line and "03 Sep (HTTP 503)" in line for line in said) and any("Testing without them" in line for line in said),
      "a day refused for good is named, and the test goes on without it")

feed = Feed({})
sys.modules["requests"] = types.SimpleNamespace(Session=lambda: feed)
again = gb.minute_candles(first_day, last_day, progress=said.append)
check(len(again) == len(got) + 2 and feed.asked and all("/2026/08/03/" in url for url in feed.asked),
      "run again, the days already fetched are kept and only the missing one is asked for")

feed = Feed({}, refused_for_good={date(2026, 11, 1) + timedelta(days=step) for step in range(30)})
sys.modules["requests"] = types.SimpleNamespace(Session=lambda: feed)

try:
    gb.minute_candles(date(2026, 11, 1), date(2026, 11, 30), progress=said.append)
    stopped = False
except RuntimeError as error:
    stopped = "run it again" in str(error)

check(stopped, "too many days missing: it stops and says to run it again, rather than test on scraps")


# ---- a setup forming, a candle early ------------------------------------------------------------

check(gb.forming(touch, made, bands, squeezes, rules) == "buy",
      "candle 1 alone -- a touch of the lower band, closing green -- is a buy that may be forming")
check(gb.forming(touch + 1, made, bands, squeezes, rules) is None, "and the confirming candle is not another")
check(gb.forming(touch, made, bands, [True] * len(made), gb.Rules(session=(0, 24))) is None,
      "bands squeezed: no warning, as no trade could follow")
check(gb.forming(touch, made, bands, squeezes, gb.Rules(squeeze_filter=False, trend_filter=True), averages, fast) is None,
      "a buy below the 200-candle average, with the trend filter: no warning either")
check(gb.forming(touch, made, bands, squeezes, gb.Rules(setup="breakout"), averages, fast) is None,
      "a breakout is one candle, so it gives no warning")
check(all(gb.forming(index, made, bands, squeezes, rules) == "buy"
          for index in range(len(made) - 1) if gb.signal(index + 1, made, bands, rsis, squeezes, rules) == "buy"),
      "every buy the strategy takes was warned of a candle before")

# ---- the New York opening-range breakout ---------------------------------------------------------

def day_at(start, closes):
    """Candles from [start] (UTC), opening at the last close, each a dollar either side; changed after."""
    made, previous = [], closes[0]
    for step, close in enumerate(closes):
        made.append((start + timedelta(minutes=15 * step), previous, max(previous, close) + 0.2,
                     min(previous, close) - 0.2, close))
        previous = close
    return made


ny = day_at(utc(2026, 10, 6, 0, 0), [4000.0] * 96)          # a quiet day; New York is UTC-4 in October
at = {candle[0]: index for index, candle in enumerate(ny)}
range_first, range_second = at[utc(2026, 10, 6, 13, 30)], at[utc(2026, 10, 6, 13, 45)]   # 9:30 and 9:45 New York
ny[range_first] = (ny[range_first][0], 4000.0, 4005.0, 3996.0, 4002.0)
ny[range_second] = (ny[range_second][0], 4002.0, 4004.0, 3995.0, 3998.0)
inside, breaks, again = at[utc(2026, 10, 6, 14, 0)], at[utc(2026, 10, 6, 14, 15)], at[utc(2026, 10, 6, 14, 30)]
ny[inside] = (ny[inside][0], 3998.0, 4004.5, 3997.0, 4004.0)
ny[breaks] = (ny[breaks][0], 4003.0, 4009.0, 4002.0, 4008.0)
ny[again] = (ny[again][0], 4008.0, 4011.0, 4007.0, 4010.0)
orb = gb.Rules(setup="orb", trend_filter=False, session=(0, 24))
worked = gb.indicators(ny)
check(gb.opening_range(range_second, ny) is None and gb.opening_range(inside, ny)[:2] == (4005.0, 3995.0),
      "the opening range is 9:30 to 10:00 New York, high 4005 and low 3995, known only once it has closed")
check(gb.signal(inside, ny, *worked[:3], orb) is None and gb.signal(breaks, ny, *worked[:3], orb) == "buy",
      "a close inside the range is nothing; the first green close above it is a buy")
check(gb.signal(again, ny, *worked[:3], orb) is None, "and only the first: the next close above it is not another")
red = list(ny)
red[breaks] = (red[breaks][0], 3996.0, 3997.0, 3990.0, 3991.0)
check(gb.signal(breaks, red, *gb.indicators(red)[:3], orb) == "sell", "a red close below the range is a sell")
check(gb.signal(breaks, ny, *worked[:3], gb.Rules(setup="orb", trend_filter=True, session=(0, 24)),
                [4100.0] * len(ny), worked[5]) is None,
      "with the trend filter, no buy below the 200-candle average")
check(gb.forming(inside, ny, worked[0], worked[2], orb) is None, "a breakout is one candle, so it gives no warning")
winter = day_at(utc(2026, 12, 7, 0, 0), [4000.0] * 96)     # New York is UTC-5 in December
check(gb.opening_range(next(i for i, c in enumerate(winter) if c[0] == utc(2026, 12, 7, 15, 0)), winter) is not None
      and gb.opening_range(next(i for i, c in enumerate(winter) if c[0] == utc(2026, 12, 7, 14, 15)), winter) is None,
      "in winter the range is an hour later in UTC: still 9:30 to 10:00 New York")

# ---- floors, ceilings and retests -----------------------------------------------------------------

def quarter_hours(start, prices):
    """Fifteen-minute candles from (open, high, low, close) tuples."""
    return [(start + timedelta(minutes=15 * step), *price) for step, price in enumerate(prices)]


# Two four-hour candles (32 fifteen-minute ones) of a range, 4000 to 4060; then a candle dipping to the floor
# and closing green -- a bounce -- one reaching the ceiling and closing red, and one doing neither.
flat = [(4030.0, 4031.0, 4029.0, 4030.0)] * 32
flat[3] = (4030.0, 4060.0, 4029.0, 4031.0)
flat[20] = (4030.0, 4031.0, 4000.0, 4029.0)
after = [(4010.0, 4012.0, 4001.0, 4011.0), (4050.0, 4059.0, 4045.0, 4048.0), (4030.0, 4031.0, 4029.0, 4030.5)]
ranged = quarter_hours(utc(2026, 10, 6, 0, 0), flat + after)
found = gb.levels(ranged, 2)
check(found["box"][32] == (4000.0, 4060.0) and found["box"][31] is None,
      "the box is the floor and ceiling of the last two finished four-hour candles, known only once they have closed")
check(found["range"][32:35] == ["buy", "sell", None],
      "near the floor, closing green above it: a buy; near the ceiling, closing red below it: a sell; elsewhere nothing")
narrow = quarter_hours(utc(2026, 10, 6, 0, 0), [(4030.0, 4031.0, 4029.0, 4030.0)] * 32 + after)
check(gb.levels(narrow, 2)["range"][32] is None, "a range too narrow for a target to fit gives no bounce")

# A breakout: the third four-hour candle closes at 4080, above the 4060 ceiling; the retest comes later.
breaking = flat + [(4030.0, 4045.0, 4029.0, 4040.0)] * 15 + [(4040.0, 4081.0, 4039.0, 4080.0)]
back = [(4080.0, 4082.0, 4070.0, 4075.0), (4061.0, 4067.0, 4059.0, 4066.0), (4064.0, 4070.0, 4061.0, 4069.0)]
retested = quarter_hours(utc(2026, 10, 6, 0, 0), breaking + back)
signals = gb.levels(retested, 2)["retest"]
check(signals[47] is None and signals[48] is None and signals[49] == "buy" and signals[50] is None,
      "after a four-hour close above the ceiling: no trade on the breakout itself; the first return to the line,"
      " closing green above it, buys; once only")
failed = quarter_hours(utc(2026, 10, 6, 0, 0), breaking + [(4080.0, 4081.0, 4040.0, 4045.0),
                                                           (4045.0, 4066.0, 4044.0, 4062.0)])
check(gb.levels(failed, 2)["retest"][49] is None, "a close a stop's width back inside the range calls the breakout off")
check(gb.signal(49, retested, *gb.indicators(retested)[:3], gb.Rules(setup="retest", box=2, session=(0, 24))) == "buy",
      "and signal() gives the same, for the backtest and the trader alike")
check(gb.history_needed(gb.REGISTRY["range60-1:3"]) > 60 * 16 and gb.history_needed(gb.CHOSEN) == 300,
      "the trader asks OANDA for enough candles for a ten-day box")

# ---- several trades open at once ----------------------------------------------------------------

# Gold flat for a while, then $100 higher, so every buy reaches its target; the setups are placed by hand.
level = series([4000.0] * 60 + [4100.0] * 5)
wanted = {30: "buy", 32: "buy", 33: "sell", 34: "buy", 36: "buy"}
real_signal = gb.signal
gb.signal = lambda index, *rest: wanted.get(index)

try:
    one = gb.backtest(level, gb.Rules(session=(0, 24)))
    three = gb.backtest(level, gb.Rules(session=(0, 24)), most_open=3)
finally:
    gb.signal = real_signal

opened_at = [level.index(next(c for c in level if c[0] + timedelta(minutes=15) == t.opened)) for t in three]
check(len(one) == 1 and one[0].result == 30.0, "one trade at a time: the setups while it is open are missed")
check(sorted(opened_at) == [30, 32, 34] and all(t.side == "buy" and t.result == 30.0 for t in three),
      f"up to three open at once: the next two buys are taken too, the fourth is not ({sorted(opened_at)})")
check(33 not in opened_at, "and a sell is never opened against the open buys, as a demo account without hedging would net them")

check(cli._periods(made, opened, 3) and abs(sum(cli._periods(made, opened, 3)) - sum(t.result for t in opened)) < 1e-9
      and cli.periods_for(3 * 365) == 3 and cli.periods_for(365) == 2 and cli.periods_for(30) == 2,
      "results split into a part a year (at least two), every trade counted once")
check(cli.worth_trading(gb.Summary(trades=90), 1.0, 2.0, 3.0) and not cli.worth_trading(gb.Summary(trades=90), 1.0, -2.0, 3.0),
      "worth trading over three years: money made in every one of them")


# The versions worth trading, run together: those entering on the same candles only differ in how they
# leave, so only the best of each kind of entry is kept (the others would never get a trade).
ranked = [gb.REGISTRY[key] for key in ("bounce-atr-1:3-be", "bounce-1:3-be", "bounce-1:2")]
kept = cli.best_per_entry(ranked)
check([rules.key() for rules in kept] == ["bounce-atr-1:3-be"],
      f"bounce versions differing only in exits: just the best is kept ({[rules.key() for rules in kept]})")
mixed_kinds = [gb.REGISTRY[key] for key in ("bounce-1:3-be", "pullback-atr-1:3-be", "bounce-1:2", "breakout-1:3-be")]
check([rules.key() for rules in cli.best_per_entry(mixed_kinds)] == ["bounce-1:3-be", "pullback-atr-1:3-be", "breakout-1:3-be"],
      "different kinds of entry are each kept, best first")

sys.exit(1 if failures else 0)
