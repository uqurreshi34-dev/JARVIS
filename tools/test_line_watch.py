"""Checks for actions/line_watch.py: support and resistance reached, said once, and the plan opened.

Checked, in a sandboxed JARVIS folder and without OANDA:
    what a candle at a line did -- bounce, pushed back, through, testing -- judged as the backtest does
    the stop beyond the wick, with the plan's room
    what is said, each way, and that a line is said once, again for a bounce, and again only once left
    only the markets in alert_markets are read; a candle already judged is not judged again
    the panel opens with the alert at the top
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import line_watch, trade_plan  # noqa: E402

failures = 0


def check(condition, message):
    global failures

    print(("PASS " if condition else "FAIL ") + message)

    if not condition:
        failures += 1


S, R = 4107.193, 4223.472


def found(candle, symbol="XAU_USD", name="Gold", start=1_000):
    """A plan as trade_plan.read returns it, cut to what the line watch reads: the lines, the four-hour ATR
    (30: a touch is within 3), and the last finished fifteen-minute candle (ATR 8: the stop's room)."""
    return {"instrument": name, "symbol": symbol, "decimals": 3, "atr": 30.0, "timeframe": "4-hour",
            "entry_timeframe": "15-minute", "support": S, "resistance": R, "live": True,
            "settings": {"stop_atr": 1.0},
            "entry": {"atr": 8.0, "candles": [[start, *candle, "09 Oct 17:15"]]}}


# Your candle of 8 October 16:15 UTC at the line: under it, back above, the upper wick the longer.
pushed = (S - 2.0, S + 18.0, S - 5.0, S + 4.0)
won = (S + 1.0, S + 6.0, S - 8.0, S + 5.0)
under = (S + 2.0, S + 3.0, S - 12.0, S - 10.0)
sitting = (S + 1.0, S + 2.5, S - 1.0, S + 0.5)
away = (4180.0, 4185.0, 4178.0, 4183.0)
capped = (R + 1.0, R + 9.0, R - 2.0, R - 4.0)

reached = {name: line_watch.hit(found(candle)) for name, candle in
           (("pushed", pushed), ("won", won), ("under", under), ("sitting", sitting), ("away", away),
            ("capped", capped))}
check(reached["pushed"]["signal"] == "pushed back" and reached["won"]["signal"] == "bounce"
      and reached["under"]["signal"] == "through" and reached["sitting"]["signal"] == "testing"
      and reached["away"] is None,
      "at support: the long upper wick pushed back, the long lower one a bounce, a close under it through,"
      " on it testing; nowhere near, nothing")
check(reached["capped"]["kind"] == "resistance" and reached["capped"]["signal"] == "bounce",
      "at resistance, the mirror: closing red back below it, the upper wick the longer, is the bounce")
check(abs(reached["won"]["stop"] - (S - 8.0 - 8.0)) < 1e-9 and abs(reached["capped"]["stop"] - (R + 9.0 + 8.0)) < 1e-9,
      "the stop sits beyond the wick by the plan's stop_atr of the fifteen-minute ATR: room for the noise")

said = {name: line_watch.words(found(candle), reached[name]) for name, candle in
        (("pushed", pushed), ("won", won), ("under", under), ("sitting", sitting))}
check(said["pushed"].startswith("Gold has reached support at 4,107, sir.") and "sellers pushed back, so wait"
      in said["pushed"] and "lower wick the longer" in said["pushed"],
      f"pushed back: wait, and what a bounce would look like ({said['pushed'][:120]})")
check("buyers took it back" in said["won"] and "stop at 4,091" in said["won"],
      f"a bounce: said, with its stop ({said['won'][:160]})")
check("4-hour close below 4,107" in said["under"] and "retest" in said["under"],
      "through the line: the four-hour close decides a break")
check("Wait for a 15-minute candle to close back above 4,107" in said["sitting"]
      and all(sentence.endswith("The plan is on the panel.") for sentence in said.values()),
      "on the line: what to wait for each way; every one points to the panel")

# Said once a line, again for a bounce, and again only once price has left it.
line_watch._said.clear()
first = line_watch._armed("XAU_USD", reached["pushed"], found(pushed))
again = line_watch._armed("XAU_USD", reached["sitting"], found(sitting))
bounce = line_watch._armed("XAU_USD", reached["won"], found(won))
bounce_again = line_watch._armed("XAU_USD", reached["won"], found(won))
line_watch._armed("XAU_USD", None, found(away))
back = line_watch._armed("XAU_USD", reached["sitting"], found(sitting))
check([first, again, bounce, bounce_again, back] == [True, False, True, False, True],
      "a line is said when reached, once more for a bounce, and again only after price has left it")

# The background look: only the markets named, each candle once, said and shown.
spoken, shown, asked_for = [], [], []
plans = {"gold": found(pushed, start=2_000), "silver": found(away, "XAG_USD", "Silver", 2_000)}


def reading(command, client=None):
    asked_for.append(command)
    return plans[command]


real_read = trade_plan.read
trade_plan.read = reading
line_watch._said.clear()
line_watch._last_seen.clear()
line_watch.set_listeners(on_say=spoken.append, on_show=lambda plan, command: shown.append((plan, command)))

try:
    first_look = line_watch.check()
    second_look = line_watch.check()
finally:
    trade_plan.read = real_read
    line_watch.set_listeners()

check(sorted(set(asked_for)) == ["gold", "silver"] and len(first_look) == 1 and second_look == []
      and spoken == first_look and shown[0][1] == "gold" and shown[0][0]["alert"] == first_look[0],
      "gold and silver are read; gold at support is said and its plan opened with the alert; nothing twice")

path = trade_plan._path(trade_plan.SETTINGS_NAME)
with open(path, "w", encoding="utf-8") as handle:
    handle.write('{"alert_markets": ["Gold", "platinum", "tin", 7]}')
check(trade_plan.settings()["alert_markets"] == ["gold", "platinum"],
      "alert_markets: the markets named, by the names trade-plan.json knows; anything else left out")
os.remove(path)
check(trade_plan.settings()["alert_markets"] == ["gold", "silver"], "gold and silver by default, platinum not")

check(line_watch.next_look(datetime(2026, 10, 9, 16, 7, 3, tzinfo=timezone.utc))
      == datetime(2026, 10, 9, 16, 15, 20, tzinfo=timezone.utc),
      "the next look is just after the next fifteen-minute close")

# The panel: the alert first, in its own box, above the verdict.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication  # noqa: E402

    app = QApplication.instance() or QApplication([])
    from trade_plan_panel import TradePlanPanel  # noqa: E402
except Exception as error:  # noqa: BLE001
    print(f"SKIP the panel (could not load Qt: {error})")
else:
    import csv  # noqa: E402

    with open(ROOT / "tools" / "fixtures" / "gold-4h-2026-10-08.csv", encoding="utf-8") as handle:
        candles = [(datetime.strptime("2026 " + row["start (UK)"], "%Y %d %b %H:%M").replace(tzinfo=timezone.utc),
                    float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
                   for row in csv.DictReader(handle)]

    plan = trade_plan.plan(candles, candles[-1][4], trade_plan.settings(),
                           {"name": "Gold", "instrument": "XAU_USD", "decimals": 3})
    panel = TradePlanPanel()
    panel._on_plan(plan)
    panel._on_plan(plan)          # once shown, at the width it keeps
    without = panel._pages[0].height()
    panel._on_plan(dict(plan, alert=said["pushed"]))
    check(not panel.grab().isNull() and panel._pages[0].height() > without,
          "the plan opened by the watch carries the alert at its top, in a box of its own")

sys.exit(1 if failures else 0)
