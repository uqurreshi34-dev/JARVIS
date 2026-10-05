"""JARVIS's trend trader (actions/trend_trader.py), against a pretend OANDA and a pretend clock.

Runs in a sandboxed JARVIS folder. Checked:

- it is off until switched on, writes its settings the first time, and
  refuses settings it does not understand (an unknown setup, a name OANDA
  would not know), holding risk and size to their ceilings;
- it looks once per newly closed daily candle, and opens nothing on one
  closed long ago (JARVIS started days later);
- a breakout opens one trade, sized by risk, its first stop 2 ATR away at
  OANDA and no target, tagged as its own and carrying its setup;
- once open, the ATR trail moves the stop up behind the best close, only
  ever forward, and a channel exit closes the trade at the market;
- closed trades are logged once and announced, and the gold trader's and
  hand-placed trades are never touched.

    python tools/test_trend_trader.py
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

from actions import trend_strategy, trend_trader  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def days(closes, start=utc(2026, 5, 31, 21, 0)):
    made, previous = [], closes[0]

    for step, close in enumerate(closes):
        made.append((start + timedelta(days=step), previous, max(previous, close) + 20.0, min(previous, close) - 20.0, close))
        previous = close

    return made


class Account:
    """OANDA as the trend trader sees it."""

    def __init__(self):
        self.open, self.closed, self.orders, self.moves, self.closes = [], [], [], [], []
        self.daily = []
        self.bid, self.ask = 30000.0, 30001.0
        self.currency = "GBP"

    def instrument(self, name):
        return {"name": name, "display_name": "US Nas 100", "minimum_units": 0.01, "unit_decimals": 2,
                "price_decimals": 1, "margin_rate": 0.05}

    def candles(self, name="XAU_USD", granularity="M15", count=300):
        assert name == "NAS100_USD" and granularity == "D"
        return self.daily[-count:], [candle[4] + 1.0 for candle in self.daily[-count:]]

    def trades(self, tag=None, state="OPEN"):
        chosen = {"OPEN": self.open, "CLOSED": self.closed, "ALL": self.open + self.closed}[state]
        return [dict(trade) for trade in chosen if tag is None or trade["tag"] == tag]

    def price(self, name="XAU_USD"):
        if name == "GBP_USD":
            return 1.2998, 1.3002, None
        return self.bid, self.ask, None

    def summary(self):
        return {"currency": self.currency, "balance": 100000.0, "margin_available": 100000.0, "open_trades": len(self.open)}

    def market_order(self, units, stop, target, tag, name="XAU_USD", price_decimals=2, comment=""):
        self.orders.append({"units": units, "stop": stop, "target": target, "tag": tag, "name": name})
        trade = {"id": str(500 + len(self.orders)), "instrument": name, "state": "OPEN", "units": units,
                 "price": self.ask if units > 0 else self.bid, "opened": now["at"], "closed": None, "close_price": None,
                 "result": 0.0, "stop": stop, "target": target, "comment": comment, "closing": [], "tag": tag}
        self.open.append(trade)
        return {"id": trade["id"], "units": units, "price": trade["price"], "time": now["at"]}

    def move_stop(self, trade_id, price, decimals=2):
        self.moves.append((trade_id, price))
        next(trade for trade in self.open if trade["id"] == trade_id)["stop"] = price

    def close_trade(self, trade_id):
        self.closes.append(trade_id)
        self.finish(trade_id, 42.0)
        return 42.0

    def finish(self, trade_id, result):
        trade = next(trade for trade in self.open if trade["id"] == trade_id)
        self.open.remove(trade)
        trade.update(state="CLOSED", closed=now["at"], close_price=trade["price"] + result, result=result)
        self.closed.append(trade)

    def closed_by(self, trade):
        return "stop"


now = {"at": utc(2026, 7, 20, 21, 30)}
account = Account()
said = []
trader = trend_trader.TrendTrader(client_factory=lambda: account, clock=lambda: now["at"])
trader.set_listener(said.append)


def settings_file():
    with open(os.path.join(folder, trend_trader.SETTINGS_NAME), encoding="utf-8") as handle:
        return json.load(handle)


# ---- settings --------------------------------------------------------------------------------------

check(trader.tick() == "off" and settings_file()["enabled"] is False and settings_file()["instrument"] == "NAS100_USD"
      and settings_file()["setup"] == "trend-20-atr-long", "off until switched on; settings written the first time")

for bad in ({"setup": "no-such-setup"}, {"instrument": "nasdaq"}, {"risk_percent": "lots"}):
    try:
        trend_trader.change(bad)
        refused = False
    except ValueError:
        refused = True

    check(refused, f"a setting not understood is refused ({bad})")

trend_trader.change({"risk_percent": 50, "units": 500})
check(trend_trader.settings()["risk_percent"] == trend_trader.MOST_RISK_PERCENT
      and trend_trader.settings()["units"] == trend_trader.MOST_UNITS, "risk and size held to their ceilings")
trend_trader.change({"risk_percent": 1, "practice_balance": 400, "units": 0.01})
trend_trader.switch(True)

# ---- a breakout ----------------------------------------------------------------------------------

# Forty flat days, then a close above their high: a breakout on the last daily candle, closed at 21:00 UTC on 20 July.
account.daily = days([30000.0] * 49 + [30300.0])
check(account.daily[-1][0] + timedelta(days=1) == utc(2026, 7, 20, 21, 0), "the made-up daily candles close at 21:00 UTC")
atr = trend_strategy.average_true_range(account.daily, trend_strategy.ATR_LENGTH)[-1]

account.open.append({"id": "9", "instrument": "XAU_USD", "state": "OPEN", "units": 1.3, "price": 4150.0, "opened": now["at"],
                     "closed": None, "close_price": None, "result": 0.0, "stop": 4140.0, "target": 4180.0,
                     "comment": "pullback-1:3 risk=10.00", "closing": [], "tag": "jarvis-gold"})
decided = trader.tick()
order = account.orders[-1] if account.orders else {}
distance = trend_strategy.FIRST_STOP_ATR * atr
expected_units = int(400 * 0.01 * 1.30 / distance * 100 + 1e-9) / 100
check(decided == "buy" and order.get("tag") == trend_trader.TAG and order.get("name") == "NAS100_USD"
      and order.get("target") is None and abs(order["stop"] - (30001.0 - distance)) < 1e-6,
      f"a breakout: a buy at the ask, its first stop 2 ATR ({distance:.1f}) below, no target, tagged as the trend trader's")
check(order.get("units") == max(0.01, expected_units) and "risking" in said[-1] and "Demo account" in said[-1]
      and "US Nas 100" in said[-1], f"sized by 1% of the practice 400 GBP, and said ({said[-1]!r})")
check(account.open[-1]["comment"].startswith("trend-20-atr-long risk="), "the trade carries its setup and first risk at OANDA")
check(trader.tick() == "waiting" and len(account.orders) == 1, "the same day is never looked at twice")
check(all(trade["stop"] == 4140.0 for trade in account.open if trade["tag"] == "jarvis-gold"), "the gold trader's trade is not touched")
account.open = [trade for trade in account.open if trade["tag"] == trend_trader.TAG]

# ---- the trail ------------------------------------------------------------------------------------

mine = account.open[0]
first_stop = mine["stop"]
account.daily.append((account.daily[-1][0] + timedelta(days=1), 30300.0, 30420.0, 30280.0, 30400.0))
now["at"] = utc(2026, 7, 21, 21, 30)
trader.tick()
atr = trend_strategy.average_true_range(account.daily, trend_strategy.ATR_LENGTH)[-1]
trailed = max(30001.0, 30400.0) - trend_strategy.TRAIL_ATR * atr
check(account.moves and abs(account.moves[-1][1] - trailed) < 1e-6 and mine["stop"] > first_stop
      if trailed > first_stop else not account.moves,
      "a day closing higher: the stop follows, 3 ATR behind the best close, when that is above the first stop")
moves = len(account.moves)
account.daily.append((account.daily[-1][0] + timedelta(days=1), 30400.0, 30410.0, 30100.0, 30150.0))
now["at"] = utc(2026, 7, 22, 21, 30)
check(trader.tick() == "holding" and len(account.moves) == moves, "a lower close: the stop never moves back")

# ---- closing, and the log ------------------------------------------------------------------------

account.finish(mine["id"], -9.5)
now["at"] = utc(2026, 7, 23, 9, 0)
account.daily.append((account.daily[-1][0] + timedelta(days=1), 30150.0, 30160.0, 30000.0, 30050.0))
trader.tick()
log = trend_trader.logged()
check(len(log) == 1 and log[0]["trade"] == mine["id"] and log[0]["how"] == "stop" and log[0]["result"] == "-9.50"
      and log[0]["instrument"] == "NAS100_USD" and any("closed at its stop" in line for line in said),
      "a trade closed at OANDA is logged once, as OANDA says it closed, and announced")
trader.tick()
check(len(trend_trader.logged()) == 1, "and only once")

# ---- a stale day ---------------------------------------------------------------------------------

account.daily.append((account.daily[-1][0] + timedelta(days=1), 30050.0, 31000.0, 30040.0, 30990.0))
now["at"] = account.daily[-1][0] + timedelta(days=3)
orders = len(account.orders)
check(trader.tick() == "stale" and len(account.orders) == orders,
      "a breakout on a day closed days ago, as when JARVIS starts late: no trade")

# ---- the channel exit -----------------------------------------------------------------------------

trend_trader.change({"setup": "trend-20-channel"})
trader = trend_trader.TrendTrader(client_factory=lambda: account, clock=lambda: now["at"])
trader.set_listener(said.append)
account.daily = days([30000.0] * 49 + [30300.0])
now["at"] = utc(2026, 7, 20, 21, 30)
check(trader.tick() == "buy", "with channel exits, the same breakout buys")
account.daily = account.daily + days([30300.0 - 40 * step for step in range(1, 12)], start=account.daily[-1][0] + timedelta(days=1))
now["at"] = account.daily[-1][0] + timedelta(days=1, minutes=30)
check(trader.tick() == "closed" and account.closes and any("trend has turned" in line for line in said),
      "and a close below the last 10 days' low closes it at the market, and says so")

source = (ROOT / "actions" / "trend_trader.py").read_text(encoding="utf-8")
import re  # noqa: E402

check(not any(name in source for name in ("import providers", "from providers", "knowledge"))
      and not re.search(r"\bllm\b", source, re.IGNORECASE),
      "no language model is asked anything")

sys.exit(1 if failures else 0)
