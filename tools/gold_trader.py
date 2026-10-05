"""Switch JARVIS's gold trader on or off, see how it has done, or have it look once now.

    python tools/gold_trader.py --on        trade on the OANDA demo account while JARVIS runs
    python tools/gold_trader.py --off       stop (an open trade keeps its stop and target at OANDA)
    python tools/gold_trader.py --status    the settings, and the results so far
    python tools/gold_trader.py --once      one look now, as JARVIS takes every 30 seconds, saying what it decided
    python tools/gold_trader.py --set risk_percent=1 practice_balance=400 max_trades_per_day=null
    python tools/gold_trader.py --set setups=bounce-1:3-be,pullback-atr-1:3-be
                                            change settings; values as in the file (numbers, true, null)

The trader itself runs inside JARVIS (actions/gold_trader.py); this only
changes gold-trader.json in the JARVIS folder and reads gold-trades.csv.
"""

import argparse
import json
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

            if name not in gold_trader.DEFAULTS or not text:
                print(f"Not a setting: {pair!r}. The settings are: {', '.join(gold_trader.DEFAULTS)}.")
                return 1

            try:
                changes[name] = json.loads(text)
            except ValueError:
                # A list may be given plainly: setups=bounce-1:3-be,pullback-atr-1:3-be
                changes[name] = text.split(",") if isinstance(gold_trader.DEFAULTS[name], list) else text

        try:
            gold_trader.change(changes)
        except ValueError as error:
            print(error)
            return 1

        print("Set: " + ", ".join(f"{name} = {json.dumps(value)}" for name, value in changes.items()) + ".")
        return 0

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
        if chosen["risk_percent"]:
            size = f"each trade risking {chosen['risk_percent']:g}% of " + (
                f"a practice balance of {chosen['practice_balance']:g} plus its results" if chosen["practice_balance"]
                else "the account")
        else:
            size = f"{chosen['units']:g} oz a trade"

        trades = "every setup" if chosen["max_trades_per_day"] is None else f"at most {chosen['max_trades_per_day']} trades a day"
        losses = ("never standing down" if chosen["max_losses_in_a_row"] is None
                  else f"standing down after {chosen['max_losses_in_a_row']} losses in a row")
        at_once = ("one trade at a time" if chosen["max_open_trades"] == 1
                   else f"up to {chosen['max_open_trades']} trades open at once, all the same way")
        forming = "saying when a setup may be forming" if chosen["announce_forming"] else "quiet until a trade"
        print(f"{'On' if chosen['enabled'] else 'Off'}. {size}, {', '.join(chosen['days'])}, candles closing {start}:00 "
              f"to {end}:00 {gold_trader.gold_strategy.ZONE_NAMES[chosen['session_zone']]} time, {trades}, {at_once}, {losses}, {forming}, spread under ${chosen['max_spread']:.2f}, "
              f"anything open closed Friday at {chosen['friday_close'][0]:02d}:{chosen['friday_close'][1]:02d}.")
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
