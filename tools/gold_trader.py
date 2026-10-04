"""Switch JARVIS's gold trader on or off, see how it has done, or have it look once now.

    python tools/gold_trader.py --on        trade on the OANDA demo account while JARVIS runs
    python tools/gold_trader.py --off       stop (an open trade keeps its stop and target at OANDA)
    python tools/gold_trader.py --status    the settings, and the results so far
    python tools/gold_trader.py --once      one look now, as JARVIS takes every 30 seconds, saying what it decided

The trader itself runs inside JARVIS (actions/gold_trader.py); this only
changes gold-trader.json in the JARVIS folder and reads gold-trades.csv.
"""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import gold_trader, oanda  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--on", action="store_true")
    which.add_argument("--off", action="store_true")
    which.add_argument("--status", action="store_true")
    which.add_argument("--once", action="store_true")
    options = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    if options.on or options.off:
        try:
            gold_trader.switch(options.on)
        except ValueError as error:
            print(error)
            return 1

        print(f"The gold trader is {'on: it trades the OANDA demo account while JARVIS runs' if options.on else 'off'}.")
        if options.on and not oanda.configured():
            print("OANDA_API_TOKEN is not in .env, so it cannot trade until it is.")
        return 0

    if options.status:
        chosen = gold_trader.settings()
        start, end = chosen["session_hours"]
        print(f"{'On' if chosen['enabled'] else 'Off'}. {chosen['units']:g} oz a trade, {', '.join(chosen['days'])}, "
              f"candles closing {start}:00 to {end}:00 UK time, at most {chosen['max_trades_per_day']} trades and "
              f"{chosen['max_losses_in_a_row']} losses in a row a day, spread under ${chosen['max_spread']:.2f}, anything open "
              f"closed Friday at {chosen['friday_close'][0]:02d}:{chosen['friday_close'][1]:02d}.")
        print(gold_trader.status().replace(", sir", ""))
        return 0

    if not oanda.configured():
        print("OANDA_API_TOKEN is not in .env.")
        return 1

    trader = gold_trader.GoldTrader()
    trader.set_listener(print)

    try:
        print(f"Decided: {trader.tick()}.")
    except oanda.OandaError as error:
        print(f"OANDA: {error}.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
