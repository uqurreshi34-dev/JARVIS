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
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import gold_backtest as gb  # noqa: E402


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
bands, rsis, squeezes, atrs, averages = gb.indicators(made)
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

check(len(gb.VARIANTS) == 12 and gb.AS_WRITTEN in gb.VARIANTS, "every combination of the open choices is run")

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
check(gb._halves(made, opened) == (5.0, 7.0), "each result counted in the half of the period it opened in")

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

sys.exit(1 if failures else 0)
