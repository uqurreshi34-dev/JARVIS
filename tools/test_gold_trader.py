"""JARVIS's gold trader (actions/gold_trader.py), against a pretend OANDA and a pretend clock.

Runs in a sandboxed JARVIS folder. Checked:

- it is off until switched on, and writes its settings the first time;
- nothing at weekends, outside the hours, or on a day not chosen;
- a fresh candle with a setup opens one trade: the size, the stop $10 and
  target $30 from the price it buys or sells at, attached to the order;
- the same candle is never acted on twice, and a stale one not at all;
- one trade at a time, so many a day, standing down after losses in a row
  (a win or a breakeven between them ends the run);
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

from actions import gold_news, gold_strategy, gold_trader  # noqa: E402


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
        self.currency = "GBP"

    def instrument(self):
        return {"name": "XAU_USD", "display_name": "Gold", "minimum_units": 0.1, "unit_decimals": 1,
                "price_decimals": 3, "margin_rate": 0.05}

    def trades(self, tag=None, state="OPEN"):
        self.asked += 1
        chosen = {"OPEN": self.open, "CLOSED": self.closed, "ALL": self.open + self.closed}[state]
        return [dict(trade) for trade in chosen]

    def price(self, name="XAU_USD"):
        if name == "GBP_USD":
            return 1.2998, 1.3002, None
        if name != "XAU_USD":
            raise gold_trader.oanda.OandaError(f"no {name}")
        return self.bid, self.ask, None

    def summary(self):
        return {"currency": self.currency, "balance": 100000.0, "margin_available": 100000.0, "open_trades": len(self.open)}

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


def fake_signal(index, candles, bands, rsis, squeezes, rules, averages=None, fast=None):
    seen.update(index=index, count=len(candles), rules=rules, averages=averages is not None)
    return decision["side"] if decision.get("setup") in (None, rules.setup) else None


gold_strategy.signal = fake_signal

# The economic calendar: none of the network; nothing due unless a check puts it there.
calendar = {"events": [], "error": None}


def fake_events(url):
    if calendar["error"]:
        raise gold_news.CalendarError(calendar["error"])
    return list(calendar["events"])


gold_news.events = fake_events
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
check(gold_trader.settings()["enabled"] and settings_file()["max_losses_in_a_row"] == 2
      and settings_file()["max_trades_per_day"] is None and settings_file()["risk_percent"] is None,
      "switched on, the rest kept: every setup taken, two losses in a row the only stop, fixed size")
# The daily cap is still there for anyone who wants one; tried here at three.
gold_trader.change({"max_trades_per_day": 3})

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
check(said and said[-1].startswith("Gold, sir, a bounce: bought 1 ounce at 4140.40, stop 4130.40, target 4170.40")
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
      "two losses in a row today: it stands down, and says so")
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

# ---- losses in a row ------------------------------------------------------------------------------

def closed(minutes, result):
    return {"state": "CLOSED", "closed": utc(2026, 10, 5, 10, minutes), "result": result}


check(gold_trader._losses_in_a_row([closed(0, -10), closed(15, 30), closed(30, -10)]) == 1,
      "loss, win, loss: one in a row, so it carries on")
check(gold_trader._losses_in_a_row([closed(0, 30), closed(15, -10), closed(30, -10)]) == 2,
      "win, loss, loss: two in a row")
check(gold_trader._losses_in_a_row([closed(0, -10), closed(15, 0.0), closed(30, -10)]) == 1,
      "a breakeven ends the run")
check(gold_trader._losses_in_a_row([{"state": "OPEN", "closed": None, "result": 0.0}]) == 0, "open trades do not count")

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

# ---- the news --------------------------------------------------------------------------------------

account.open = []
gold_trader.switch(True)
settings = settings_file()
settings.update(days=["Mon", "Tue", "Wed", "Thu", "Fri"], units=1, max_trades_per_day=50, max_losses_in_a_row=50)
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)

cpi = (utc(2026, 10, 14, 12, 30), "USD", "High", "CPI m/m")   # 13:30 UK
calendar["events"] = [cpi, (utc(2026, 10, 14, 9, 0), "EUR", "High", "German ZEW"),
                      (utc(2026, 10, 14, 10, 0), "USD", "Medium", "Business optimism")]
decision["side"] = "buy"
orders = len(account.orders)
fresh(utc(2026, 10, 14, 9, 15, 20))
check(trader.tick() == "buy" and any(line.startswith("Gold today, sir: CPI m/m at 13:30") for line in said),
      "the day's first look names the high-impact US release in the window; the euro and the medium one are not news here")
account.open = []
fresh(utc(2026, 10, 14, 12, 0, 20))       # 13:00 UK: half an hour before
check(trader.tick() == "news" and len(account.orders) == orders + 1, "half an hour before it: no new trade")
fresh(utc(2026, 10, 14, 12, 45, 20))      # 13:45 UK: a quarter of an hour after
check(trader.tick() == "news", "and for half an hour after")
fresh(utc(2026, 10, 14, 13, 0, 20))       # 14:00 UK: clear again
check(trader.tick() == "buy" and sum(line.startswith("Gold today") for line in said) == 1,
      "then clear again, and the day's news said only once")
account.open = []

calendar["error"] = "the calendar answered 503"
fresh(utc(2026, 10, 15, 9, 15, 20))
check(trader.tick() == "no calendar" and any("can't read the economic calendar" in line for line in said),
      "the calendar unreadable: it does not assume the coast is clear, and says so")
fresh(utc(2026, 10, 15, 9, 30, 20))
check(trader.tick() == "no calendar" and sum("can't read the economic calendar" in line for line in said) == 1, "once a day")
calendar["error"] = None

settings = settings_file()
settings["news_filter"] = False
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)
calendar["events"] = [(utc(2026, 10, 15, 9, 45), "USD", "High", "Jobless claims")]
fresh(utc(2026, 10, 15, 9, 45, 20))
check(trader.tick() == "buy", "with the news filter off, the calendar is not consulted")
account.open = []
settings["news_filter"] = True
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)

with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump({"enabled": True, "units": 2, "max_losses_per_day": 2}, handle)
check(gold_trader.settings()["units"] == 2 and settings_file()["news_minutes_before"] == 30
      and settings_file()["units"] == 2 and "max_losses_per_day" not in settings_file(),
      "settings added since the file was written appear in it, retired ones go; nothing set is changed")
settings["units"] = 1
with open(os.path.join(folder, gold_trader.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    json.dump(settings, handle)

moved = []
account.move_stop = lambda trade_id, price, decimals=2: moved.append((trade_id, price))
real_chosen = gold_strategy.CHOSEN
gold_strategy.CHOSEN = gold_strategy.Rules(rsi="not_extreme", trend_filter=True, target=20.0, breakeven=True)
gold_trader.switch(True)

try:
    account.open = [{"id": "200", "state": "OPEN", "units": 1.0, "price": 4150.0, "opened": now["at"], "closed": None,
                     "close_price": None, "result": 0.0, "stop": 4140.0, "target": 4170.0, "comment": "", "closing": []}]
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
finally:
    gold_strategy.CHOSEN = real_chosen

# ---- several setups, ATR stops ----------------------------------------------------------------------

try:
    gold_trader.change({"setups": ["bounce-trail"]})
    refused = False
except ValueError:
    refused = True

check(refused, "a setup whose exits the trader cannot place at OANDA is refused in the settings")

try:
    gold_trader.change({"setups": ["no-such-setup"]})
    refused = False
except ValueError:
    refused = True

check(refused, "and so is a setup the backtest does not know")

gold_trader.change({"setups": ["bounce-1:3-be", "pullback-atr-1:3-be"], "max_trades_per_day": None,
                    "max_losses_in_a_row": None})
decision.update(side="buy", setup="pullback")
account.open, account.bid, account.ask = [], 4100.0, 4100.4
orders = len(account.orders)
fresh(utc(2026, 10, 16, 9, 15, 20))
check(trader.tick() == "buy" and len(account.orders) == orders + 1 and "a pullback" in said[-1],
      "with two setups listed, the second takes a setup the first does not see")
order = account.orders[-1]
check(abs((4100.4 - order["stop"]) - 1.5 * 2.0) < 1e-9 and abs((order["target"] - 4100.4) - 3 * 1.5 * 2.0) < 1e-9,
      f"an ATR stop: 1.5 ATR ($2 here) below, the target three times that above ({order['stop']}, {order['target']})")
check(account.open[-1]["comment"] == "pullback-atr-1:3-be risk=3.00", "the trade carries its setup and risk at OANDA")

moved = []
account.move_stop = lambda trade_id, price, decimals=2: moved.append((trade_id, price))
account.bid, account.ask = 4103.5, 4103.9
fresh(utc(2026, 10, 16, 9, 30, 20))
trader.tick()
check(moved == [(account.open[-1]["id"], 4100.4)], "its own risk, $3, decides breakeven: $3.10 up, the stop goes to the entry")
account.open, decision["setup"] = [], None
gold_trader.change({"setups": ["bounce-1:3-be"]})


source = (ROOT / "actions" / "gold_trader.py").read_text(encoding="utf-8")
check(not any(name in source for name in ("import providers", "from providers", "knowledge", "llm")),
      "no language model is asked anything")

# ---- sizing by risk ---------------------------------------------------------------------------------

gold_trader.change({"risk_percent": 1, "practice_balance": 400})
results = sum(float(row["result"]) for row in gold_trader.logged() if row.get("result"))
units, words = trader._size(account, gold_trader.settings(), 10.0)
expected = int((400 + results) * 0.01 * 1.30 / 10 * 10 + 1e-9) / 10
check(units == expected and words.startswith(", risking ") and words.endswith(" GBP"),
      f"1% of a practice 400 GBP plus results ({400 + results:.2f}), at $1.30 a pound, $10 stop: {units} oz{words}")

gold_trader.change({"practice_balance": None})
check(trader._size(account, gold_trader.settings(), 10.0)[0] == gold_trader.MOST_UNITS,
      "1% of the whole 100,000 demo balance: held to the ceiling")

gold_trader.change({"practice_balance": 20})
check(trader._size(account, gold_trader.settings(), 10.0) == "too small to size"
      and "no trade" in said[-1], "an account too small for even OANDA's smallest trade at that risk: no trade, said why")

account.currency = "USD"
gold_trader.change({"practice_balance": 400})
check(trader._size(account, gold_trader.settings(), 10.0)[0]
      == int((400 + results) * 0.01 / 10 * 10 + 1e-9) / 10, "a dollar account needs no conversion")
account.currency = "JPY"
real_price = account.price
account.price = lambda name="XAU_USD": (149.9, 150.1, None) if name == "USD_JPY" else real_price(name)
check(abs(trader._dollars_per(account, "JPY") - 1 / 150) < 1e-12, "and a currency quoted the other way round is turned over")
account.price, account.currency = real_price, "GBP"

try:
    gold_trader.change({"risk_percent": "lots"})
    refused = False
except ValueError:
    refused = True

check(refused and settings_file()["risk_percent"] == 1, "a setting that would not be understood is refused, and nothing changes")
gold_trader.change({"risk_percent": None, "practice_balance": None, "max_trades_per_day": None, "max_losses_in_a_row": None})
check(gold_trader.settings()["max_losses_in_a_row"] is None and gold_trader._losses_in_a_row([closed(0, -10), closed(15, -10)]) == 2,
      "null: no cap on trades, and never standing down")

from tools import gold_trader as cli  # noqa: E402

check(cli.main(["--set", "max_trades_per_day=null", "risk_percent=1"]) == 0 and settings_file()["risk_percent"] == 1
      and settings_file()["max_trades_per_day"] is None and cli.main(["--set", "nonsense=1"]) == 1,
      "the --set command changes settings, and refuses names that are not settings")

sys.exit(1 if failures else 0)
