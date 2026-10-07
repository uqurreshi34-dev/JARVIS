"""The journal of your own trades (actions/trade_journal.py, tools/trade_journal.py).

Checked against a pretend OANDA holding the two hand-placed trades of
7 October 2026 -- a 0.1 sell stopped out for -0.31 and a 0.5 sell stopped
out for -4.99 -- beside trades of the gold and trend traders', in a
sandboxed JARVIS folder:

- only trades placed by hand are copied, never the traders' own, and each
  once, oldest first, with how it closed as OANDA records it;
- R is the result in the trade's own risk: a full stop-out is -1 R, and a
  trade with no stop has none;
- a trade marked "counted: no" stays in the file but out of the numbers,
  and is not copied in again;
- the spoken summary gives the count, wins, money and R, how many reached
  the target or the stop, and warns while there are too few to judge by;
- OANDA's trades say who placed them, and are asked for up to its limit;
- "how are my trades going" reaches it, by phrase and through the model;
  the journal is never tidied away.

    python tools/test_trade_journal.py
"""

import contextlib
import csv
import io
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import folder_organizer, gold_trader, oanda, trade_journal, trend_trader  # noqa: E402
from tools import trade_journal as cli  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def trade(number, units, price, stop, target, closed_at, exit_price, result, tag="", instrument="XAU_USD"):
    return {"id": str(number), "instrument": instrument, "state": "CLOSED", "units": units, "price": price,
            "opened": utc(2026, 10, 7, 10, 30), "closed": closed_at, "close_price": exit_price, "result": result,
            "stop": stop, "target": target, "comment": "", "closing": [str(number + 1)], "tag": tag}


class Account:
    """OANDA's closed trades, newest first, as it lists them."""

    def __init__(self):
        self.closed = [
            trade(26, -0.5, 4110.0, 4123.0, 4075.0, utc(2026, 10, 7, 17, 5), 4123.36, -4.99),
            trade(24, 1.3, 4130.0, 4120.0, 4160.0, utc(2026, 10, 7, 15, 0), 4160.0, 29.0, tag=gold_trader.TAG),
            trade(19, -0.1, 4111.25, 4115.217, 4095.341, utc(2026, 10, 7, 10, 38), 4115.25, -0.31),
            trade(9, 0.01, 30000.0, 29000.0, None, utc(2026, 7, 21, 21, 0), 30500.0, 3.85, tag=trend_trader.TAG,
                  instrument="NAS100_USD"),
        ]
        self.asked = []

    def trades(self, tag=None, state="OPEN", count=50):
        self.asked.append((tag, state, count))
        return [dict(item) for item in self.closed if tag is None or item["tag"] == tag]

    def closed_by(self, trade):
        return {"26": "stop", "19": "stop"}.get(trade["id"], "closed")

    def summary(self):
        return {"currency": "GBP"}


account = Account()

# ---- copying ---------------------------------------------------------------------------------------

check(trade_journal.sync(account) == 2 and account.asked[-1] == (None, "CLOSED", trade_journal.LOOK_BACK),
      "the two hand-placed trades are copied, from as far back as OANDA will list")
kept = trade_journal.rows()
check([row["trade"] for row in kept] == ["19", "26"], "only yours, never the traders', oldest first")
first, second = kept
check(first == {"trade": "19", "instrument": "XAU_USD", "side": "sell", "units": "0.1", "opened (UK)": "2026-10-07 11:30",
                "closed (UK)": "2026-10-07 11:38", "entry": "4111.25", "stop": "4115.217", "target": "4095.341",
                "exit": "4115.25", "result": "-0.31", "how": "stop", "R": "-1.01", "counted": "yes"},
      f"the accidental 0.1 sell, as OANDA had it, in UK time ({first})")
check(second["result"] == "-4.99" and second["R"] == "-1.03" and second["how"] == "stop" and second["target"] == "4075",
      "today's 0.5 sell: -4.99, a full stop-out of -1 R and a little slippage past it")
check(trade_journal.sync(account) == 0 and len(trade_journal.rows()) == 2, "each trade only once")

check(trade_journal.r_multiple("buy", 100.0, 90.0, 130.0) == 3.0 and trade_journal.r_multiple("sell", 100.0, 110.0, 70.0) == 3.0
      and trade_journal.r_multiple("buy", 100.0, None, 130.0) is None, "R: a 1:3 target either way is +3 R; no stop, no R")

# ---- the summary ------------------------------------------------------------------------------------

said = trade_journal.summary("GBP")
check("2 finished trades of your own, sir: 0 won, 2 lost, -5.30 GBP overall" in said and "losers -1.0 R" in said
      and "0 reached the target, 2 the stop, and 0 were closed by hand" in said and "28 more" in said,
      f"said: count, wins, money, R, how they closed, and too few to judge yet ({said})")

# The accidental one, marked out by hand in the file.
with open(trade_journal.path(), newline="", encoding="utf-8") as handle:
    marked = list(csv.DictReader(handle))

marked[0]["counted"] = "no"

with open(trade_journal.path(), "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=trade_journal.COLUMNS)
    writer.writeheader()
    writer.writerows(marked)

said = trade_journal.summary("GBP")
check("1 finished trade of your own (1 left out, as you marked them)" in said and "-4.99 GBP" in said
      and trade_journal.sync(account) == 0, "a trade marked 'counted: no' stays out of the numbers, and is not copied back")

account.closed.insert(0, trade(30, 0.2, 4100.0, None, None, utc(2026, 10, 8, 14, 0), 4112.0, 1.8))
trade_journal.sync(account)
said = trade_journal.summary()
check(trade_journal.rows()[-1]["R"] == "" and "1 were closed by hand" in said and "2 finished trades" in said,
      "a trade with no stop is counted in money, not in R, and closed by hand")

# ---- spoken, with the gold trader's record ------------------------------------------------------------

real = (oanda.configured, oanda.Client)
oanda.configured, oanda.Client = (lambda: True), (lambda: account)

try:
    spoken = trade_journal.report()
finally:
    oanda.configured, oanda.Client = real

check(spoken.startswith(trade_journal.summary("GBP")) and "gold trader" in spoken, "spoken: yours, then the gold trader's")


class Down(Account):
    def trades(self, tag=None, state="OPEN", count=50):
        raise oanda.OandaError("OANDA could not be reached (ProxyError)")


oanda.configured, oanda.Client = (lambda: True), (lambda: Down())

try:
    with contextlib.redirect_stdout(io.StringIO()):
        spoken = trade_journal.report()
finally:
    oanda.configured, oanda.Client = real

check("couldn't be reached" in spoken and "2 finished trades" in spoken, "OANDA out of reach: the journal as it is, and saying so")

printed = io.StringIO()

with contextlib.redirect_stdout(printed):
    code = cli.main(["--offline"])

check(code == 0 and "4111.25" in printed.getvalue() and "counted" in printed.getvalue()
      and ", sir" not in printed.getvalue(), "the tool lists the journal and sums it up")

# ---- OANDA's side ------------------------------------------------------------------------------------

raw = {"id": "26", "instrument": "XAU_USD", "initialUnits": "-0.5", "price": "4110.0", "realizedPL": "-4.99",
       "openTime": "2026-10-07T16:40:00.000000000Z", "closeTime": "2026-10-07T17:05:00.000000000Z",
       "averageClosePrice": "4123.36", "stopLossOrder": {"price": "4123.000"}, "closingTransactionIDs": ["27"]}
tagged = dict(raw, clientExtensions={"tag": gold_trader.TAG, "comment": "pullback-1:3"})
check(oanda._trade(raw)["tag"] == "" and oanda._trade(tagged)["tag"] == gold_trader.TAG,
      "OANDA's trades say who placed them: a trade by hand has no tag")


class Asked(oanda.Client):
    def __init__(self):
        self.params = None

    def account_id(self):
        return "101"

    def _get(self, path, params=None):
        self.params = params
        return {"trades": [raw, tagged]}


asked = Asked()
check([item["id"] for item in asked.trades(gold_trader.TAG, state="CLOSED", count=9999)] == ["26"]
      and asked.params == {"state": "CLOSED", "count": 500}, "asked for up to OANDA's limit of 500, and filtered by tag")

# ---- reaching it -------------------------------------------------------------------------------------

commands = (ROOT / "commands.py").read_text(encoding="utf-8")
llm = (ROOT / "llm.py").read_text(encoding="utf-8")
check(re.search(r'"how are my trades going".*?"trading_record"\)', commands, re.DOTALL)
      and 'if intent == "trading_record":\n        return _query(intent, trade_journal.report)' in commands,
      "'how are my trades going' is answered from the journal")
check('"trading_record",' in llm and "Use trading_record when" in llm, "and the model can choose it for other wordings")
check(folder_organizer.is_protected(trade_journal.JOURNAL_NAME), "the journal is never tidied away")

sys.exit(1 if failures else 0)
