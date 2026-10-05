"""Switch JARVIS's trend trader on or off, see how it has done, or have it look once now.

    python tools/trend_trader.py --on        trade on the OANDA demo account while JARVIS runs
    python tools/trend_trader.py --off       stop (an open trade keeps its stop at OANDA)
    python tools/trend_trader.py --status    the settings, and the results so far
    python tools/trend_trader.py --once      one look now, as JARVIS takes every five minutes, saying what it decided
    python tools/trend_trader.py --set risk_percent=1 practice_balance=400
    python tools/trend_trader.py --set instrument=NAS100_USD setup=trend-20-atr-long
                                             change settings; values as in the file (numbers, true, null)

The trader itself runs inside JARVIS (actions/trend_trader.py); this only
changes trend-trader.json in the JARVIS folder and reads trend-trades.csv.
"""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import oanda, trend_trader  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--on", action="store_true")
    which.add_argument("--off", action="store_true")
    which.add_argument("--status", action="store_true")
    which.add_argument("--once", action="store_true")
    which.add_argument("--set", nargs="+", metavar="NAME=VALUE")
    options = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    if options.set:
        changes = {}

        for pair in options.set:
            name, _, text = pair.partition("=")

            if name not in trend_trader.DEFAULTS or not text:
                print(f"Not a setting: {pair!r}. The settings are: {', '.join(trend_trader.DEFAULTS)}.")
                return 1

            try:
                changes[name] = json.loads(text)
            except ValueError:
                changes[name] = text

        try:
            trend_trader.change(changes)
        except ValueError as error:
            print(error)
            return 1

        print("Set: " + ", ".join(f"{name} = {json.dumps(value)}" for name, value in changes.items()) + ".")
        return 0

    if options.on or options.off:
        try:
            trend_trader.switch(options.on)
        except ValueError as error:
            print(error)
            return 1

        print(f"The trend trader is {'on: it trades the OANDA demo account while JARVIS runs' if options.on else 'off'}.")

        if options.on and not oanda.configured():
            print("OANDA_API_TOKEN is not in .env, so it cannot trade until it is.")

        return 0

    if options.status:
        chosen = trend_trader.settings()
        size = (f"each trade risking {chosen['risk_percent']:g}% of "
                + (f"a practice balance of {chosen['practice_balance']:g} plus its results" if chosen["practice_balance"]
                   else "the account")) if chosen["risk_percent"] else f"{chosen['units']:g} units a trade"
        print(f"{'On' if chosen['enabled'] else 'Off'}. {chosen['instrument']}, {chosen['setup']}, {size}; looks once a"
              f" day, after OANDA's daily candle closes, and holds trades over weekends.")
        print(trend_trader.status().replace(", sir", ""))
        return 0

    if not oanda.configured():
        print("OANDA_API_TOKEN is not in .env.")
        return 1

    trader = trend_trader.TrendTrader()
    trader.set_listener(print)

    try:
        print(f"Decided: {trader.tick()}.")
    except oanda.OandaError as error:
        print(f"OANDA: {error}.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
