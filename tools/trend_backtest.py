"""Backtest the slow daily trend-follower (actions/trend_strategy.py) over many years of OANDA's prices.

A daily strategy trades a few times a year, so a fair test needs many
years: fifteen by default, judged in five parts, every part on its own.
Nothing is traded; nothing is sent anywhere but OANDA's own price history.

    python tools/trend_backtest.py                          gold, the last fifteen years
    python tools/trend_backtest.py --instrument XAG_USD     silver
    python tools/trend_backtest.py --years 20 --periods 4   a longer look, in four parts
    python tools/trend_backtest.py --financing 3            holding costs 3% a year instead of 5%

Needs OANDA_API_TOKEN in .env (the demo account's), as the prices are
OANDA's daily candles, the ones a trader would trade on.

Results are in R -- the first stop's distance -- as a trader risking a set
share of the account sees them: risking 1% a trade, +10 R is about +10% of
the account. Dollars are for one unit, after the spread and financing.
Every row is split into parts of the period: a rule found by trying twelve
and keeping the best can look good by luck; one that makes money in every
part, separately, is more likely to be real.
"""

import argparse
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.trend_strategy import (  # noqa: E402
    ATR_LENGTH, FINANCING_PERCENT, FIRST_STOP_ATR, TRAIL_ATR, VARIANTS, backtest, parts, summarise,
)


YEARS = 15
PERIODS = 5

# What a version must show to be worth trading: enough trades to mean something, and money made in every part.
MOST_FEW_TRADES = 20


def worth_trading(summary, nets):
    return summary.trades >= MOST_FEW_TRADES and all(net > 0 for net in nets)


def report(candles, instrument, spread, financing, periods):
    first, last = candles[0][0], candles[-1][0]
    print(f"\nOANDA {instrument}, {len(candles)} daily candles, {first:%d %b %Y} to {last:%d %b %Y}.")
    print(f"First stop {FIRST_STOP_ATR:g} ATR ({ATR_LENGTH} days) away; the 'channel' exit is a close beyond half the entry's"
          f" days' opposite extreme, the 'ATR trail' a stop {TRAIL_ATR:g} ATR behind the best close.\nSpread"
          f" ${spread:.2f}; financing {financing:g}% a year of the position, every day a trade is held, both ways.\n")
    step = (last - first) / periods
    heads = [f"from {first + step * part:%b %y} R" for part in range(periods)]
    print(f"  {'rules':58} {'trades':>6} {'won':>4} {'net R':>7} {'net $':>9} {'days held':>9} {'worst run':>9}"
          f" {'deepest dip R':>13} " + " ".join(f"{head:>13}" for head in heads) + "  key")
    passing = []

    for rules in VARIANTS:
        trades = backtest(candles, replace(rules, spread=spread, financing=financing))
        summary = summarise(trades)
        nets = parts(candles, trades, periods)

        if worth_trading(summary, nets):
            passing.append((summary.net_r, rules, summary))

        print(f"  {rules.name():58} {summary.trades:6d} {summary.win_rate:4.0%} {summary.net_r:7.1f} {summary.net:9.2f}"
              f" {summary.average_days:9.0f} {summary.worst_run:9d} {summary.deepest_r:13.1f} "
              + " ".join(f"{net:13.1f}" for net in nets) + f"  {rules.key()}")

    print("\n'won' is the share of trades that made money; 'days held' the average; 'worst run' the most losses in a row;"
          "\n'deepest dip' the furthest the running total fell from its best, in R. Risking 1% a trade, 1 R is about"
          " 1% of the account.\nPast results are no promise of future ones.")

    if not passing:
        print(f"\nNo version made money in every part with at least {MOST_FEW_TRADES} trades: no trend-follower is worth"
              f" building for {instrument} on this evidence.")
        return []

    passing.sort(key=lambda found: -found[0])
    print(f"\nWorth trading -- at least {MOST_FEW_TRADES} trades and money made in every part: "
          + ", ".join(f"{rules.key()} ({summary.net_r:+.1f} R)" for _net, rules, summary in passing) + ".")
    return [rules for _net, rules, _summary in passing]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--instrument", default="XAU_USD", type=str.upper,
                        help="OANDA's name for what to test, e.g. XAG_USD for silver (default XAU_USD, gold)")
    parser.add_argument("--years", type=int, default=YEARS, help=f"how many years back to test (default {YEARS})")
    parser.add_argument("--periods", type=int, default=PERIODS, help=f"how many parts to judge them in (default {PERIODS})")
    parser.add_argument("--financing", type=float, default=FINANCING_PERCENT,
                        help=f"the cost of holding, percent of the position a year (default {FINANCING_PERCENT:g})")
    options = parser.parse_args(argv)

    if not re.fullmatch(r"[A-Z0-9]{2,10}_[A-Z]{3}", options.instrument):
        print(f"--instrument wants OANDA's name for it, such as XAU_USD or XAG_USD, not {options.instrument!r}.")
        return 1

    if options.years < 2 or options.periods < 2 or not 0 <= options.financing <= 50:
        print("--years and --periods want 2 or more, and --financing a yearly percentage from 0 to 50.")
        return 1

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    from actions import oanda

    if not oanda.configured():
        print("The trend backtest uses OANDA's own daily prices: set OANDA_API_TOKEN in .env.")
        return 1

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=round(365.25 * options.years))
    print(f"{options.instrument} from your OANDA demo account, daily, {start:%Y-%m-%d} to {end:%Y-%m-%d}...")

    try:
        candles, ask_closes = oanda.Client().history(start, end, name=options.instrument, granularity="D")
    except oanda.OandaError as error:
        print(f"OANDA: {error}.")
        return 1

    if len(candles) < 250:
        print(f"Only {len(candles)} days of prices came back; a daily strategy needs years of them to be judged.")
        return 1

    spreads = sorted(ask - candle[4] for candle, ask in zip(candles, ask_closes))
    report(candles, options.instrument, spreads[len(spreads) // 2], options.financing, options.periods)
    return 0


if __name__ == "__main__":
    sys.exit(main())
