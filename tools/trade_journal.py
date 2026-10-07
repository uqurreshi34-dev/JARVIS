"""Your own trades at OANDA, from the journal JARVIS keeps (actions/trade_journal.py).

    python tools/trade_journal.py             bring the journal up to date from OANDA, list it, and sum it up
    python tools/trade_journal.py --offline   list and sum up what is already in it, without asking OANDA

The journal is manual-trades.csv in the JARVIS folder. To leave a trade out
of the numbers -- one placed by mistake -- change its "counted" to "no".
"""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import oanda, trade_journal  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="do not ask OANDA for newly closed trades")
    options = parser.parse_args(argv)
    currency = ""

    if not options.offline:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        except ImportError:
            pass

        if not oanda.configured():
            print("OANDA_API_TOKEN is not in .env; showing the journal as it is (--offline).")
        else:
            try:
                client = oanda.Client()
                added = trade_journal.sync(client)
                currency = client.summary().get("currency", "")
                print(f"{added} newly closed trade{'s' if added != 1 else ''} of your own added from OANDA.")
            except oanda.OandaError as error:
                print(f"OANDA: {error}. Showing the journal as it is.")

    kept = trade_journal.rows()

    if kept:
        print()
        widths = {column: max(len(column), *(len(row.get(column, "")) for row in kept))
                  for column in trade_journal.COLUMNS}
        print("  ".join(column.ljust(widths[column]) for column in trade_journal.COLUMNS))

        for row in kept:
            print("  ".join(row.get(column, "").ljust(widths[column]) for column in trade_journal.COLUMNS))

        print()

    print(trade_journal.summary(currency).replace(", sir", "").replace(" sir", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
