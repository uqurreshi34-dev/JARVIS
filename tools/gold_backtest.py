"""Backtest the gold strategy (actions/gold_strategy.py) over a year of past spot gold.

Runs the rules over months of past 15-minute spot gold, candle by candle as
if live, and reports what they would have done. Nothing is traded; nothing
is sent anywhere.

    python tools/gold_backtest.py                 the last year
    python tools/gold_backtest.py --days 120      a shorter look
    python tools/gold_backtest.py --hours 10 16   candles closing 10:00 to 16:00 UK instead of 10 to 14
    python tools/gold_backtest.py --trades        and write every trade to gold-backtest-trades.csv here
    python tools/gold_backtest.py --candle "2026-10-02 00:15"
                                                  one candle's prices (UTC), to compare with Fortrade's chart

The prices are OANDA's own gold (XAU/USD) when OANDA_API_TOKEN is in .env --
exactly what the trader trades on, with the spread as it really was in the
trading window -- or otherwise spot gold from Dukascopy's free public history,
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
whether RSI must have been extreme at all; whether to trade only with the
bigger trend (buys above the 200-candle average, sells below it); and the
exits -- the fixed $10 and $30; a stop just past candle 1's wick with the
middle band as the target, where a bounce off a band naturally heads; or
a trailing stop sized by how lively gold is (ATR), moved to the entry once
the trade is 1 R up and then following the best price. The row marked * is
the rules as first written.

Each row is also split into the first and second half of the period. A
rule found by trying many and keeping the best can look good by luck; one
that makes money in both halves, separately, is more likely to be real.

Costs are counted as on Fortrade: buys pay the spread ($0.80 by default)
on entry, and a stop or target is judged on the price it would really be
filled at. When one candle reaches both the stop and the target, the stop
is assumed: a backtest that guesses in its own favour flatters itself.
"""

import argparse
import csv
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.gold_strategy import (  # noqa: E402
    AS_WRITTEN, BREAKEVEN_AT_R, CHOSEN, SESSION, SPREAD, STOP_DOLLARS, TARGET_DOLLARS, TRAIL_DISTANCE,
    TRADEABLE_EXITS, TRAIL_FIRST_STOP, TREND_LENGTH, VARIANTS, WICK_BUFFER, backtest, fifteen_minute, in_session,
    indicators,
    minute_candles, summarise,
)


def _halves(candles, trades):
    """Net result of [trades] opened in the first and in the second half of [candles]' span."""
    middle = candles[0][0] + (candles[-1][0] - candles[0][0]) / 2
    first = sum(trade.result for trade in trades if trade.opened < middle)
    return first, sum(trade.result for trade in trades) - first


def report(candles, lot_ounces=1.0, spread=SPREAD, source="Spot gold", session=SESSION):
    first, last = candles[0][0], candles[-1][0]
    print(f"\n{source}, {len(candles)} fifteen-minute candles, {first:%d %b %Y} to {last:%d %b %Y} (UTC).")
    print(f"Each trade 1 oz (Fortrade's 0.01 lot, OANDA's 1 unit): $1 a dollar of movement. Spread ${spread:.2f}, candles "
          f"closing {session[0]}:00 to {session[1]}:00 UK time. 'fixed' exits: stop ${STOP_DOLLARS:g}, target "
          f"${TARGET_DOLLARS:g}.\n'bands' exits: stop ${WICK_BUFFER:g} past candle 1's wick, target the middle band."
          f"\n'trail' exits: stop {TRAIL_FIRST_STOP:g} ATR away, moved to the entry at {BREAKEVEN_AT_R:g} R, then "
          f"{TRAIL_DISTANCE:g} ATR behind the best price; no fixed target.\n'trend filter': buys only above the "
          f"{TREND_LENGTH}-candle average, sells only below it. The squeeze filter is on throughout.\n'ATR' exits: stop "
          f"1.5 ATR away, target 2 or 3 times that. 'pullback': joining the trend at the 20-candle average; "
          f"'breakout': a squeeze breaking out with the trend.\n")
    print(f"  {'rules':52} {'trades':>6} {'won':>4} {'net $':>8} {'risk $':>6} {'worst run':>9} {'deepest dip $':>13}"
          f" {'1st half $':>10} {'2nd half $':>10}  key")
    worked_out = indicators(candles)
    passing = []

    for rules in VARIANTS:
        mark = "*" if rules == AS_WRITTEN else "+" if rules == CHOSEN else " "
        trades = backtest(candles, replace(rules, spread=spread, session=session), worked_out)
        summary = summarise(trades)
        early, late = _halves(candles, trades)

        if worth_trading(summary, early, late) and rules.exits in TRADEABLE_EXITS:
            passing.append((summary.net, rules))

        print(f"{mark} {rules.name():52} {summary.trades:6d} {summary.win_rate:4.0%} {summary.net * lot_ounces:8.2f} "
              f"{summary.average_risk * lot_ounces:6.2f} {summary.worst_run:9d} {summary.deepest * lot_ounces:13.2f}"
              f" {early * lot_ounces:10.2f} {late * lot_ounces:10.2f}  {rules.key()}")

    print("\n* the rules as you first wrote them; + the version the trader uses. 'won' is the share of trades that made money; 'risk $' the"
          " average distance to the stop.\n'worst run' is the most losses in a row; 'deepest dip' the furthest the"
          " running total fell from its best.\nA rule worth trusting makes money in both halves, not just overall:"
          " one good half is often luck.")
    print("Past results are no promise of future ones.")
    _together(candles, worked_out, passing, spread, session, lot_ounces)


# What a version must show to be worth trading: enough trades to mean
# something, and money made in each half of the period on its own.
MOST_FEW_TRADES = 30


def worth_trading(summary, early, late):
    return summary.trades >= MOST_FEW_TRADES and early > 0 and late > 0


def entry(rules):
    """What decides when a version enters; versions alike in this differ only in their exits."""
    return (rules.setup, rules.rsi, rules.touch, rules.trend_filter, rules.squeeze_filter)


def best_per_entry(ranked):
    """From versions ranked best first, the first of each kind of entry."""
    seen = set()
    kept = []

    for rules in ranked:
        if entry(rules) not in seen:
            seen.add(entry(rules))
            kept.append(rules)

    return kept


def _together(candles, worked_out, passing, spread, session, lot_ounces):
    """The versions worth trading, run together as the trader would: one trade at a time, best first."""
    if not passing:
        print(f"\nNo version made money in both halves with at least {MOST_FEW_TRADES} trades. Keep the trader's"
              " current setups, or try other hours.")
        return

    ranked = [rules for _net, rules in sorted(passing, key=lambda pair: -pair[0])]
    chosen = best_per_entry(ranked)
    trades = backtest(candles, [replace(rules, spread=spread, session=session) for rules in chosen], worked_out)
    summary = summarise(trades)
    early, late = _halves(candles, trades)
    print(f"\nWorth trading -- at least {MOST_FEW_TRADES} trades and money made in both halves: "
          f"{', '.join(rules.key() for rules in ranked)}.")
    print("Versions that enter on the same candle only differ in how they leave, and one trade is open at a time,"
          f" so the best exits for each kind of entry: {', '.join(rules.key() for rules in chosen)}.")
    print(f"Run together, one trade at a time: {summary.trades} trades, {summary.win_rate:.0%} won, "
          f"{summary.net * lot_ounces:+.2f} $ ({early * lot_ounces:+.2f}, {late * lot_ounces:+.2f} by half), "
          f"worst run {summary.worst_run}, deepest dip {summary.deepest * lot_ounces:.2f} $.")
    print(f"To trade them all: python tools/gold_trader.py --set setups={','.join(rules.key() for rules in chosen)}")


def check_candle(candles, when_utc):
    """The candle starting at [when_utc], to compare with Fortrade's chart."""
    for candle in candles:
        if candle[0] == when_utc:
            return candle
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=365, help="how many days back to test (default 365)")
    parser.add_argument("--trades", action="store_true", help="write the trader's rules' trades to gold-backtest-trades.csv here")
    parser.add_argument("--hours", type=int, nargs=2, metavar=("FROM", "TO"), default=list(SESSION),
                        help="the UK hours candles may close in, e.g. --hours 10 16 (default 10 14)")
    parser.add_argument("--source", choices=("oanda", "dukascopy"),
                        help="whose prices: OANDA's own (the default when OANDA_API_TOKEN is set), or Dukascopy's")
    parser.add_argument("--candle", metavar="'YYYY-MM-DD HH:MM'",
                        help="show the fifteen-minute candle starting then (UTC), to compare with your broker's chart")
    options = parser.parse_args(argv)

    last = datetime.now(timezone.utc).date() - timedelta(days=1)
    first = last - timedelta(days=max(7, options.days))
    session = tuple(options.hours)

    if not 0 <= session[0] < session[1] <= 24:
        print("--hours wants two UK hours, the first before the second, e.g. --hours 10 16.")
        return 1

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    from actions import oanda

    source = options.source or ("oanda" if oanda.configured() else "dukascopy")
    spread = SPREAD

    if source == "oanda":
        print(f"Gold (XAU/USD) from your OANDA demo account, {first} to {last}: the prices the trader trades on...")

        try:
            start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
            end = datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1)
            candles, ask_closes = oanda.Client().history(start, end)
        except oanda.OandaError as error:
            print(f"OANDA: {error}.")
            return 1

        # The spread as it really was in the trading window, not at night when it widens.
        spreads = sorted(ask - candle[4] for candle, ask in zip(candles, ask_closes)
                         if in_session(candle[0] + timedelta(minutes=15), session))

        if spreads:
            spread = spreads[len(spreads) // 2]

        label = "OANDA gold"
    else:
        print(f"Spot gold from Dukascopy, {first} to {last} (downloaded once, then kept)...")

        try:
            candles = fifteen_minute(minute_candles(first, last))
        except RuntimeError as error:
            print(error)
            return 1

        label = "Spot gold (Dukascopy)"

    if len(candles) < 500:
        print("Too few prices came back to test anything; check the connection and try again.")
        return 1

    median = sorted(candle[4] for candle in candles)[len(candles) // 2]

    if not 300 <= median <= 30000:
        print(f"The prices look wrong (a typical close of {median:.2f}); not testing on them.")
        return 1

    report(candles, spread=spread, source=label, session=session)

    if options.candle:
        wanted = datetime.strptime(options.candle, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        found = check_candle(candles, wanted)

        if found:
            print(f"\nThe candle from {options.candle} UTC: open {found[1]:.2f}, high {found[2]:.2f}, "
                  f"low {found[3]:.2f}, close {found[4]:.2f}. Your chart should be within a dollar or so.")
        else:
            print(f"\nNo candle starts at {options.candle} UTC in these prices.")

    if options.trades:
        with open("gold-backtest-trades.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["side", "opened (UTC)", "entry", "stop", "target", "closed (UTC)", "exit", "result $", "how"])

            for trade in backtest(candles, replace(CHOSEN, spread=spread, session=session)):
                writer.writerow([trade.side, f"{trade.opened:%Y-%m-%d %H:%M}", f"{trade.entry:.2f}", f"{trade.stop:.2f}",
                                 f"{trade.target:.2f}", f"{trade.closed:%Y-%m-%d %H:%M}" if trade.closed else "",
                                 f"{trade.exit:.2f}" if trade.exit is not None else "",
                                 f"{trade.result:.2f}" if trade.result is not None else "", trade.reason])

        print("Every trade of the trader's rules is in gold-backtest-trades.csv.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
