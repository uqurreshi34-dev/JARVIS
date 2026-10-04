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
5. Only on candles closing 10:00 to 14:00 UK time, one trade at a time,
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


def in_session(close_utc, session=SESSION):
    """Whether a candle closing at [close_utc] closes within the UK trading hours."""
    local = uk_time(close_utc)
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
    touch: str = "wick"          # "wick": the candle reaches the band; "close": it closes beyond it
    rsi: str = "turned"          # "turned": was beyond 30/70 and is back; "not_extreme": simply not beyond now
    squeeze_filter: bool = True
    exits: str = "fixed"         # "fixed": $10 stop, $30 target; "bands": stop past candle 1's wick, target the
                                 # middle band; "trail": an ATR stop, breakeven at 1 R, then trailing
    trend_filter: bool = False   # only buy above the 200-candle average, only sell below it
    breakeven: bool = False      # fixed exits: the stop moves to the entry once the trade is 1 R up
    stop: float = STOP_DOLLARS
    target: float = TARGET_DOLLARS
    spread: float = SPREAD
    session: tuple = SESSION

    def name(self):
        exits = self.exits

        if exits == "fixed":
            exits = f"1:{self.target / self.stop:g}" + ("+BE" if self.breakeven else "")

        return f"rsi={self.rsi:11} exits={exits:7} trend filter={'on ' if self.trend_filter else 'off'}"


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


def signal(index, candles, bands, rsis, squeezes, rules, averages=None):
    """"buy", "sell" or None for the confirmation candle at [index] (candle 1 is index - 1).

    [averages], the 200-candle average, is needed only with the trend filter.
    """
    if index < 1 or bands[index - 1] is None or rsis[index] is None:
        return None

    trend = averages[index] if averages else None

    if rules.trend_filter and trend is None:
        return None

    _start, open1, high1, low1, close1 = candles[index - 1]
    _start, open2, _high2, _low2, close2 = candles[index]
    lower, _middle, upper = bands[index - 1]

    if rules.squeeze_filter and squeezes[index - 1]:
        return None

    recent = [value for value in rsis[max(0, index - RSI_LOOKBACK):index] if value is not None]

    touched_low = (low1 <= lower) if rules.touch == "wick" else (close1 <= lower)
    if touched_low and close1 > open1 and close2 > open2:
        if rules.rsi == "turned":
            turned = any(value < RSI_LOW for value in recent) and rsis[index] >= RSI_LOW
        else:
            turned = rsis[index] >= RSI_LOW

        if turned and not (rules.trend_filter and close2 <= trend):
            return "buy"

    touched_high = (high1 >= upper) if rules.touch == "wick" else (close1 >= upper)
    if touched_high and close1 < open1 and close2 < open2:
        if rules.rsi == "turned":
            turned = any(value > RSI_HIGH for value in recent) and rsis[index] <= RSI_HIGH
        else:
            turned = rsis[index] <= RSI_HIGH

        if turned and not (rules.trend_filter and close2 >= trend):
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
    """(bands, rsi, squeezed, ATR, 200-candle average) for [candles], worked out once for every set of rules."""
    closes = [candle[4] for candle in candles]
    bands = bollinger(closes)
    return bands, rsi(closes), squeezed(bands), average_true_range(candles), moving_average(closes)


def backtest(candles, rules=Rules(), worked_out=None):
    """Every trade [rules] would have taken over [candles] (fifteen-minute, bid), oldest first."""
    bands, rsis, squeezes, atrs, averages = worked_out or indicators(candles)
    trades = []
    open_trade = None

    for index, candle in enumerate(candles):
        if open_trade is not None:
            middle = bands[index - 1][1] if bands[index - 1] else None

            if _settle(open_trade, candle, rules, middle, atrs[index - 1]):
                trades.append(open_trade)
                open_trade = None
            continue

        closes_at = candle[0] + timedelta(minutes=CANDLE_MINUTES)

        if not in_session(closes_at, rules.session):
            continue

        side = signal(index, candles, bands, rsis, squeezes, rules, averages)

        touch = candles[index - 1]

        if side and rules.exits == "trail":
            if not atrs[index]:
                continue

            distance = TRAIL_FIRST_STOP * atrs[index]
            entry = candle[4] + rules.spread if side == "buy" else candle[4]
            stop = entry - distance if side == "buy" else entry + distance
            open_trade = Trade(side, closes_at, entry, stop, None, trailing=True, risk=distance, best=entry)
        elif side == "buy":
            entry = candle[4] + rules.spread   # bought at the ask
            if rules.exits == "bands":
                open_trade = Trade("buy", closes_at, entry, touch[3] - WICK_BUFFER, None)
            else:
                open_trade = Trade("buy", closes_at, entry, entry - rules.stop, entry + rules.target,
                                   breakeven=rules.breakeven, risk=rules.stop)
        elif side == "sell":
            entry = candle[4]                  # sold at the bid
            if rules.exits == "bands":
                open_trade = Trade("sell", closes_at, entry, touch[2] + rules.spread + WICK_BUFFER, None)
            else:
                open_trade = Trade("sell", closes_at, entry, entry + rules.stop, entry - rules.target,
                                   breakeven=rules.breakeven, risk=rules.stop)

    return trades


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

AS_WRITTEN = Rules()

# The version that made money in both halves of the year (59 trades, 32%
# won): RSI simply not beyond 30/70, fixed $10 stop and $30 target, the
# trend filter and the squeeze filter on. The trader uses these rules.
CHOSEN = Rules(rsi="not_extreme", exits="fixed", trend_filter=True)
