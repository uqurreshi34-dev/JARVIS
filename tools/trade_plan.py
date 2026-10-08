"""Print the trade plan JARVIS shows (actions/trade_plan.py): the lines with every swing that made them, and
what each page says -- to check the lines against your own chart.

    python tools/trade_plan.py gold
    python tools/trade_plan.py "crude oil"
    python tools/trade_plan.py gold --save     and keep the four-hour candles in the JARVIS folder, one
                                               file a market (trade-plan-candles-XAU_USD.csv), to check
                                               the lines against

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
    print("\nLines, each through peaks that stand out (<- the resistance and support in use: the most recent"
          " pair of close highs above the price, of lows below it):")

    for line in reversed(found["levels"]):
        kind = "yours" if line["yours"] else f"{line.get('kind') or ''}s".replace("highs", "highs (resistance)").replace(
            "lows", "lows (support)")
        mark = " <-" if line["price"] in (found["support"], found["resistance"]) else ""
        swings = ", ".join(f"{_when(when)} at {price:,.{d}f}" for when, price in line.get("swings") or [])
        print(f"  {line['price']:>12,.{d}f}  {kind:18}  {line['touches']} peaks{mark}: {swings}")

    for page in (found, found.get("entry")):
        if page:
            print(f"\n{page['timeframe']}: {page['verdict'].upper()} -- {page['headline']}")

    if found.get("entry_error"):
        print(f"\nThe entry candles could not be read: {found['entry_error']}.")


def save(found):
    """The chart's candles, as drawn, to a file beside JARVIS's others."""
    import csv
    import os

    from actions import files

    path = os.path.join(files.root(), f"trade-plan-candles-{found['symbol']}.csv")

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start (UK)", "open", "high", "low", "close"])

        for row in found["read_candles"]:
            writer.writerow(row)

    print(f"\nThe {len(found['read_candles'])} candles the lines came from are in {path}.")


def main(argv=None):
    words = argv if argv is not None else sys.argv[1:]
    keep = "--save" in words
    words = " ".join(word for word in words if word != "--save") or "gold"

    # The OANDA token, as JARVIS reads it at start-up; the other tools do the same.
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    if not tp.which(words):
        print(f"Not a market I know: {words}. Known: {', '.join(tp.known())}.")
        return 1

    try:
        found = tp.read(f"{words} plan")
        report(found)

        if keep:
            save(found)
    except tp.PlanError as error:
        print(f"Could not read the chart: {error}.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
