"""Backtest the gold strategy: Bollinger band touch, RSI turning back, a confirmation candle.

Runs the rules over months of past 15-minute spot gold, candle by candle as
if live, and reports what they would have done. Nothing is traded; nothing
is sent anywhere.

    python tools/gold_backtest.py                 the last year
    python tools/gold_backtest.py --days 120      a shorter look
    python tools/gold_backtest.py --trades        and write every trade to gold-trades.csv here
    python tools/gold_backtest.py --candle "2026-10-02 00:15"
                                                  one candle's prices (UTC), to compare with Fortrade's chart

The prices are spot gold (XAU/USD) from Dukascopy's free public history,
one-minute candles built into fifteen-minute ones. Fortrade's Gold (USD) is
a CFD on spot gold too, so the two agree within a dollar or so -- unlike the COMEX
futures TradingView reports, which run dollars away. The first run
downloads about a megabyte a month and keeps it, so later runs are instant.

The rules (a buy; a sell is the mirror image):

1. Candle 1 touches the lower Bollinger band (20 candles, 2 standard
   deviations) and closes green.
2. Candle 2, the confirmation, closes green too.
3. RSI (14 candles) was below 30 and is back above it by candle 2's close.
4. Enter as candle 2 closes. Stop $10 below, target $30 above (1:3).
5. Only on candles closing 10:00 to 14:00 UK time, one trade at a time,
   and none while the bands are squeezed.

Where the rules leave a choice, every combination is run side by side:
whether RSI must have been extreme at all, whether the squeeze filter is
on, and the exits -- the fixed $10 and $30, or a stop just past candle 1's
wick with the middle band as the target, which is where a bounce off a
band naturally heads. The row marked * is the rules as first written.

Each row is also split into the first and second half of the period. A
rule found by trying many and keeping the best can look good by luck; one
that makes money in both halves, separately, is more likely to be real.

Costs are counted as on Fortrade: buys pay the spread ($0.80 by default)
on entry, and a stop or target is judged on the price it would really be
filled at. When one candle reaches both the stop and the target, the stop
is assumed: a backtest that guesses in its own favour flatters itself.
"""

import argparse
import concurrent.futures
import csv
import lzma
import math
import os
import struct
import sys
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
    exits: str = "fixed"         # "fixed": $10 stop, $30 target; "bands": stop past candle 1's wick, target the middle band
    stop: float = STOP_DOLLARS
    target: float = TARGET_DOLLARS
    spread: float = SPREAD
    session: tuple = SESSION

    def name(self):
        return (f"rsi={self.rsi:11} exits={self.exits:5} squeeze filter={'on ' if self.squeeze_filter else 'off'}")


@dataclass
class Trade:
    side: str
    opened: datetime
    entry: float
    stop: float
    target: float                # None: the middle band, wherever it is
    closed: datetime = None
    exit: float = None
    result: float = None
    reason: str = ""


def signal(index, candles, bands, rsis, squeezes, rules):
    """"buy", "sell" or None for the confirmation candle at [index] (candle 1 is index - 1)."""
    if index < 1 or bands[index - 1] is None or rsis[index] is None:
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

        if turned:
            return "buy"

    touched_high = (high1 >= upper) if rules.touch == "wick" else (close1 >= upper)
    if touched_high and close1 < open1 and close2 < open2:
        if rules.rsi == "turned":
            turned = any(value > RSI_HIGH for value in recent) and rsis[index] <= RSI_HIGH
        else:
            turned = rsis[index] <= RSI_HIGH

        if turned:
            return "sell"

    return None


def _settle(trade, candle, rules, middle=None):
    """Close [trade] if [candle] (bid prices) reaches its stop or target; the stop first if both.

    A trade aiming for the middle band aims at [middle], the band as it stood
    when the candle began, so nothing is known before it could be.
    """
    start, _open, high, low, _close = candle
    closes_at = start + timedelta(minutes=CANDLE_MINUTES)
    target = trade.target if trade.target is not None else middle

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

    return False


def indicators(candles):
    """(bands, rsi, squeezed) for [candles], worked out once for every set of rules."""
    closes = [candle[4] for candle in candles]
    bands = bollinger(closes)
    return bands, rsi(closes), squeezed(bands)


def backtest(candles, rules=Rules(), worked_out=None):
    """Every trade [rules] would have taken over [candles] (fifteen-minute, bid), oldest first."""
    bands, rsis, squeezes = worked_out or indicators(candles)
    trades = []
    open_trade = None

    for index, candle in enumerate(candles):
        if open_trade is not None:
            middle = bands[index - 1][1] if bands[index - 1] else None

            if _settle(open_trade, candle, rules, middle):
                trades.append(open_trade)
                open_trade = None
            continue

        closes_at = candle[0] + timedelta(minutes=CANDLE_MINUTES)

        if not in_session(closes_at, rules.session):
            continue

        side = signal(index, candles, bands, rsis, squeezes, rules)

        touch = candles[index - 1]

        if side == "buy":
            entry = candle[4] + rules.spread   # bought at the ask
            if rules.exits == "bands":
                open_trade = Trade("buy", closes_at, entry, touch[3] - WICK_BUFFER, None)
            else:
                open_trade = Trade("buy", closes_at, entry, entry - rules.stop, entry + rules.target)
        elif side == "sell":
            entry = candle[4]                  # sold at the bid
            if rules.exits == "bands":
                open_trade = Trade("sell", closes_at, entry, touch[2] + rules.spread + WICK_BUFFER, None)
            else:
                open_trade = Trade("sell", closes_at, entry, entry + rules.stop, entry - rules.target)

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
        summary.risked += abs(trade.entry - trade.stop)
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


# ---- the report ------------------------------------------------------------------------------

# A close beyond the band barely ever happened (one to four trades in four
# months), so only a wick reaching it is tried.
VARIANTS = [Rules(rsi=rule, exits=exits, squeeze_filter=squeeze)
            for rule in ("turned", "not_extreme") for exits in ("fixed", "bands") for squeeze in (True, False)]

AS_WRITTEN = Rules()


def _halves(candles, trades):
    """Net result of [trades] opened in the first and in the second half of [candles]' span."""
    middle = candles[0][0] + (candles[-1][0] - candles[0][0]) / 2
    first = sum(trade.result for trade in trades if trade.opened < middle)
    return first, sum(trade.result for trade in trades) - first


def report(candles, lot_ounces=1.0):
    first, last = candles[0][0], candles[-1][0]
    print(f"\nSpot gold, {len(candles)} fifteen-minute candles, {first:%d %b %Y} to {last:%d %b %Y} (UTC).")
    print(f"Each trade 0.01 lot ({lot_ounces:g} oz): $1 a dollar of movement. Spread ${SPREAD:.2f}, candles "
          f"closing {SESSION[0]}:00 to {SESSION[1]}:00 UK time. 'fixed' exits: stop ${STOP_DOLLARS:g}, target "
          f"${TARGET_DOLLARS:g}.\n'bands' exits: stop ${WICK_BUFFER:g} past candle 1's wick, target the middle band.\n")
    print(f"  {'rules':48} {'trades':>6} {'won':>4} {'net $':>8} {'risk $':>6} {'worst run':>9} {'deepest dip $':>13}"
          f" {'1st half $':>10} {'2nd half $':>10}")
    worked_out = indicators(candles)

    for rules in VARIANTS:
        trades = backtest(candles, rules, worked_out)
        summary = summarise(trades)
        early, late = _halves(candles, trades)
        mark = "*" if rules == AS_WRITTEN else " "
        print(f"{mark} {rules.name():48} {summary.trades:6d} {summary.win_rate:4.0%} {summary.net * lot_ounces:8.2f} "
              f"{summary.average_risk * lot_ounces:6.2f} {summary.worst_run:9d} {summary.deepest * lot_ounces:13.2f}"
              f" {early * lot_ounces:10.2f} {late * lot_ounces:10.2f}")

    print("\n* the rules as you first wrote them. 'won' is the share of trades that made money; 'risk $' the"
          " average distance to the stop.\n'worst run' is the most losses in a row; 'deepest dip' the furthest the"
          " running total fell from its best.\nA rule worth trusting makes money in both halves, not just overall:"
          " one good half is often luck.")
    print("Past results are no promise of future ones.")


def check_candle(candles, when_utc):
    """The candle starting at [when_utc], to compare with Fortrade's chart."""
    for candle in candles:
        if candle[0] == when_utc:
            return candle
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=365, help="how many days back to test (default 365)")
    parser.add_argument("--trades", action="store_true", help="write the as-written rules' trades to gold-trades.csv")
    parser.add_argument("--candle", metavar="'YYYY-MM-DD HH:MM'",
                        help="show the fifteen-minute candle starting then (UTC), to compare with Fortrade's chart")
    options = parser.parse_args(argv)

    last = datetime.now(timezone.utc).date() - timedelta(days=1)
    first = last - timedelta(days=max(7, options.days))
    print(f"Spot gold from Dukascopy, {first} to {last} (downloaded once, then kept)...")

    try:
        candles = fifteen_minute(minute_candles(first, last))
    except RuntimeError as error:
        print(error)
        return 1

    if len(candles) < 500:
        print("Too few prices came back to test anything; check the connection and try again.")
        return 1

    median = sorted(candle[4] for candle in candles)[len(candles) // 2]

    if not 300 <= median <= 30000:
        print(f"The prices look wrong (a typical close of {median:.2f}); not testing on them.")
        return 1

    report(candles)

    if options.candle:
        wanted = datetime.strptime(options.candle, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        found = check_candle(candles, wanted)

        if found:
            print(f"\nThe candle from {options.candle} UTC: open {found[1]:.2f}, high {found[2]:.2f}, "
                  f"low {found[3]:.2f}, close {found[4]:.2f}. Fortrade's chart should be within a dollar or so.")
        else:
            print(f"\nNo candle starts at {options.candle} UTC in these prices.")

    if options.trades:
        with open("gold-trades.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["side", "opened (UTC)", "entry", "stop", "target", "closed (UTC)", "exit", "result $", "how"])

            for trade in backtest(candles, AS_WRITTEN):
                writer.writerow([trade.side, f"{trade.opened:%Y-%m-%d %H:%M}", f"{trade.entry:.2f}", f"{trade.stop:.2f}",
                                 f"{trade.target:.2f}", f"{trade.closed:%Y-%m-%d %H:%M}" if trade.closed else "",
                                 f"{trade.exit:.2f}" if trade.exit is not None else "",
                                 f"{trade.result:.2f}" if trade.result is not None else "", trade.reason])

        print("Every trade of the rules as written is in gold-trades.csv.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
