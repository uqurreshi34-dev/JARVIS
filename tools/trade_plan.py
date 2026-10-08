"""Print the trade plan JARVIS shows (actions/trade_plan.py): the lines with every swing that made them, and
what each page says -- to check the lines against your own chart.

    python tools/trade_plan.py gold
    python tools/trade_plan.py "crude oil"

Read-only: OANDA's candles and price, nothing placed. Settings in trade-plan.json in the JARVIS folder.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import gold_strategy, trade_plan as tp  # noqa: E402


def _when(ms):
    return gold_strategy.uk_time(datetime.fromtimestamp(ms / 1000, tz=timezone.utc)).strftime("%d %b %H:%M")


def report(found):
    d = found["decimals"]
    print(f"\n{found['instrument']} ({found['symbol']}), {found['timeframe']} candles to {found['read']} UK,"
          f" price {found['price']:,.{d}f}, ATR {found['atr']:,.{d}f}")
    print("\nLines (support below the price, resistance above), each from the swings price clearly turned at:")

    for line in reversed(found["levels"]):
        kind = "yours" if line["yours"] else ("resistance" if line["price"] > found["price"] else "support")
        mark = " <-" if line["price"] in (found["support"], found["resistance"]) else ""
        swings = ", ".join(f"{_when(when)} at {price:,.{d}f}" for when, price in line.get("swings") or [])
        print(f"  {line['price']:>12,.{d}f}  {kind:10}  {line['touches']} swings{mark}: {swings}")

    for page in (found, found.get("entry")):
        if page:
            print(f"\n{page['timeframe']}: {page['verdict'].upper()} -- {page['headline']}")

    if found.get("entry_error"):
        print(f"\nThe entry candles could not be read: {found['entry_error']}.")


def main(argv=None):
    words = " ".join(argv if argv is not None else sys.argv[1:]) or "gold"

    if not tp.which(words):
        print(f"Not a market I know: {words}. Known: {', '.join(tp.known())}.")
        return 1

    try:
        report(tp.read(f"{words} plan"))
    except tp.PlanError as error:
        print(f"Could not read the chart: {error}.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
