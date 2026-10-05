"""The daily trend-follower (actions/trend_strategy.py) and its backtest (tools/trend_backtest.py).

Checked on made-up daily prices, with a pretend OANDA:

- a close above the last N days' high is a buy, below their low a sell,
  and "buys only" takes no sells;
- a trade opens at the signalling close, its first stop 2 ATR away; a
  channel exit closes it at a close beyond the opposite extreme of half as
  many days, and a trailing stop only ever moves forward;
- a day that opens beyond the stop is filled at its open, not the stop;
- financing is charged for every day held, and results are in R as well
  as dollars; each trade counts once in the part of the period it opened;
- the backtest reads OANDA's daily candles for the instrument asked, and
  refuses without a token, or with a name OANDA would not know.

    python tools/test_trend_backtest.py
"""

import contextlib
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import oanda  # noqa: E402
from actions import trend_strategy as ts  # noqa: E402
from tools import trend_backtest as cli  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def days(closes, start=datetime(2020, 1, 1, 22, 0, tzinfo=timezone.utc), wick=1.0):
    """Daily candles opening at the last close, a dollar of wick either side."""
    made, previous = [], closes[0]

    for step, close in enumerate(closes):
        made.append((start + timedelta(days=step), previous, max(previous, close) + wick, min(previous, close) - wick, close))
        previous = close

    return made


# ---- signals -------------------------------------------------------------------------------------

flat_then_up = days([100.0] * 30 + [106.0])
rules = ts.Rules(entry_days=20)
check(ts.signal(29, flat_then_up, rules) is None and ts.signal(30, flat_then_up, rules) == "buy",
      "a close above the last 20 days' high is a buy; a flat day is nothing")
flat_then_down = days([100.0] * 30 + [94.0])
check(ts.signal(30, flat_then_down, rules) == "sell" and ts.signal(30, flat_then_down, ts.Rules(entry_days=20, sides="long")) is None,
      "a close below their low is a sell, and buys only takes none")
check(ts.signal(10, flat_then_up, rules) is None, "not before there are 20 days to look back on")

# ---- a trade, out on the channel ---------------------------------------------------------------

climb = [100.0] * 30 + [100.0 + 2 * step for step in range(1, 31)] + [160.0 - 3 * step for step in range(1, 21)]
made = days(climb)
trades = ts.backtest(made, ts.Rules(entry_days=20, exit="channel", sides="long", financing=0.0))
first = trades[0] if trades else None
atrs = ts.average_true_range(made, ts.ATR_LENGTH)
check(first is not None and first.side == "buy" and first.entry == 102.0 and abs(first.risk - 2 * atrs[30]) < 1e-9
      and abs(first.stop - (102.0 - first.risk)) < 1e-9,
      "the breakout opens a buy at its close, the first stop 2 ATR below")
check(first is not None and first.reason == "channel" and first.result > 0 and first.r > 1,
      f"and the turn closes it on a close below the last 10 days' low, well in profit ({first.r if first else 0:.1f} R)")

# ---- financing --------------------------------------------------------------------------------

charged = ts.backtest(made, ts.Rules(entry_days=20, exit="channel", sides="long", financing=5.0))[0]
held = (charged.closed - charged.opened).days
check(abs(charged.financing - charged.entry * 0.05 * held / 365) < 1e-9 and abs(charged.result - (first.result - charged.financing)) < 1e-9,
      f"holding costs 5% a year of the position, for each of the {held} days held")

# ---- the stop, and a gap through it -------------------------------------------------------------

drop = days([100.0] * 30 + [106.0, 107.0])
drop.append((drop[-1][0] + timedelta(days=1), 90.0, 91.0, 89.0, 90.5))   # opens far below the stop
gapped = ts.backtest(drop, ts.Rules(entry_days=20, exit="channel", financing=0.0))
check(len(gapped) == 1 and gapped[0].reason == "stop" and gapped[0].exit == 90.0 and gapped[0].r < -1,
      "a day opening beyond the stop is filled at its open, a worse price than the stop")

# ---- a trailing stop only moves forward ----------------------------------------------------------

up_and_back = [100.0] * 30 + [100.0 + 2 * step for step in range(1, 21)] + [140.0 - step for step in range(1, 40)]
trail_trades = ts.backtest(days(up_and_back), ts.Rules(entry_days=20, exit="atr", financing=0.0))
check(trail_trades and trail_trades[0].reason == "stop" and trail_trades[0].exit > trail_trades[0].entry,
      "the ATR trail follows the rise and, once it turns, takes the profit")

# ---- summaries and parts ---------------------------------------------------------------------------

summary = ts.summarise(trades)
nets = ts.parts(made, trades, 3)
check(summary.trades == len(trades) and abs(sum(nets) - summary.net_r) < 1e-9 and summary.average_days > 0,
      "each trade counted once, in the part it opened in; the average days held")
check(len(ts.VARIANTS) == 12 and len(ts.REGISTRY) == 12 and "trend-55-channel-long" in ts.REGISTRY,
      "twelve versions, each with its own key")
check(cli.worth_trading(ts.Summary(trades=25), [1.0, 2.0]) and not cli.worth_trading(ts.Summary(trades=10), [1.0, 2.0])
      and not cli.worth_trading(ts.Summary(trades=25), [1.0, -0.5]),
      "worth trading: at least 20 trades and money made in every part")

# ---- the backtest, on a pretend OANDA ------------------------------------------------------------

long_run = days([1500.0 + 300 * ((step // 90) % 2) + (step % 90) * (1 if (step // 90) % 2 == 0 else -1)
                 for step in range(1500)])


class History:
    asked = {}

    def history(self, start, end, name="XAU_USD", granularity="M15"):
        History.asked = {"name": name, "granularity": granularity}
        return long_run, [candle[4] + 0.4 for candle in long_run]


real_client, real_configured = oanda.Client, oanda.configured
oanda.Client, oanda.configured = History, (lambda: True)
printed = io.StringIO()

try:
    with contextlib.redirect_stdout(printed):
        code = cli.main(["--instrument", "xag_usd", "--years", "5"])
        bad = cli.main(["--instrument", "silver"])
    oanda.configured = lambda: False
    with contextlib.redirect_stdout(io.StringIO()):
        no_token = cli.main([])
finally:
    oanda.Client, oanda.configured = real_client, real_configured

output = printed.getvalue()
check(code == 0 and History.asked == {"name": "XAG_USD", "granularity": "D"} and "OANDA XAG_USD, 1500 daily candles" in output
      and "Spread $0.40" in output and output.count("trend-") >= 12,
      "it reads OANDA's daily candles for the instrument asked, with the spread as it was, and runs every version")
check(bad == 1 and no_token == 1, "and refuses a name OANDA would not know, or no token")

sys.exit(1 if failures else 0)
