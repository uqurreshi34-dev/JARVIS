"""Read Hyperliquid's top traders now and print the board JARVIS shows (actions/trader_watch.py).

    python tools/trader_watch.py              read the exchange afresh, save the board, print it
    python tools/trader_watch.py --offline    print the board last saved, without reading anything

Read-only: Hyperliquid's public leaderboard and each account's public
fills, profit history and open positions. No key, nothing that can trade.
Settings are in trader-watch.json in the JARVIS folder.
"""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import trader_watch as tw  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="print the saved board without reading the exchange")
    options = parser.parse_args(argv)

    if options.offline:
        board = tw.cached()

        if not board:
            print("No board saved yet: run without --offline.")
            return 1
    else:
        try:
            board = tw.refresh(report=lambda text: print(f"  reading {text}", flush=True))
        except tw.WatchError as error:
            print(f"{error}.")
            return 1

    print(f"\n{board['source']}, read {tw.updated_text(board)}: {len(board['traders'])} of {board['looked_at']} examined,"
          f" last {board['days']} days in {board['parts']} parts.\n")
    print(f"  {'#':>2}  {'trader':24} {'verdict':14} {'trades':>6} {'win':>5} {'p.f.':>5} {'net':>10}"
          f" {'deepest fall':>13}  parts")

    for index, trader in enumerate(board["traders"], 1):
        factor = f"{trader['profit_factor']:.2f}" if trader.get("profit_factor") else "-"
        parts = " ".join(tw.money(value) for value in trader["parts"])
        print(f"  {index:>2}  {tw.short_name(trader)[:24]:24} {trader['verdict']:14} {trader['trades']:>6}"
              f" {trader['win_rate']:>5.0%} {factor:>5} {tw.money(trader['net']):>10}"
              f" {tw.money(-trader['dip']):>8} {trader['dip_share']:>4.0%}  {parts}")

    print("\n" + tw.describe(board).replace(", sir", "").replace(" sir.", "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
