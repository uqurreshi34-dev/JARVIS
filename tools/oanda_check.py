"""Check JARVIS can see your OANDA demo account, and what OANDA's gold really is.

    python tools/oanda_check.py

Reads OANDA_API_TOKEN (and OANDA_ACCOUNT_ID, if set) from .env, asks the
practice server, and says: the account's currency and balance, OANDA's own
terms for gold (the smallest trade and how finely it can be sized), the
price and spread now, and how fresh the last finished 15-minute candle is
-- whether the candles arrive quickly enough to trade on.

Nothing is traded. The token is never printed.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import oanda  # noqa: E402


def main(client=None, now=None):
    if client is None:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        except ImportError:
            pass

        if not oanda.configured():
            print("OANDA_API_TOKEN is not in .env yet. Open an OANDA demo account, then My Account ->"
                  " Manage API Access -> Generate, and add the line OANDA_API_TOKEN=<the token> to .env.")
            return 1

        client = oanda.Client()

    try:
        summary = client.summary()
        gold = client.instrument()
        bid, ask, _when = client.price()
        candles, _asks = client.candles(count=3)
    except oanda.OandaError as error:
        print(f"Not yet: {error}.")
        return 1

    now = now or datetime.now(timezone.utc)
    print(f"Connected to your OANDA demo account: {summary['currency']} {summary['balance']:,.2f}, "
          f"{summary['open_trades']} open trade(s).")
    print(f"{gold['display_name']}: smallest trade {gold['minimum_units']:g} units (ounces), sized to "
          f"{gold['unit_decimals']} decimal place(s); margin {gold['margin_rate']:.0%} of the trade's value.")
    print(f"  At {gold['minimum_units']:g} units, each $1 gold moves is ${gold['minimum_units']:g}; at 1 unit, $1.")
    print(f"Gold now: bid {bid:.2f}, ask {ask:.2f}, spread ${ask - bid:.2f}.")

    if candles:
        start = candles[-1][0]
        late = now - (start + timedelta(minutes=15))
        print(f"Last finished 15-minute candle: {start:%d %b %H:%M} UTC, closed {int(late.total_seconds() // 60)}"
              f" minute(s) ago (open {candles[-1][1]:.2f}, close {candles[-1][4]:.2f}).")

        if late <= timedelta(minutes=16):
            print("Fresh: candles arrive as they close, quick enough to trade 15-minute candles on.")
        else:
            print("Not fresh just now -- normal when the market is closed (weekends, and an hour each evening).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
