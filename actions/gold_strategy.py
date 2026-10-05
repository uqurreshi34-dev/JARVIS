"""The gold strategy: Bollinger band touch, a confirmation candle, RSI, and the bigger trend.

Shared by the backtest (tools/gold_backtest.py), which runs it over a year
of past prices, and the trader (actions/gold_trader.py), which runs it on
OANDA's live candles -- so what is tested is exactly what trades. Nothing
here calls a model: every decision is arithmetic on the candles.

The rules (a buy; a sell is the mirror image):

1. Candle 1 touches the lower Bollinger band (20 candles, 2 standard
   deviations) and closes green.
2. Candle 2, the confirmation, closes green too.
3. RSI (14 candles): as first written, it was below 30 and is back above
   it by candle 2's close; the version that held up is simply not below 30.
4. Enter as candle 2 closes. Stop $10 below, target $30 above (1:3).
5. Only on candles closing 10:00 to 14:00 UK time, one trade at a time
   (or as many as the trader's max_open_trades allows, all the same way),
   none while the bands are squeezed, and -- with the trend filter -- only
   buys above the 200-candle average and sells below it.

Also here: Dukascopy's free spot gold history for the backtest, UK time,
and the indicators (Bollinger bands, RSI, ATR, the moving average).
"""

import concurrent.futures
import lzma
import math
import os
import struct
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone


SYMBOL = "XAUUSD"
FEED = "https://datafeed.dukascopy.com/datafeed"

# Dukascopy stores gold in thousandths of a dollar.
PRICE_SCALE = 1000.0

# One one-minute candle: seconds into the day, open, close, low, high, volume.
_RECORD = struct.Struct(">5if")

BAND_LENGTH = 20
BAND_DEVIATIONS = 2.0
RSI_LENGTH = 14
RSI_LOW, RSI_HIGH = 30.0, 70.0

# How recently RSI must have been beyond 30 or 70, in candles before the
# confirmation, for "it was extreme and has turned back".
RSI_LOOKBACK = 3

# The bands count as squeezed when their width is in the narrowest fifth
# of the last hundred candles.
SQUEEZE_WINDOW = 100
SQUEEZE_SHARE = 0.20

STOP_DOLLARS = 10.0
TARGET_DOLLARS = 30.0

# With exits from the bands: how far beyond candle 1's wick the stop sits.
WICK_BUFFER = 1.0

# The trailing exit, in multiples of the average true range (ATR, 14
# candles): how far gold normally moves in a candle, so the stop is as wide
# as the market is lively rather than a fixed $10. The first stop sits 1.5
# ATR away; once the trade is 1 R in profit (R being that first distance)
# the stop moves to the entry, so it can no longer lose; after that it
# follows 2 ATR behind the best price reached, and only ever forward. No
# fixed target: the trailing stop takes the profit.
ATR_LENGTH = 14
TRAIL_FIRST_STOP = 1.5
BREAKEVEN_AT_R = 1.0
TRAIL_DISTANCE = 2.0

# The trend filter: buy only above the 200-candle average (about two days of
# 15-minute candles), sell only below it, so a bounce is taken with the
# bigger move rather than against it.
TREND_LENGTH = 200

# The pullback setup's average: in a trend, price dipping back to the
# 20-candle average and turning is the classic place to join it.
FAST_LENGTH = 20

# Exits sized by ATR: the stop this many ATRs from the entry, the target a
# fixed multiple of the stop, so a lively day gets a wider stop and a quiet
# one a tighter, and the reward stays the same multiple of the risk.
ATR_STOP = 1.5
SPREAD = 0.80

# UK hours, inclusive of a candle closing at the start, exclusive at the end.
SESSION = (10, 14)

CANDLE_MINUTES = 15


# ---- the prices ------------------------------------------------------------------------------

def _cache_folder():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    folder = os.path.join(base, "JARVIS", "dukascopy", SYMBOL)
    os.makedirs(folder, exist_ok=True)
    return folder


def _day_url(day):
    # Dukascopy counts months from 0: January is 00.
    return f"{FEED}/{SYMBOL}/{day.year}/{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5"


def decode_day(day, raw):
    """One day's compressed one-minute candles as (utc datetime, open, high, low, close)."""
    if not raw:
        return []

    data = lzma.decompress(raw)
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    candles = []

    for offset in range(0, len(data) - _RECORD.size + 1, _RECORD.size):
        seconds, open_, close, low, high, _volume = _RECORD.unpack_from(data, offset)

        # A minute with no trades repeats the last price with no range.
        candles.append((start + timedelta(seconds=seconds), open_ / PRICE_SCALE, high / PRICE_SCALE,
                        low / PRICE_SCALE, close / PRICE_SCALE))

    return candles


# Dukascopy refuses a client asking too much at once (it answers 503 or
# 429); a few at a time, and a refused day asked again after a growing wait.
WORKERS = 4
ATTEMPTS = 5

# A run goes ahead with a few days missing, which only thins the test, but
# not with more than this share of them.
MOST_MISSING = 0.10


def _fetch_day(day, folder, session):
    """(day, compressed prices or None, why not) -- from the kept copy if there is one."""
    path = os.path.join(folder, f"{day.isoformat()}.bi5")

    if os.path.exists(path):
        with open(path, "rb") as handle:
            return day, handle.read(), ""

    why = "no answer"

    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(2 ** attempt)

        try:
            response = session.get(_day_url(day), timeout=30)
        except Exception as error:
            why = type(error).__name__
            continue

        if response.status_code == 404:
            raw = b""
        elif response.ok:
            raw = response.content
        else:
            why = f"HTTP {response.status_code}"
            continue

        try:
            decode_day(day, raw)
        except Exception as error:
            why = f"unreadable ({type(error).__name__})"
            continue

        # Today's file is still growing; only finished days are kept.
        if day < datetime.now(timezone.utc).date():
            with open(path, "wb") as handle:
                handle.write(raw)

        return day, raw, ""

    return day, None, why


def minute_candles(first, last, progress=print):
    """Every one-minute candle from [first] to [last] (dates, UTC), oldest first.

    Days that could not be fetched are left out and named; too many of them
    stops the run (RuntimeError), as the test would no longer mean much.
    Whatever was fetched is kept, so running again asks only for the rest.
    """
    import requests

    folder = _cache_folder()
    days = [first + timedelta(days=step) for step in range((last - first).days + 1)]
    days = [day for day in days if day.weekday() != 5]   # gold never trades on a Saturday
    found, missing = {}, {}
    wanted = sum(1 for day in days if not os.path.exists(os.path.join(folder, f"{day.isoformat()}.bi5")))
    began = time.monotonic()

    if wanted:
        progress(f"  {len(days) - wanted} days already here, {wanted} to download. The first time takes a few"
                 " minutes; Ctrl+C is safe, as every day downloaded is kept.")

    with requests.Session() as session, concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        session.headers["User-Agent"] = "JARVIS gold backtest"
        jobs = [pool.submit(_fetch_day, day, folder, session) for day in days]

        for done, job in enumerate(concurrent.futures.as_completed(jobs), 1):
            day, raw, why = job.result()

            if raw is None:
                missing[day] = why
            else:
                found[day] = decode_day(day, raw)

            if wanted and (done % 10 == 0 or done == len(days)):
                progress(f"  prices: {done} of {len(days)} days ({time.monotonic() - began:.0f} s)")

    if missing:
        named = ", ".join(f"{day:%d %b} ({why})" for day, why in sorted(missing.items())[:5])
        more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        progress(f"  {len(missing)} of {len(days)} days could not be fetched: {named}{more}")

        if len(missing) > MOST_MISSING * len(days):
            raise RuntimeError("Too many days are missing to test fairly. What did arrive is kept: "
                               "run it again in a few minutes and it asks only for the rest.")

        progress("  Testing without them; run it again later to fill them in.")

    return [candle for day in sorted(found) for candle in found[day]]


def fifteen_minute(candles, minutes=CANDLE_MINUTES):
    """One-minute candles built into [minutes]-minute ones on the clock: (start, open, high, low, close)."""
    built = []

    for at, open_, high, low, close in candles:
        start = at.replace(minute=at.minute - at.minute % minutes, second=0, microsecond=0)

        if built and built[-1][0] == start:
            _, first_open, top, bottom, _ = built[-1]
            built[-1] = (start, first_open, max(top, high), min(bottom, low), close)
        else:
            built.append((start, open_, high, low, close))

    return built


# ---- UK time ---------------------------------------------------------------------------------

def _last_sunday(year, month):
    day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def uk_time(utc):
    """[utc] as UK clock time: British Summer Time from 01:00 UTC on the last Sunday of March to the last of October."""
    starts = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    ends = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return utc + timedelta(hours=1 if starts <= utc < ends else 0)


def _nth_sunday(year, month, nth):
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def new_york_time(utc):
    """[utc] as New York clock time: daylight time (UTC-4) from 2:00 local on the second Sunday of March to
    2:00 local on the first Sunday of November, otherwise UTC-5. Worked out, not looked up, as Windows
    Python has no time zone database without an extra package."""
    starts = datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    ends = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return utc - timedelta(hours=4 if starts <= utc < ends else 5)


# The clocks the trading hours can be set in: the UK's, or New York's, so a window tied to the US session
# follows it through the weeks each spring and autumn when one country has changed its clocks and the other
# has not.
ZONES = {"uk": uk_time, "new_york": new_york_time}
ZONE_NAMES = {"uk": "UK", "new_york": "New York"}


def in_session(close_utc, session=SESSION, zone="uk"):
    """Whether a candle closing at [close_utc] closes within the trading hours, on [zone]'s clock."""
    local = ZONES[zone](close_utc)
    minutes = local.hour * 60 + local.minute
    return session[0] * 60 <= minutes <= session[1] * 60


# ---- the indicators --------------------------------------------------------------------------

def bollinger(closes, length=BAND_LENGTH, deviations=BAND_DEVIATIONS):
    """(lower, middle, upper) for each close, None until there are [length] of them.

    The standard deviation of the closes themselves (not of a sample), as
    TradingView and MetaTrader draw the bands.
    """
    bands = []

    for index in range(len(closes)):
        if index + 1 < length:
            bands.append(None)
            continue

        window = closes[index + 1 - length:index + 1]
        middle = sum(window) / length
        spread = math.sqrt(sum((value - middle) ** 2 for value in window) / length)
        bands.append((middle - deviations * spread, middle, middle + deviations * spread))

    return bands


def average_true_range(candles, length=ATR_LENGTH):
    """Wilder's average true range for each candle, None until there are [length] ranges."""
    values = [None] * len(candles)
    ranges = []

    for index, (_start, _open, high, low, _close) in enumerate(candles):
        previous = candles[index - 1][4] if index else None
        ranges.append(high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous)))

        if index + 1 == length:
            values[index] = sum(ranges) / length
        elif index + 1 > length:
            values[index] = (values[index - 1] * (length - 1) + ranges[-1]) / length

    return values


def moving_average(closes, length=TREND_LENGTH):
    """The exponential moving average of [closes], None until there are [length] of them."""
    values = [None] * len(closes)

    if len(closes) < length:
        return values

    values[length - 1] = sum(closes[:length]) / length
    weight = 2.0 / (length + 1)

    for index in range(length, len(closes)):
        values[index] = values[index - 1] + weight * (closes[index] - values[index - 1])

    return values


def rsi(closes, length=RSI_LENGTH):
    """Wilder's RSI for each close, None until it has [length] changes behind it."""
    values = [None] * len(closes)

    if len(closes) <= length:
        return values

    gains = losses = 0.0

    for index in range(1, length + 1):
        change = closes[index] - closes[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)

    gains, losses = gains / length, losses / length

    for index in range(length, len(closes)):
        if index > length:
            change = closes[index] - closes[index - 1]
            gains = (gains * (length - 1) + max(change, 0.0)) / length
            losses = (losses * (length - 1) + max(-change, 0.0)) / length

        values[index] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)

    return values


def squeezed(bands, window=SQUEEZE_WINDOW, share=SQUEEZE_SHARE):
    """For each candle, whether the bands are in the narrowest [share] of the last [window] candles."""
    widths = [None if band is None else (band[2] - band[0]) / band[1] for band in bands]
    flags = []

    for index, width in enumerate(widths):
        past = [value for value in widths[max(0, index + 1 - window):index + 1] if value is not None]

        if width is None or len(past) < window // 2:
            flags.append(False)
            continue

        ordered = sorted(past)
        flags.append(width <= ordered[max(0, int(len(ordered) * share) - 1)])

    return flags


# ---- the strategy ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    setup: str = "bounce"        # "bounce": off a band, against the move; "pullback": back to the 20-candle
                                 # average, with the trend; "breakout": out of a squeeze, with the trend;
                                 # "orb": the first close beyond the New York opening range;
                                 # "range": a bounce off the floor or ceiling of the last [box] four-hour
                                 # candles; "retest": after a four-hour close beyond one, the first return to it
    touch: str = "wick"          # "wick": the candle reaches the band; "close": it closes beyond it
    rsi: str = "turned"          # "turned": was beyond 30/70 and is back; "not_extreme": simply not beyond now
    squeeze_filter: bool = True
    exits: str = "fixed"         # "fixed": $10 stop, $30 target; "atr": a stop of 1.5 ATR, the target [ratio] times
                                 # it; "bands": stop past candle 1's wick, target the middle band; "trail": an
                                 # ATR stop, breakeven at 1 R, then trailing
    trend_filter: bool = False   # only buy above the 200-candle average, only sell below it
    breakeven: bool = False      # fixed exits: the stop moves to the entry once the trade is 1 R up
    stop: float = STOP_DOLLARS
    target: float = TARGET_DOLLARS
    ratio: float = 3.0           # "atr" exits: the target as a multiple of the stop
    spread: float = SPREAD
    session: tuple = SESSION
    zone: str = "uk"             # whose clock [session] is in (ZONES)
    box: int = 0                 # "range" and "retest": how many four-hour candles the floor and ceiling span

    def _exits(self):
        if self.exits == "fixed":
            return f"1:{self.target / self.stop:g}" + ("+BE" if self.breakeven else "")

        if self.exits == "atr":
            return f"ATR 1:{self.ratio:g}" + ("+BE" if self.breakeven else "")

        return self.exits

    def name(self):
        if self.setup == "bounce":
            entry = f"rsi={self.rsi:11}"
        elif self.box:
            entry = f"{self.setup + ' ' + str(self.box) + 'x4h':15}"
        else:
            entry = f"{self.setup:15}"

        return f"{entry} exits={self._exits():10} trend filter={'on ' if self.trend_filter else 'off'}"

    def atr_stop_distance(self, atr):
        return ATR_STOP * atr

    def key(self):
        """A short, stable name for these rules, as gold-trader.json lists the setups to trade."""
        exits = self._exits().lower().replace(" ", "-").replace("+be", "-be")
        if self.box:
            return f"{self.setup}{self.box}-{exits}"

        trend = "" if self.trend_filter else "-no-trend"
        rsi = "" if self.setup != "bounce" or self.rsi == "not_extreme" else "-rsi-turned"
        return f"{self.setup}-{exits}{trend}{rsi}"


@dataclass
class Trade:
    side: str
    opened: datetime
    entry: float
    stop: float
    target: float                # None: the middle band, or no target at all when trailing
    closed: datetime = None
    exit: float = None
    result: float = None
    reason: str = ""
    trailing: bool = False
    breakeven: bool = False      # the stop goes to the entry at 1 R, and stays
    risk: float = 0.0            # the first stop's distance, R
    best: float = None           # the best price reached since the entry
    rules: object = None         # the rules that opened it, when several are run together


def signal(index, candles, bands, rsis, squeezes, rules, averages=None, fast=None):
    """"buy", "sell" or None for the confirmation candle at [index] (candle 1 is index - 1).

    [averages], the 200-candle average, is needed only with the trend
    filter; [fast], the 20-candle average, only for the pullback setup.
    """
    if index < 1 or bands[index - 1] is None or rsis[index] is None:
        return None

    trend = averages[index] if averages else None

    if rules.trend_filter and trend is None:
        return None

    if rules.setup == "pullback":
        return _pullback(index, candles, rsis, trend, fast)

    if rules.setup == "breakout":
        return _breakout(index, candles, bands, rsis, squeezes, rules, trend)

    if rules.setup == "orb":
        return _opening_range_break(index, candles, rules, trend)

    if rules.setup in ("range", "retest"):
        return levels(candles, rules.box)[rules.setup][index]

    _start, open2, _high2, _low2, close2 = candles[index]

    if rules.squeeze_filter and squeezes[index - 1]:
        return None

    recent = [value for value in rsis[max(0, index - RSI_LOOKBACK):index] if value is not None]
    touched = _band_touch(candles[index - 1], bands[index - 1], rules)

    if touched == "buy" and close2 > open2:
        if rules.rsi == "turned":
            turned = any(value < RSI_LOW for value in recent) and rsis[index] >= RSI_LOW
        else:
            turned = rsis[index] >= RSI_LOW

        if turned and not (rules.trend_filter and close2 <= trend):
            return "buy"

    if touched == "sell" and close2 < open2:
        if rules.rsi == "turned":
            turned = any(value > RSI_HIGH for value in recent) and rsis[index] <= RSI_HIGH
        else:
            turned = rsis[index] <= RSI_HIGH

        if turned and not (rules.trend_filter and close2 >= trend):
            return "sell"

    return None


def _band_touch(candle, band, rules):
    """"buy" when [candle] reaches the lower band and closes green, "sell" when it reaches the upper and closes
    red: a bounce's candle 1. None otherwise."""
    _start, open_, high, low, close = candle
    lower, _middle, upper = band

    if ((low <= lower) if rules.touch == "wick" else (close <= lower)) and close > open_:
        return "buy"

    if ((high >= upper) if rules.touch == "wick" else (close >= upper)) and close < open_:
        return "sell"

    return None


def forming(index, candles, bands, squeezes, rules, averages=None, fast=None):
    """"buy", "sell" or None: whether the candle at [index] could be candle 1 of [rules]' setup.

    A warning, a candle early: the next candle's close decides whether
    signal() sees the setup. The bounce: a band touched and the candle
    closed back the right way, the bands not squeezed, and on the trend's
    side. The pullback: a dip to the 20-candle average with the trend. A
    breakout is a single candle, so it gives no warning.
    """
    if index < 0 or bands[index] is None:
        return None

    trend = averages[index] if averages else None
    close = candles[index][4]

    if rules.setup == "bounce":
        if rules.squeeze_filter and squeezes[index]:
            return None

        side = _band_touch(candles[index], bands[index], rules)

        if side and rules.trend_filter and (trend is None or (close <= trend if side == "buy" else close >= trend)):
            return None

        return side

    if rules.setup == "pullback" and fast and trend is not None and fast[index] is not None:
        _start, _open, high, low, _close = candles[index]

        if close > trend and fast[index] > trend and low <= fast[index]:
            return "buy"

        if close < trend and fast[index] < trend and high >= fast[index]:
            return "sell"

    return None


def _pullback(index, candles, rsis, trend, fast):
    """Joining a trend after a dip: the trend up (price and the 20-candle average both above the 200),
    candle 1 dips to the 20-candle average, candle 2 closes green above it and above candle 1's high,
    RSI between 40 and 70 -- momentum with the trend, not yet stretched. A sell is the mirror image."""
    if not fast or trend is None or fast[index - 1] is None:
        return None

    _start, _open1, high1, low1, _close1 = candles[index - 1]
    _start, open2, _high2, _low2, close2 = candles[index]
    average = fast[index - 1]

    if close2 > trend and average > trend and low1 <= average and close2 > open2 and close2 > max(average, high1) \
            and 40.0 <= rsis[index] <= 70.0:
        return "buy"

    if close2 < trend and average < trend and high1 >= average and close2 < open2 and close2 < min(average, low1) \
            and 30.0 <= rsis[index] <= 60.0:
        return "sell"

    return None


# The opening range: the first half hour of the New York stock market, 9:30 to 10:00 New York time, when
# gold's busiest stretch of the day begins. Its high and low are where the day's first real move breaks out.
ORB_OPEN = (9, 30)
ORB_MINUTES = 30

# How far back to look for the day's range: a whole day of fifteen-minute candles.
ORB_LOOKBACK = 24 * 60 // CANDLE_MINUTES


def opening_range(index, candles):
    """(high, low, broken) of the New York opening range on the day of the candle at [index], from the
    candles before it; None if the range is not yet complete (the candle is inside it, or one is missing).

    [broken]: whether a candle after the range, and before [index], already closed beyond it -- only the
    first close beyond the range is a breakout.
    """
    local = new_york_time(candles[index][0])
    opens = local.replace(hour=ORB_OPEN[0], minute=ORB_OPEN[1], second=0, microsecond=0)
    formed = opens + timedelta(minutes=ORB_MINUTES)

    if local < formed:
        return None

    inside, after = [], []

    for earlier in range(index - 1, max(-1, index - 1 - ORB_LOOKBACK), -1):
        start = new_york_time(candles[earlier][0])

        if start.date() != local.date() or start < opens:
            break

        (inside if start < formed else after).append(candles[earlier])

    if len(inside) != ORB_MINUTES // CANDLE_MINUTES:
        return None

    high = max(candle[2] for candle in inside)
    low = min(candle[3] for candle in inside)
    broken = any(candle[4] > high or candle[4] < low for candle in after)
    return high, low, broken


def _opening_range_break(index, candles, rules, trend):
    """The first candle of the day to close beyond the New York opening range, in its own colour -- above
    the range and green, a buy; below it and red, a sell -- and with the trend filter, only the trend's way."""
    found = opening_range(index, candles)

    if found is None or found[2]:
        return None

    high, low, _broken = found
    _start, open_, _high, _low, close = candles[index]

    if close > high and close > open_ and not (rules.trend_filter and close <= trend):
        return "buy"

    if close < low and close < open_ and not (rules.trend_filter and close >= trend):
        return "sell"

    return None


# Floors and ceilings: the lowest low and highest high of the last [box] four-hour candles, the lines a
# trader draws on the four-hour chart, made a rule so they can be tested. Four-hour candles are built from
# the fifteen-minute ones on the UTC clock (00:00, 04:00, ...).
BLOCK_HOURS = 4

# How near the line a fifteen-minute candle must reach to count as touching it.
TOUCH_DOLLARS = 2.0

# A bounce needs room inside the range for its target: the range at least this many stops wide.
RANGE_ROOM = 4.0

# How long after a four-hour close beyond a line its retest is still waited for.
RETEST_HOURS = 24

_LEVELS = {}


def _blocks(candles):
    """The four-hour candles: (first index, last index, high, low, close) of each, in order."""
    blocks = []

    for index, (start, _open, high, low, close) in enumerate(candles):
        key = (start.date(), start.hour // BLOCK_HOURS)

        if blocks and blocks[-1][0] == key:
            _key, first, _last, top, bottom, _close = blocks[-1]
            blocks[-1] = (key, first, index, max(top, high), min(bottom, low), close)
        else:
            blocks.append((key, index, index, high, low, close))

    return [block[1:] for block in blocks]


def levels(candles, box, stop=STOP_DOLLARS):
    """For each fifteen-minute candle, the side a "range" bounce and a "retest" would take on its close, or None.

    {"range": [...], "retest": [...], "box": [(floor, ceiling) or None, ...]}, worked out once for each
    candles list and [box]. Only four-hour candles already finished count, so nothing is known before it
    could be.

    Range: a candle reaching the floor (within TOUCH_DOLLARS) and closing green above it is a buy; one
    reaching the ceiling and closing red below it a sell -- when the range is wide enough for a target.

    Retest: a four-hour close above the ceiling (or below the floor) of the [box] before it arms that line
    for RETEST_HOURS. The first fifteen-minute candle to come back to it and close the breakout's way --
    green above it after a break up, red below it after a break down -- is the trade; one closing a stop's
    width back through it, or a four-hour close back inside, disarms it.
    """
    signature = (id(candles), len(candles), candles[0][0] if candles else None, box, stop)

    if signature in _LEVELS:
        return _LEVELS[signature]

    count = len(candles)
    ranges, retests, boxes = [None] * count, [None] * count, [None] * count
    blocks = _blocks(candles)
    armed = None   # (side, line, index from which it may trade, index after which it expires)

    for number, (first, last, _high, _low, close) in enumerate(blocks):
        if number < box:
            continue

        window = blocks[number - box:number]
        ceiling = max(block[2] for block in window)
        floor = min(block[3] for block in window)

        for index in range(first, last + 1):
            _start, open_, high, low, shut = candles[index]
            boxes[index] = (floor, ceiling)

            if ceiling - floor >= RANGE_ROOM * stop:
                if low <= floor + TOUCH_DOLLARS and shut > floor and shut > open_:
                    ranges[index] = "buy"
                elif high >= ceiling - TOUCH_DOLLARS and shut < ceiling and shut < open_:
                    ranges[index] = "sell"

            if armed and armed[2] <= index <= armed[3]:
                side, line = armed[0], armed[1]

                if side == "buy" and low <= line + TOUCH_DOLLARS and shut > line and shut > open_:
                    retests[index], armed = "buy", None
                elif side == "sell" and high >= line - TOUCH_DOLLARS and shut < line and shut < open_:
                    retests[index], armed = "sell", None
                elif (side == "buy" and shut < line - stop) or (side == "sell" and shut > line + stop):
                    armed = None

        # This four-hour candle has closed: a close beyond its range arms the retest; back inside disarms it.
        expires = last + RETEST_HOURS * 60 // CANDLE_MINUTES

        if close > ceiling:
            armed = ("buy", ceiling, last + 1, expires)
        elif close < floor:
            armed = ("sell", floor, last + 1, expires)
        elif armed and ((armed[0] == "buy" and close < armed[1]) or (armed[0] == "sell" and close > armed[1])):
            armed = None

    found = {"range": ranges, "retest": retests, "box": boxes}

    if len(_LEVELS) > 64:
        _LEVELS.clear()

    _LEVELS[signature] = found
    return found


def history_needed(rules):
    """How many fifteen-minute candles [rules] need behind the latest to decide: enough for every indicator,
    and for "range" and "retest" a whole [box] of four-hour candles more."""
    return max(300, (rules.box + 2) * BLOCK_HOURS * 60 // CANDLE_MINUTES + 50)


def _breakout(index, candles, bands, rsis, squeezes, rules, trend):
    """A squeeze ending: the bands were in their narrowest fifth on candle 1, and candle 2 closes beyond
    one of them, in the trend's direction, without RSI already stretched past 80 or 20."""
    if not squeezes[index - 1] or bands[index] is None:
        return None

    _start, open2, _high2, _low2, close2 = candles[index]
    lower, _middle, upper = bands[index]
    with_trend_up = not rules.trend_filter or close2 > trend
    with_trend_down = not rules.trend_filter or close2 < trend

    if close2 > upper and close2 > open2 and with_trend_up and rsis[index] < 80.0:
        return "buy"

    if close2 < lower and close2 < open2 and with_trend_down and rsis[index] > 20.0:
        return "sell"

    return None


def _trail(trade, candle, rules, atr):
    """Move a trailing trade's stop after [candle]: to the entry at 1 R, then 2 ATR behind its best price.

    Only ever towards profit. Worked out from the candle just finished, so
    the new stop applies from the next one.
    """
    _start, _open, high, low, _close = candle

    if trade.side == "buy":
        trade.best = max(trade.best, high)
        gained = trade.best - trade.entry
        moved = [trade.stop]

        if gained >= BREAKEVEN_AT_R * trade.risk:
            moved.append(trade.entry)
            if atr:
                moved.append(trade.best - TRAIL_DISTANCE * atr)

        trade.stop = max(moved)
    else:
        trade.best = min(trade.best, low + rules.spread)
        gained = trade.entry - trade.best
        moved = [trade.stop]

        if gained >= BREAKEVEN_AT_R * trade.risk:
            moved.append(trade.entry)
            if atr:
                moved.append(trade.best + TRAIL_DISTANCE * atr)

        trade.stop = min(moved)


def _settle(trade, candle, rules, middle=None, atr=None):
    """Close [trade] if [candle] (bid prices) reaches its stop or target; the stop first if both.

    A trade aiming for the middle band aims at [middle], the band as it stood
    when the candle began, so nothing is known before it could be. A
    trailing trade still open after the candle has its stop moved (_trail).
    """
    start, _open, high, low, _close = candle
    closes_at = start + timedelta(minutes=CANDLE_MINUTES)
    target = trade.target if trade.target is not None or trade.trailing else middle

    if trade.side == "buy":
        # A buy is closed by selling, at the bid.
        hit_stop = low <= trade.stop
        hit_target = target is not None and high >= target
    else:
        # A sell is closed by buying, at the ask: the bid plus the spread.
        hit_stop = high + rules.spread >= trade.stop
        hit_target = target is not None and low + rules.spread <= target

    if hit_stop or hit_target:
        trade.closed = closes_at
        trade.exit = trade.stop if hit_stop else target
        trade.reason = "stop" if hit_stop else "target"
        trade.result = (trade.exit - trade.entry) if trade.side == "buy" else (trade.entry - trade.exit)
        return True

    if trade.trailing:
        _trail(trade, candle, rules, atr)
    elif trade.breakeven:
        _to_breakeven(trade, candle, rules)

    return False


def _to_breakeven(trade, candle, rules):
    """Once the trade has been 1 R in profit, its stop is the entry: it can no longer lose."""
    _start, _open, high, low, _close = candle

    if trade.side == "buy" and high - trade.entry >= trade.risk:
        trade.stop = max(trade.stop, trade.entry)
    elif trade.side == "sell" and trade.entry - (low + rules.spread) >= trade.risk:
        trade.stop = min(trade.stop, trade.entry)


def indicators(candles):
    """(bands, rsi, squeezed, ATR, 200- and 20-candle averages) for [candles], worked out once for every set of rules."""
    closes = [candle[4] for candle in candles]
    bands = bollinger(closes)
    return (bands, rsi(closes), squeezed(bands), average_true_range(candles), moving_average(closes),
            moving_average(closes, FAST_LENGTH))


def exits_for(rules, entry, side, atr):
    """(stop, target, risk) for a trade entered at [entry]: the stop and target prices, and the stop's distance.

    None when the exits need an ATR that is not there yet. Used alike by the
    backtest and the trader, so both place exactly the same levels.
    """
    if rules.exits == "atr":
        if not atr:
            return None
        distance = rules.atr_stop_distance(atr)
        reward = distance * rules.ratio
    else:
        distance, reward = rules.stop, rules.target

    if side == "buy":
        return entry - distance, entry + reward, distance

    return entry + distance, entry - reward, distance


def backtest(candles, rules=Rules(), worked_out=None, most_open=1):
    """Every trade [rules] would have taken over [candles] (fifteen-minute, bid), in the order they closed.

    [rules] may be several sets of rules, run together as the trader runs
    them: the first set to see a setup on a candle taking it. At most
    [most_open] trades are open at once, all the same way: a demo account
    without hedging nets a sell against an open buy, so the trader never
    opens one against the other. A candle that begins with every place
    taken opens nothing, even if a trade closes during it.
    """
    together = list(rules) if isinstance(rules, (list, tuple)) else [rules]
    bands, rsis, squeezes, atrs, averages, fast = worked_out or indicators(candles)
    trades = []
    open_trades = []

    for index, candle in enumerate(candles):
        full = len(open_trades) >= most_open

        if open_trades:
            middle = bands[index - 1][1] if bands[index - 1] else None
            still = []

            for trade in open_trades:
                if _settle(trade, candle, trade.rules, middle, atrs[index - 1]):
                    trades.append(trade)
                else:
                    still.append(trade)

            open_trades = still

        if full:
            continue

        closes_at = candle[0] + timedelta(minutes=CANDLE_MINUTES)

        for chosen in together:
            if not in_session(closes_at, chosen.session, chosen.zone):
                continue

            side = signal(index, candles, bands, rsis, squeezes, chosen, averages, fast)

            if side and any(trade.side != side for trade in open_trades):
                continue

            if side:
                opened = _open(side, index, candles, chosen, atrs, closes_at)

                if opened is not None:
                    open_trades.append(opened)
                    break

    return trades


def _open(side, index, candles, rules, atrs, closes_at):
    """The trade [rules] open on [side] at the close of candle [index], or None if it cannot be sized yet."""
    candle, touch = candles[index], candles[index - 1]
    entry = candle[4] + rules.spread if side == "buy" else candle[4]   # bought at the ask, sold at the bid

    if rules.exits == "trail":
        if not atrs[index]:
            return None

        distance = TRAIL_FIRST_STOP * atrs[index]
        stop = entry - distance if side == "buy" else entry + distance
        return Trade(side, closes_at, entry, stop, None, trailing=True, risk=distance, best=entry, rules=rules)

    if rules.exits == "bands":
        stop = touch[3] - WICK_BUFFER if side == "buy" else touch[2] + rules.spread + WICK_BUFFER
        return Trade(side, closes_at, entry, stop, None, rules=rules)

    levels = exits_for(rules, entry, side, atrs[index])

    if levels is None:
        return None

    stop, target, distance = levels
    return Trade(side, closes_at, entry, stop, target, breakeven=rules.breakeven, risk=distance, rules=rules)


@dataclass
class Summary:
    trades: int = 0
    wins: int = 0
    net: float = 0.0
    worst_run: int = 0
    deepest: float = 0.0
    risked: float = 0.0
    results: list = field(default_factory=list)

    @property
    def average_risk(self):
        return self.risked / self.trades if self.trades else 0.0

    @property
    def win_rate(self):
        return self.wins / self.trades if self.trades else 0.0


def summarise(trades):
    summary = Summary()
    run = 0
    peak = balance = 0.0

    for trade in trades:
        summary.trades += 1
        summary.net += trade.result
        summary.risked += trade.risk or abs(trade.entry - trade.stop)   # the first stop, before any move
        summary.results.append(trade.result)

        if trade.result > 0:
            summary.wins += 1
            run = 0
        else:
            run += 1
            summary.worst_run = max(summary.worst_run, run)

        balance += trade.result
        peak = max(peak, balance)
        summary.deepest = max(summary.deepest, peak - balance)

    return summary


# ---- the versions tested ------------------------------------------------------------------

# A close beyond the band barely ever happened (one to four trades in four
# months), so only a wick reaching it is tried.
# The squeeze filter is always on: off, every version did worse.
VARIANTS = [Rules(rsi=rule, exits=exits, trend_filter=trend)
            for rule in ("turned", "not_extreme") for exits in ("fixed", "bands", "trail") for trend in (False, True)]

# For the entry that held up (RSI not extreme, trend filter on): other
# targets for the same $10 stop -- 1:2 and 1:4 beside 1:3 -- and each with
# the stop moved to breakeven at 1 R, so a trade that has gone well cannot
# then lose.
VARIANTS += [Rules(rsi="not_extreme", trend_filter=True, target=target, breakeven=breakeven)
             for target in (20.0, 30.0, 40.0) for breakeven in (False, True) if (target, breakeven) != (30.0, False)]

# Stops sized by ATR, the target 2 or 3 times the stop, breakeven at 1 R --
# for the bounce, and for the two other kinds of setup, which find their
# chances where the bounce does not: joining a trend on a pullback, and a
# squeeze breaking out. Each is tested on its own, and kept only on its own
# merits.
VARIANTS += [Rules(rsi="not_extreme", trend_filter=True, exits="atr", ratio=ratio, breakeven=True)
             for ratio in (2.0, 3.0)]
VARIANTS += [Rules(setup=setup, trend_filter=True, exits=exits, ratio=ratio, breakeven=True)
             for setup in ("pullback", "breakout")
             for exits, ratio in (("fixed", 3.0), ("atr", 2.0), ("atr", 3.0))]

# The pullback with fixed exits, with and without breakeven, at 1:3 and 1:4:
# breakeven saves the trades that go 1 R up and then all the way to the
# stop, but costs those that come back to the entry before going on to the
# target -- which wins depends on the setup (for the bounce it lost), so it
# is measured, not assumed.
VARIANTS += [Rules(setup="pullback", trend_filter=True, target=target, breakeven=breakeven)
             for target in (30.0, 40.0) for breakeven in (False, True) if (target, breakeven) != (30.0, True)]

# And nearer targets for the same $10 stop, 1:2 and 1:1.5, without breakeven: hit more often, each win
# paying less -- whether that makes more, after the spread, is measured.
VARIANTS += [Rules(setup="pullback", trend_filter=True, target=target) for target in (20.0, 15.0)]

# The pullback with stops sized by ATR and no breakeven: the form that means the same on any instrument
# (silver among them), where gold's $10 does not.
VARIANTS += [Rules(setup="pullback", trend_filter=True, exits="atr", ratio=ratio) for ratio in (2.0, 3.0)]

# The New York opening-range breakout: a different moment from the pullback, so trades the pullback does not
# take. With and without the trend filter, fixed $10 stops at 1:2 and 1:3 and stops sized by ATR, and no
# breakeven, which cost the other setups here.
VARIANTS += [Rules(setup="orb", trend_filter=trend, target=target) for trend in (True, False)
             for target in (20.0, 30.0)]
VARIANTS += [Rules(setup="orb", trend_filter=trend, exits="atr", ratio=ratio) for trend in (True, False)
             for ratio in (2.0, 3.0)]

# Horizontal lines on the four-hour chart: the floor and ceiling of the last 30 four-hour candles (five
# days) or 60 (ten), traded as a bounce inside the range or as the retest after a break out of it; fixed
# $10 stops at 1:2 and 1:3, and stops sized by ATR at 1:3. No trend filter: the lines are the setup.
VARIANTS += [Rules(setup=setup, box=box, target=target) for setup in ("range", "retest") for box in (30, 60)
             for target in (20.0, 30.0)]
VARIANTS += [Rules(setup=setup, box=box, exits="atr", ratio=3.0) for setup in ("range", "retest") for box in (30, 60)]

# The exits the trader can place at OANDA; the others are tested only.
TRADEABLE_EXITS = ("fixed", "atr")

# Every version by its key, as gold-trader.json names the ones to trade.
REGISTRY = {rules.key(): rules for rules in VARIANTS}

AS_WRITTEN = Rules()

# The version the trader uses: RSI simply not beyond 30/70, the trend and
# squeeze filters on, a $10 stop and $30 target, with the stop moved to the
# entry once the trade is $10 up. Over the year to October 2026: 59 trades,
# +$200, and positive in both halves (+$170, +$30) -- the best of the
# versions that were. Without breakeven: +$160 and +$10. 1:2 and 1:4 lost
# in the second half.
CHOSEN = Rules(rsi="not_extreme", exits="fixed", trend_filter=True, breakeven=True)
