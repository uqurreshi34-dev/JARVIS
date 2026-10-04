"""JARVIS's gold trader (actions/gold_trader.py), against a pretend OANDA and a pretend clock.

Runs in a sandboxed JARVIS folder. Checked:

- it is off until switched on, and writes its settings the first time;
- nothing at weekends, outside the hours, or on a day not chosen;
- a fresh candle with a setup opens one trade: the size, the stop $10 and
  target $30 from the price it buys or sells at, attached to the order;
- the same candle is never acted on twice, and a stale one not at all;
- one trade at a time, so many a day, standing down after the losses;
- no trade into a wide spread, nor on a candle OANDA has not finished;
- a closed trade is logged once, as OANDA says it closed, and announced;
- on Friday evening anything open is closed for the weekend;
- sizes are kept between OANDA's minimum and a ceiling no setting passes;
- it uses the strategy's chosen rules on the last finished candle, and
  asks no language model anything.

    python tools/test_gold_trader.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import gold_strategy, gold_trader  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


class Account:
    """OANDA as the trader sees it: a few trades, a price, the candles."""

    def __init__(self):
        self.open, self.closed, self.orders, self.closes, self.asked = [], [], [], [], 0
        self.bid, self.ask = 4140.0, 4140.4
        self.last_candle_start = None
        self.reasons = {}

    def instrument(self):
        return {"name": "XAU_USD", "display_name": "Gold", "minimum_units": 0.1, "unit_decimals": 1,
                "price_decimals": 3, "margin_rate": 0.05}

    def trades(self, tag=None, state="OPEN"):
        self.asked += 1
        chosen = {"OPEN": self.open, "CLOSED": self.closed, "ALL": self.open + self.closed}[state]
        return [dict(trade) for trade in chosen]

    def price(self):
        return self.bid, self.ask, None

    def candles(self, count=300):
        start = self.last_candle_start
        made = [(start - timedelta(minutes=15 * (count - 1 - index)), 4100.0, 4101.0, 4099.0, 4100.5)
                for index in range(count)]
        return made, [candle[4] + 0.4 for candle in made]

    def market_order(self, units, stop, target, tag, price_decimals=2, comment=""):
        self.orders.append({"units": units, "stop": stop, "target": target, "tag": tag, "decimals": price_decimals})
        trade = {"id": str(100 + len(self.orders)), "state": "OPEN", "units": units,
                 "price": self.ask if units > 0 else self.bid, "opened": now["at"], "closed": None,
                 "close_price": None, "result": 0.0, "stop": stop, "target": target, "comment": comment, "closing": []}
        self.open.append(trade)
        return {"id": trade["id"], "units": units, "price": trade["price"], "time": now["at"]}

    def close_trade(self, trade_id):
        self.closes.append(trade_id)
        self.finish(trade_id, -3.5, "closed")
        return -3.5

    def finish(self, trade_id, result, how):
        trade = next(trade for trade in self.open if trade["id"] == trade_id)
        self.open.remove(trade)
        trade.update(state="CLOSED", closed=now["at"], close_price=trade["price"] + result, result=result,
                     closing=["9" + trade_id])
        self.closed.append(trade)
        self.reasons[trade_id] = how

    def move_stop(self, trade_id, price, decimals=2):
        raise AssertionError("no stop should move with these rules")

    def closed_by(self, trade):
        return self.reasons.get(trade["id"], "closed")


now = {"at": utc(2026, 10, 5, 9, 15, 30)}       # Monday 10:15:30 UK (BST), the 09:00 UTC candle just finished
account = Account()
account.last_candle_start = utc(2026, 10, 5, 9, 0)
said = []
decision = {"side": None}
seen = {}


def fake_signal(index, candles, bands, rsis, squeezes, rules, averages=None):
    seen.update(index=index, count=len(candles), rules=rules, averages=averages is not None)
    return decision["side"]


gold_strategy.signal = fake_signal
trader = gold_trader.GoldTrader(client_factory=lambda: account, clock=lambda: now["at"])
trader.set_listener(said.append)


def settings_file():
    with open(os.path.join(folder, gold_trader.SETTINGS_NAME), encoding="utf-8") as handle:
        return json.load(handle)


def fresh(at):
    """Move the clock to [at]; the candle that just finished is the one before it."""
    now["at"] = at
    minutes = at.minute - at.minute % 15
    account.last_candle_start = at.replace(minute=minutes, second=0, microsecond=0) - timedelta(minutes=15)


# ---- off until on --------------------------------------------------------------------------------

check(trader.tick() == "off" and settings_file()["enabled"] is False and settings_file()["units"] == 1,
      "off until switched on; the settings are written the first time, with 1 oz a trade")
gold_trader.switch(True)
check(gold_trader.settings()["enabled"] and settings_file()["max_losses_per_day"] == 2, "switched on, the rest kept")

# ---- when it looks -------------------------------------------------------------------------------

fresh(utc(2026, 10, 3, 9, 15, 30))   # a Saturday
asked = account.asked
check(trader.tick() == "weekend" and account.asked == asked, "a Saturday: nothing, not even a question to OANDA")

fresh(utc(2026, 10, 5, 13, 15, 30))  # Monday 14:15 UK: the candle closed at 14:15, past the window
check(trader.tick() == "outside hours", "after 14:00 UK: outside the hours")

fresh(utc(2026, 10, 5, 9, 15, 30))
check(trader.tick() == "quiet" and seen == {"index": gold_trader.HISTORY - 1, "count": gold_trader.HISTORY,
                                            "rules": gold_strategy.CHOSEN, "averages": True},
      "no setup: quiet -- the chosen rules, judged on the last finished candle, with the trend average")
check(trader.tick() == "waiting", "the same candle is never looked at twice")

fresh(utc(2026, 10, 5, 9, 34, 0))    # the 09:30 candle, looked at four minutes late
check(trader.tick() == "stale", "a candle more than three minutes old is not traded on")

# ---- a trade ---------------------------------------------------------------------------------------

decision["side"] = "buy"
fresh(utc(2026, 10, 5, 9, 45, 20))
check(trader.tick() == "buy" and account.orders[-1] == {"units": 1.0, "stop": 4130.4, "target": 4170.4,
                                                         "tag": gold_trader.TAG, "decimals": 3},
      "a setup: buy 1 oz at the ask, stop $10 below and target $30 above it, attached, tagged as JARVIS's")
check(said and said[-1].startswith("Gold, sir: bought 1 ounce at 4140.40, stop 4130.40, target 4170.40")
      and "Demo account" in said[-1], f"and it says so ({said[-1] if said else None!r})")

fresh(utc(2026, 10, 5, 10, 0, 20))
check(trader.tick() == "in a trade" and len(account.orders) == 1, "one trade at a time")

account.finish("101", 30.0, "target")
fresh(utc(2026, 10, 5, 10, 15, 20))
decision["side"] = "sell"
account.bid, account.ask = 4150.0, 4152.0
check(trader.tick() == "spread too wide" and len(account.orders) == 1, "a $2 spread: no trade")
log = gold_trader.logged()
check(len(log) == 1 and log[0]["trade"] == "101" and log[0]["how"] == "target" and log[0]["result"] == "30.00"
      and log[0]["side"] == "buy" and log[0]["opened (UK)"] == "2026-10-05 10:45" and log[0]["spread at entry"] == "0.40",
      "the closed trade is logged, as OANDA says it closed, in UK time, with the spread it was opened into")
check(any("closed at its target, sir: +30.00" in line for line in said), "and announced")

account.bid, account.ask = 4150.0, 4150.5
fresh(utc(2026, 10, 5, 10, 30, 20))
check(trader.tick() == "sell" and account.orders[-1]["units"] == -1.0 and account.orders[-1]["stop"] == 4160.0
      and account.orders[-1]["target"] == 4120.0, "a sell: at the bid, stop $10 above, target $30 below")
check(len(gold_trader.logged()) == 1, "and the first trade is not logged twice")

account.finish("102", -10.0, "stop")
decision["side"] = "buy"
fresh(utc(2026, 10, 5, 10, 45, 20))
trader.tick()
account.finish("103", -10.0, "stop")
fresh(utc(2026, 10, 5, 11, 0, 20))
check(trader.tick() == "enough trades today", "three trades today: no more")

settings = settings_file()
settings["max_trades_per_day"] = 5
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)
fresh(utc(2026, 10, 5, 11, 15, 20))
check(trader.tick() == "stood down" and sum("Standing down" in line for line in said) == 1,
      "two losses today: it stands down, and says so")
fresh(utc(2026, 10, 5, 11, 30, 20))
check(trader.tick() == "stood down" and sum("Standing down" in line for line in said) == 1, "once")

fresh(utc(2026, 10, 6, 9, 15, 20))   # Tuesday: a new day
account.last_candle_start = utc(2026, 10, 6, 8, 45)   # but OANDA has not finished the 09:00 candle yet
check(trader.tick() == "no fresh candle" and len(account.orders) == 3, "a candle OANDA has not finished: no trade")

# ---- Friday ----------------------------------------------------------------------------------------

fresh(utc(2026, 10, 9, 9, 15, 20))   # Friday morning
check(trader.tick() == "buy", "Friday morning trades as usual")
fresh(utc(2026, 10, 9, 19, 0, 10))   # Friday 20:00 UK
check(trader.tick() == "friday close" and account.closes == ["104"] and not account.open,
      "Friday at 20:00 UK, the open trade is closed for the weekend")
log = gold_trader.logged()
check(log[-1]["trade"] == "104" and log[-1]["how"] == "closed for the weekend" and any("for the weekend" in line for line in said),
      "logged and said as such")

# ---- days and sizes ------------------------------------------------------------------------------

settings = settings_file()
settings.update(days=["Tue", "Wed"], units=50)
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)
fresh(utc(2026, 10, 12, 9, 15, 20))  # a Monday
check(trader.tick() == "not a trading day", "a day not chosen: nothing")
check(gold_trader.settings()["units"] == gold_trader.MOST_UNITS, "50 oz in the settings is held to the ceiling")

settings.update(days=["Mon"], units=0.04)
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)
fresh(utc(2026, 10, 12, 9, 30, 20))
check(trader.tick() == "buy" and account.orders[-1]["units"] == 0.1, "and 0.04 is raised to OANDA's 0.1 minimum")

with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    handle.write("{ not json")
check(gold_trader.settings()["enabled"] is False, "settings that cannot be read: it does not trade")

# ---- the whole -----------------------------------------------------------------------------------

check("4 finished trades, 1 won, +6.50" in gold_trader.status(), f"the status counts the finished trades ({gold_trader.status()!r})")

# ---- breakeven, and exits it cannot place ---------------------------------------------------------

try:
    gold_trader.switch(True)
    switched = True
except ValueError as error:
    switched = "not valid JSON" in str(error)

check(switched, "switching on over unreadable settings refuses, rather than overwrite them")
os.remove(os.path.join(folder, gold_trader.SETTINGS_NAME))
gold_trader.settings()

moved = []
account.move_stop = lambda trade_id, price, decimals=2: moved.append((trade_id, price))
real_chosen = gold_strategy.CHOSEN
gold_strategy.CHOSEN = gold_strategy.Rules(rsi="not_extreme", trend_filter=True, target=20.0, breakeven=True)
gold_trader.switch(True)

try:
    account.open = [dict(account.open[-1], stop=4140.5)] if account.open else []
    entry = account.open[0]["price"]
    account.bid, account.ask = entry + 5.0, entry + 5.4
    fresh(utc(2026, 10, 13, 9, 15, 20))
    trader.tick()
    check(not moved, "breakeven: $5 up of a $10 risk, the stop stays")
    account.bid, account.ask = entry + 10.0, entry + 10.4
    fresh(utc(2026, 10, 13, 9, 30, 20))
    check(trader.tick() == "in a trade" and moved == [(account.open[0]["id"], entry)]
          and "can no longer lose" in said[-1], "1 R up: the stop moves to the entry, at OANDA, and it says so")
    account.open[0]["stop"] = entry
    fresh(utc(2026, 10, 13, 9, 45, 20))
    trader.tick()
    check(len(moved) == 1, "and only once")

    account.open = []
    gold_strategy.CHOSEN = gold_strategy.Rules(rsi="not_extreme", trend_filter=True, exits="trail")
    orders = len(account.orders)
    fresh(utc(2026, 10, 13, 10, 0, 20))
    check(trader.tick() == "exits not supported" and len(account.orders) == orders,
          "rules whose exits the trader cannot place: no trade, and it says why")
finally:
    gold_strategy.CHOSEN = real_chosen
source = (ROOT / "actions" / "gold_trader.py").read_text(encoding="utf-8")
check(not any(name in source for name in ("import providers", "from providers", "knowledge", "llm")),
      "no language model is asked anything")

sys.exit(1 if failures else 0)
