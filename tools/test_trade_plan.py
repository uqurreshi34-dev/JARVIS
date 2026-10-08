"""The trade plan (actions/trade_plan.py, trade_plan_panel.py), on candles made for each case.

Checked, in a sandboxed JARVIS folder:

- swings and lines: a swing beyond the candles either side; swings close
  together are one line; a lone swing is not a line; your own lines are
  always lines, for their own market only;
- the stages, for each side: waiting for the break, broken and waiting for
  the retest, retested and waiting for confirmation, confirmed on the last
  candle, and passed; a break that did not hold is no break;
- the figures: entry, stop beyond the retest's wick by the ATR, target short
  of the next line by a share of the ATR, reward : risk, all with the
  arithmetic that gives them; a target too close says skip, as does RSI
  outside the range;
- with no line on a side, that side says so instead of inventing one;
- any market: the one named is the one read, by OANDA's name, its prices to
  OANDA's decimal places and its display name; one not known is asked about;
- reading OANDA: the live price when there is one, the last close when the
  market is shut, and a plain sentence when OANDA cannot be read;
- "shall I buy or sell gold", "should I go long on oil" and "close the gold
  plan" are recognised, and "remind me to buy gold earrings" and the gold
  trader's own questions are not; nothing here can place an order;
- the panel draws the plan and grows to hold it.

    python tools/test_trade_plan.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import folder_organizer, oanda, trade_plan as tp  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


START = datetime(2026, 9, 1, tzinfo=timezone.utc)
GOLD = {"name": "Gold", "instrument": "XAU_USD", "decimals": 2}


def made(rows):
    """Candles from (open, high, low, close) rows, four hours apart."""
    return [(START + timedelta(hours=4 * index), *row) for index, row in enumerate(rows)]


def zigzag(count=100, low=4010.0, high=4090.0, period=10, wick=2.0):
    """A range: up for [period] candles, down for [period], with small wicks."""
    rows, price, step = [], (low + high) / 2, (high - low) / period

    for index in range(count):
        rising = (index // period) % 2 == 0
        opened = price
        price = min(high, price + step) if rising else max(low, price - step)
        rows.append((opened, max(opened, price) + wick, min(opened, price) - wick, price))

    return rows


chosen = tp.settings()
check(os.path.exists(os.path.join(folder, tp.SETTINGS_NAME)) and folder_organizer.is_protected(tp.SETTINGS_NAME),
      "the settings are written the first time, and never tidied away")

# ---- the lines -------------------------------------------------------------------------------

base = zigzag()
range_candles = made(base + [(4050, 4052, 4046, 4050)])
atr = tp.gold_strategy.average_true_range(range_candles)[-1]
lines = tp.levels(range_candles, atr, chosen)
prices = [line["price"] for line in lines]
check(len(lines) == 2 and abs(prices[0] - 4008) < 1 and abs(prices[1] - 4092) < 1
      and all(line["touches"] >= 2 for line in lines),
      f"the range's floor and ceiling are found, each turned at more than once ({prices})")
lone = made(base + [(4050, 4052, 4046, 4050)] * 3 + [(4050, 4200, 4046, 4050)] + [(4050, 4052, 4046, 4050)] * 4)
check(all(line["price"] < 4150 for line in tp.levels(lone, atr, chosen)), "a lone spike is a swing, not a line")
# OANDA gold's real four-hour candles of 8 October 2026: a trader drew resistance at 4,229 through the highs
# of 30 Sep and 2 Oct and support at 4,114 through the lows of 28 Sep and 6 Oct, by eye. The lines must agree.
import csv  # noqa: E402

with open(ROOT / "tools" / "fixtures" / "gold-4h-2026-10-08.csv", encoding="utf-8") as handle:
    real = [(datetime.strptime("2026 " + row["start (UK)"], "%Y %d %b %H:%M").replace(tzinfo=timezone.utc),
             float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
            for row in csv.DictReader(handle)]

drawn = tp.plan(real, real[-1][4], chosen, GOLD)
check(abs(drawn["resistance"] - 4223.5) < 1 and abs(drawn["support"] - 4107.2) < 1,
      f"on real gold: resistance {drawn['resistance']:,.1f} and support {drawn['support']:,.1f}, where a trader drew"
      f" 4,229 and 4,114")
check(all(line["touches"] == 2 and len(line["swings"]) == 2 for line in drawn["levels"]),
      "each line runs through a pair of highs or of lows")
# Two highs on 1 and 6 Oct (4,193 and 4,184) are close together, but price closed far above them on 2 Oct, in
# between: it did not hold there, so they are no line.
check(not any(4180 < line["price"] < 4200 for line in drawn["levels"]),
      "two highs with a close above them in between are no line: price did not hold there")

# Silver the same day: its range floor of 30 Sep and 6 Oct (60.12) broken on 7 Oct, the top at 61.96.
with open(ROOT / "tools" / "fixtures" / "silver-4h-2026-10-08.csv", encoding="utf-8") as handle:
    silver = [(datetime.strptime("2026 " + row["start (UK)"], "%Y %d %b %H:%M").replace(tzinfo=timezone.utc),
               float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
              for row in csv.DictReader(handle)]

argent = tp.plan(silver, silver[-1][4], chosen, {"name": "Silver", "instrument": "XAG_USD", "decimals": 5})
check(abs(argent["support"] - 60.118) < 0.01 and abs(argent["resistance"] - 61.956) < 0.01
      and "has broken below its support at 60.11775" in argent["headline"] and argent["sell"] is None
      and "no line below the price" in argent["headline"],
      f"silver: the broken range floor is still drawn as the support it was, and said so ({argent['headline'][:90]})")

# Resistance as a trader draws it: the most recent two highs close together above the price -- not merely
# the nearest line. Here an older pair sits nearer; the newer pair, further up, is the resistance.
older = {"price": 4150.0, "kind": "high", "touches": 2, "yours": False, "last": 10, "swings": []}
newer = {"price": 4229.0, "kind": "high", "touches": 2, "yours": False, "last": 90, "swings": []}
floor_ = {"price": 4100.0, "kind": "low", "touches": 2, "yours": False, "last": 80, "swings": []}
above_low = {"price": 4140.0, "kind": "low", "touches": 2, "yours": False, "last": 95, "swings": []}
check(tp.chart_lines.latest([older, newer, floor_, above_low], "high") is newer
      and tp.chart_lines.latest([older, newer, floor_, above_low], "low") is above_low,
      "resistance: the most recent pair of highs; support: the most recent pair of lows -- never a pair of lows as"
      " resistance")
check(all(len(line["swings"]) == line["touches"] and all(when > 0 for when, _price in line["swings"]) for line in lines),
      "every line carries the swings that made it, by time, for the chart to ring")
# A shallow wobble mid-range: highs and lows a few dollars apart, never moving an ATR from them.
wobble = base + [(4050 + (2 if index % 4 < 2 else -2), 4053 + (2 if index % 4 < 2 else -2),
                  4047 + (2 if index % 4 < 2 else -2), 4050 + (2 if index % 4 < 2 else -2)) for index in range(40)]
shallow = tp.levels(made(wobble), atr, chosen)
check(not any(4040 < line["price"] < 4060 for line in shallow)
      and any(4040 < line["price"] < 4060 for line in tp.levels(made(wobble), atr, dict(chosen, peak_atr=0.01))),
      "a wobble price never clearly turned from makes no line; without the standing-out rule it would")
mine = dict(chosen, your_levels={"XAU_USD": [4050.0]})
check(any(line["yours"] and line["price"] == 4050.0 for line in tp.plan(range_candles, 4050.0, mine, GOLD)["levels"])
      and not any(line["yours"] for line in tp.plan(range_candles, 4050.0, mine, dict(GOLD, instrument="XAG_USD"))
                  ["levels"]),
      "your own line is a line whatever the chart says, on its own market only")

# ---- the stages, and the figures -------------------------------------------------------------------

plan = tp.plan(range_candles, 4050.0, chosen, GOLD)
check(plan["verdict"] == "wait" and plan["buy"]["stage"] == "break" and plan["sell"]["stage"] == "break"
      and "Gold is between support" in plan["headline"],
      f"inside the range: wait for a break either way ({plan['headline']})")
buy = plan["buy"]
check(abs(buy["stop"] - (buy["line"] - chosen["stop_atr"] * plan["atr"])) < 0.02 and "1 ATR" in buy["stop_working"]
      and buy["entry"] == buy["line"], "before a retest: entry at the line, stop one ATR beyond it")
sell = plan["sell"]
check(sell["target"] < sell["entry"] < sell["stop"], "the sell plan: stop above, target below")

ceiling = prices[1]
broken = base + [(4050, 4062, 4048, 4060), (4060, 4082, 4058, 4080), (4080, ceiling + 10, 4078, ceiling + 8),
                 (ceiling + 8, ceiling + 20, ceiling + 6, ceiling + 18)]
plan = tp.plan(made(broken), ceiling + 18, chosen, GOLD)
check(plan["buy"]["stage"] == "retest" and plan["buy"]["line"] == ceiling and plan["buy"]["steps"][0]["done"]
      and not plan["buy"]["steps"][1]["done"] and "come back to" in plan["headline"],
      "broken above: waiting for price to come back to the line")

retested = broken + [(ceiling + 18, ceiling + 19, ceiling + 1, ceiling + 4)]
plan = tp.plan(made(retested), ceiling + 4, chosen, GOLD)
check(plan["buy"]["stage"] == "confirm" and plan["buy"]["steps"][1]["done"],
      "back at the line on a red candle: retested, waiting for confirmation")

confirmed = retested + [(ceiling + 4, ceiling + 14, ceiling + 2, ceiling + 12)]
plan = tp.plan(made(confirmed), ceiling + 12, chosen, GOLD)
buy = plan["buy"]
wick = ceiling + 1
check(buy["stage"] == "ready" and all(step["done"] for step in buy["steps"]),
      "a green candle closing back above it: confirmed, on the last candle")
check(buy["entry"] == round(ceiling + 12, 2) and abs(buy["stop"] - (wick - plan["atr"])) < 0.02
      and "wick" in buy["stop_working"],
      f"entry at the confirmation's close; stop one ATR under the retest's wick ({buy['stop_working']})")
check(abs(buy["ratio"] - round(buy["reward"] / buy["risk"], 2)) < 0.02 and buy["next_line"] is None
      and abs(buy["target"] - (buy["entry"] + chosen["fallback_reward"] * buy["risk"])) < 0.05,
      f"with no line above: the target is {chosen['fallback_reward']:g} times the risk ({buy['target_working']})")
expected = "buy" if buy["rsi_ok"] else "skip"
check(plan["verdict"] == expected and (buy["rsi_ok"] or "RSI" in plan["headline"]),
      f"the verdict follows RSI on the confirmation ({buy['rsi']:.0f}): {plan['verdict']}")

# A sell confirmed right on top of support: it must not aim through the support at a line far beyond.
crowded = tp.plan(made(confirmed), ceiling + 12, dict(chosen, your_levels={"XAU_USD": [ceiling + 13.0]}), GOLD)
check(crowded["verdict"] != "buy" and crowded["buy"]["no_room"] and "no room" in crowded["buy"]["target_working"]
      and crowded["buy"]["ratio"] <= 0, f"a line right past the entry: no room, never a target beyond it ({crowded['headline']})")
spaced = tp.chart_lines.levels([(START + timedelta(hours=4 * index), price, price + 0.5, price - 0.5, price)
                                for index, price in enumerate([100, 110, 103, 110, 107, 110, 111, 100])], 5.0, 1, 2)
check(all(max(price for _i, price in line["swings"]) - min(price for _i, price in line["swings"]) <= 5.0
          for line in spaced), "every swing in a line lies within the merge distance of every other: lines cannot creep")
near = tp.plan(made(confirmed), ceiling + 12, dict(chosen, your_levels={"XAU_USD": [ceiling + 20.0]}), GOLD)
buffer = chosen["target_buffer_atr"] * near["atr"]
check(near["verdict"] == "skip" and "under" in near["headline"] and not near["buy"]["worth"]
      and abs(near["buy"]["target"] - (ceiling + 20 - buffer)) < 0.02 and "ATR" in near["buy"]["target_working"],
      f"a line just above: target a share of the ATR short of it, too little reward, skip ({near['headline']})")
cool = tp.plan(made(confirmed), ceiling + 12, dict(chosen, rsi_high=99, rsi_low=1), GOLD)
check(cool["buy"]["rsi_ok"] and cool["verdict"] == "buy" and "Buy setup confirmed" in cool["headline"],
      "with RSI in range and room to the target: the rules say buy, with every figure")

later = confirmed + [(ceiling + 12, ceiling + 22, ceiling + 10, ceiling + 20)]
gone = tp.plan(made(later), ceiling + 20, chosen, GOLD)
check(gone["buy"] is None and gone["buy_passed"]["line"] == ceiling and gone["buy_passed"]["when"],
      "a candle later, that setup has been and gone: kept as history, not chased")
ahead = tp.plan(made(later), ceiling + 20, dict(chosen, your_levels={"XAU_USD": [ceiling + 80.0]}), GOLD)
check(ahead["buy"]["stage"] == "break" and ahead["buy"]["line"] == ceiling + 80 and ahead["buy"]["passed"]
      and "came and went" in ahead["headline"],
      f"and the plan moves on to the next line to break ({ahead['headline'][:120]})")

failed = base + [(4050, 4062, 4048, 4060), (4060, ceiling + 6, 4058, ceiling + 4), (ceiling + 4, ceiling + 5, 4070, 4072)]
check(tp.plan(made(failed), 4072, chosen, GOLD)["buy"]["stage"] == "break", "a break that closed back inside is no break")

top = tp.plan(made(broken), ceiling + 18, chosen, GOLD)
check("has broken above its resistance at" in top["headline"] and top["resistance"] == ceiling,
      "above the resistance: said that it has broken it, the line still drawn")

# The same rules on a market priced in single figures: lines, figures and words to its own decimals.
gas_rows = [(o / 1000, h / 1000, l / 1000, c / 1000) for o, h, l, c in zigzag(low=3010, high=3090, wick=2)]
gas = tp.plan(made(gas_rows + [(3.05, 3.052, 3.046, 3.05)]), 3.05, chosen,
              {"name": "Natural Gas", "instrument": "NATGAS_USD", "decimals": 3})
check(gas["verdict"] == "wait" and "Natural Gas is between support 3.008 and resistance 3.092" in gas["headline"]
      and gas["buy"]["stop_working"].count(".") >= 2 and gas["atr"] < 1,
      f"a market priced in single figures reads the same way, to its own decimals ({gas['headline']})")

# Words and chart agree, on every market: each price the plan names is a line on the chart, and a line a plan
# has broken and waits to retest is called the support (or resistance) it now is.
def named_lines_drawn(found):
    drawn = {line["price"] for line in found["levels"]}
    named = [found.get("support"), found.get("resistance")] + [found[side]["line"] for side in ("buy", "sell")
                                                               if found.get(side)]
    in_words = found["verdict"] != "wait" or all(f"{value:,.{found['decimals']}f}" in found["headline"]
                                                 for value in named[:2] if value)
    return all(value in drawn for value in named if value is not None) and in_words


plans = [tp.plan(made(rows), rows_price, chosen, GOLD) for rows, rows_price in
         ((broken, ceiling + 18), (retested, ceiling + 4), (confirmed, ceiling + 12))]
plans.append(tp.plan(real, real[-1][4], chosen, GOLD))
check(all(named_lines_drawn(found) for found in plans),
      "every line the words name is drawn, and the support and resistance named are the ones drawn")
check(plans[0]["resistance"] == plans[0]["buy"]["line"] and plans[0]["buy"]["stage"] == "retest"
      and "has broken above its resistance" in plans[0]["headline"],
      "a resistance broken upward and awaiting its retest is said to be broken, and the plan waits on that line")

# ---- reading OANDA ------------------------------------------------------------------------------


class Oanda:
    MARKETS = {"XAU_USD": ("Gold", 2), "WTICO_USD": ("West Texas Oil", 3)}

    def __init__(self, rows, open_market=True, broken=False, entry_rows=None, entry_broken=False):
        self.rows, self.open_market, self.broken, self.asked = rows, open_market, broken, []
        self.entry_rows, self.entry_broken = entry_rows or rows, entry_broken
        self.now = (entry_rows or rows)[-1][4]

    def instrument(self, name):
        if name not in self.MARKETS:
            raise oanda.OandaError(f"this account cannot trade {name}")

        shown, places = self.MARKETS[name]
        return {"name": name, "display_name": shown, "price_decimals": places}

    def candles(self, name, granularity, count, align_utc=False, mid=False):
        self.asked.append((name, granularity, count, align_utc, mid))

        if self.broken or (granularity == "M15" and self.entry_broken):
            raise oanda.OandaError("OANDA answered 503")

        rows = self.entry_rows if granularity == "M15" else self.rows
        return rows[-count:], [row[4] for row in rows[-count:]]

    def forming(self, name, granularity, align_utc=False, mid=False):
        rows = self.entry_rows if granularity == "M15" else self.rows
        start = rows[-1][0] + (timedelta(minutes=15) if granularity == "M15" else timedelta(hours=4))
        return (start, rows[-1][4], rows[-1][4] + 1, rows[-1][4] - 1, self.now)

    def price(self, name):
        if not self.open_market:
            raise oanda.OandaError(f"no price for {name} just now (the market may be closed)")

        return self.now - 0.5, self.now + 0.5, START


def made15(rows, start=START + timedelta(days=30)):
    """Fifteen-minute candles from (open, high, low, close) rows."""
    return [(start + timedelta(minutes=15 * index), *row) for index, row in enumerate(rows)]


# Fifteen-minute candles under the four-hour floor's line: broken above it, retested, confirmed on the last.
floor = prices[0]
quarter = ([(4050.0 - 0.3 * index, 4051.0 - 0.3 * index, 4049.0 - 0.3 * index, 4050.0 - 0.3 * index)
            for index in range(180)]
           + [(3997.0, 3999.0, 3995.0, 3998.0), (3998.0, floor + 9, 3997.0, floor + 8),
              (floor + 8, floor + 12, floor + 6, floor + 11), (floor + 11, floor + 12, floor + 1, floor + 3),
              (floor + 3, floor + 10, floor + 2, floor + 9)])
shown = []
tp.set_listeners(on_plan=shown.append, on_hide=lambda: shown.append("hidden"))
client = Oanda(range_candles, entry_rows=made15(quarter))
said = tp.show("jarvis shall i buy or sell gold", client)
check(client.asked == [("XAU_USD", "H4", chosen["candles"], False, True),
                      ("XAU_USD", "M15", chosen["entry_candles"], False, True)]
      and shown and shown[-1]["price"] == floor + 9 and shown[-1]["live"] and shown[-1]["symbol"] == "XAU_USD",
      "gold: OANDA's four-hour candles from its own day, then the fifteen-minute ones, at mid prices as OANDA's"
      " chart draws them")
entry = shown[-1]["entry"]
check(entry and entry["timeframe"] == "15-minute" and entry["lines_from"] == "4-hour"
      and [line["price"] for line in entry["levels"]] == [line["price"] for line in shown[-1]["levels"]],
      "the fifteen-minute page works against the four-hour chart's lines")
check(shown[-1]["forming"] and entry["forming"] and shown[-1]["forming"][0] > shown[-1]["candles"][-1][0]
      and entry["forming"][4] == shown[-1]["price"],
      "each page carries the candle still forming, after the last finished one, so the chart reaches now")
check(entry["atr"] < shown[-1]["atr"] and entry["line_atr"] == shown[-1]["atr"],
      f"its stop sized by its own, smaller ATR ({entry['atr']} against {shown[-1]['atr']})")
check(entry["buy"]["stage"] == "ready" and entry["buy"]["line"] == floor and "15-minute" in entry["buy"]["steps"][0]["text"],
      "a break, retest and confirmation on fifteen-minute candles at the four-hour line")
check(f"Gold is {floor + 9:,.0f} on the 4-hour chart" in said and "On the 15-minute chart" in said and "panel" in said
      and len(said) < 600, f"said briefly, both charts; the panel holds the rest ({said})")
check(tp.refresh() and shown[-1]["refreshed"] and len(client.asked) == 4,
      "refreshed while open: read again, and shown without a word")
tp.show("shall i buy or sell gold", Oanda(range_candles, entry_broken=True))
check(shown[-1]["entry"] is None and "503" in shown[-1]["entry_error"] and shown[-1]["verdict"],
      "the fifteen-minute candles unreadable: the four-hour page still stands, the reason kept")
client = Oanda(made([(o / 50, h / 50, l / 50, c / 50) for o, h, l, c in base] + [(81.0, 81.04, 80.92, 81.0)]))
said = tp.show("should i go long on crude oil", client)
check(client.asked[0][0] == "WTICO_USD" and shown[-1]["instrument"] == "West Texas Oil" and shown[-1]["decimals"] == 3
      and said.startswith("West Texas Oil is 81.000"),
      f"any market: oil is read by OANDA's name and shown by its own ({said[:60]})")
tp.show("shall i buy or sell gold", Oanda(range_candles, open_market=False))
check(not shown[-1]["live"] and shown[-1]["price"] == 4050.0, "with the market shut, the last close")
said = tp.show("shall i buy or sell gold", Oanda(range_candles, broken=True))
check("couldn't read the gold chart" in said and "503" in said, f"OANDA unreadable: said plainly ({said})")
said = tp.show("should i sell silver", Oanda(range_candles))
check("cannot trade XAG_USD" in said, f"a market the account does not offer: said ({said})")
said = tp.show("shall i buy or sell", Oanda(range_candles))
check(said.startswith("Which market") and "gold" in said and "natural gas" in said,
      f"no market named: asked which, with what can be read ({said[:80]})")
check(tp.hide() and shown[-1] == "hidden", "and put away")

# ---- what is asked ----------------------------------------------------------------------------------

asked = ("jarvis shall i buy or sell gold", "should i sell gold", "should i go long on gold", "gold plan",
         "what's the plan for gold", "is it a good time to buy gold", "should i buy silver",
         "shall i short natural gas", "copper trade setup", "should i go long on crude oil")
not_asked = ("remind me to buy gold earrings", "how has the gold trader done", "turn on the gold trader",
             "set a price alert for gold", "what is gold at", "close the gold plan", "should i buy or sell")
check(all(tp.wanted(text) for text in asked), "a question about trading a market is recognised")
check(not any(tp.wanted(text) for text in not_asked), "other sentences about it are left to their own routes")
check(tp.which("should i go long on crude oil") == ("crude oil", "WTICO_USD")
      and tp.which("shall i buy brent crude")[1] == "BCO_USD", "the longest name wins: crude oil, brent crude")
check(tp.dismissed("close the gold plan") and tp.dismissed("hide the trade plan")
      and tp.dismissed("close the oil chart") and not tp.dismissed("close my gold trade"),
      "closing the plan, and not a trade")

try:
    import commands  # noqa: E402  (Windows only: it drives the desktop)
except ImportError as error:
    print(f"SKIP routing: the command router needs Windows ({error})")
else:
    trader = commands._fast_path("how has the gold trader done")
    check(commands._fast_path("shall i buy or sell gold")["intent"] == "trade_plan"
          and commands._fast_path("should i go long on oil")["intent"] == "trade_plan"
          and commands._fast_path("close the gold plan")["intent"] == "trade_plan_hide"
          and (trader is None or trader["intent"] != "trade_plan"),
          "routed without a model call; the gold trader's own questions are not")

source = (ROOT / "actions" / "trade_plan.py").read_text(encoding="utf-8")
check("market_order" not in source and "close_trade" not in source and "move_stop" not in source,
      "nothing here can place, move or close a trade")

with open(os.path.join(folder, tp.SETTINGS_NAME), "w", encoding="utf-8") as handle:
    handle.write('{"granularity": "H3", "stop_atr": -1, "your_levels": {"XAU_USD": [4100, "x", true]},'
                 ' "min_reward": "lots", "markets": {"Gold": "XAU_USD", "tin": "not an instrument"}}')

fixed = tp.settings()
check(fixed["granularity"] == "H4" and fixed["stop_atr"] == 1.0 and fixed["your_levels"] == {"XAU_USD": [4100.0]}
      and fixed["min_reward"] == 2.0 and fixed["markets"] == {"gold": "XAU_USD"},
      "nonsense in the settings falls back to the defaults; a market needs a real OANDA name")
os.remove(os.path.join(folder, tp.SETTINGS_NAME))

# ---- the panel ------------------------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from trade_plan_panel import TradePlanPanel  # noqa: E402

panel = TradePlanPanel()

for name, found in (("wait", tp.plan(range_candles, 4050.0, chosen, GOLD)),
                    ("ready", tp.plan(made(confirmed), ceiling + 12, chosen, GOLD)), ("gas", gas)):
    panel._on_plan(found)
    image = panel.grab()
    check(not image.isNull() and panel._pages[0].height() > panel.height() and not panel._tabs[1].isVisible(),
          f"the {name} plan draws, scrollable; no second page without entry candles")

    if os.environ.get("TRADE_PLAN_SHOTS"):
        image.save(os.path.join(os.environ["TRADE_PLAN_SHOTS"], f"plan-{name}.png"))

check("NATURAL GAS" in panel._title.text() and "4-HOUR" in panel._title.text() and "3.050" in panel._subtitle.text()
      and "next close" in panel._subtitle.text(), "titled with the market, the timeframe, its price and the next close")

both = tp.read("shall i buy or sell gold", Oanda(range_candles, entry_rows=made15(quarter)))
panel._on_plan(both)
check(panel._showing == 0 and panel._tabs[1].isVisible() and panel._tabs[1].text() == "15-MINUTE"
      and panel._refresh.isActive(), "with entry candles: two pages, the four-hour first, and a fresh reading booked")
panel._turn_to(1)
check(panel._scroll.widget() is panel._pages[1] and "15-MINUTE" in panel._title.text() and panel._tabs[1].isChecked(),
      "the second page: the fifteen-minute plan")
panel._scroll.verticalScrollBar().setValue(120)
panel._on_plan(dict(both, refreshed=True))
check(panel._showing == 1 and panel._scroll.verticalScrollBar().value() == 120,
      "a fresh reading keeps the page and the place scrolled to")
image = panel.grab()
check(not image.isNull(), "the fifteen-minute page draws")

if os.environ.get("TRADE_PLAN_SHOTS"):
    image.save(os.path.join(os.environ["TRADE_PLAN_SHOTS"], "plan-15m.png"))

import contextlib  # noqa: E402
import io  # noqa: E402

from tools import trade_plan as cli  # noqa: E402

printed = io.StringIO()

with contextlib.redirect_stdout(printed):
    cli.report(both)

cli_source = (ROOT / "tools" / "trade_plan.py").read_text(encoding="utf-8")
check('load_dotenv(ROOT / ".env")' in cli_source, "tools/trade_plan.py reads the OANDA token from .env, as JARVIS does")
check("peaks" in printed.getvalue() and "(support)" in printed.getvalue() and "15-minute:" in printed.getvalue(),
      "tools/trade_plan.py prints every line with the peaks that made it, and both pages' verdicts")
saved = tp.files.root()
cli.save(both)
kept = os.path.join(saved, f"trade-plan-candles-{both['symbol']}.csv")
check(os.path.exists(kept) and folder_organizer.is_protected(os.path.basename(kept))
      and len(open(kept, encoding="utf-8").read().splitlines())
      == len(both["read_candles"]) + 1, "--save keeps every candle the lines came from, to check them against")

panel._on_plan(tp.read("shall i buy or sell gold", Oanda(range_candles, entry_broken=True)))
panel._turn_to(1)
check(panel._pages[1].error and "503" in panel._pages[1].error, "entry candles unreadable: that page says why")
panel._on_hide()
check(not panel._refresh.isActive(), "and put away, it stops reading")

sys.exit(1 if failures else 0)
