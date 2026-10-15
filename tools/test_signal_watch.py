"""Checks for actions/signal_watch.py and actions/signal_desk.py: a setup confirming on any market is said,
unasked, once; a briefing is kept after each four-hour close.

    python tools/test_signal_watch.py

Without OANDA: the markets are read by a stand-in that returns plans made here. Checked:

- four-hour closes on OANDA's own day, 17:00 New York, through both countries' clock changes, and on
  UTC's hours when set;
- a buy or sell setup on either page is said, with its figures and until when it is live; a skip is not;
  the fifteen-minute page says whether the four-hour chart agrees;
- each setup is said once, the first one's plan opens, everything said is journalled;
- markets shut (a weekend, the daily break) -- a price still quoted but no candle forming: nothing said,
  no briefing; a setup past its next close is never said, as after a restart over the weekend;
- the briefing on a four-hour close draws charts only for markets with a setup;
- signal_markets is in the settings, all seven by default, and none means no watching.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import signal_desk, signal_watch, trade_plan  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


chosen = trade_plan.settings()

# ---- when a four-hour candle closes ------------------------------------------------------------------
# 15 October 2026: New York on daylight time, so OANDA's day starts 21:00 UTC.
check(signal_watch.is_close(utc(2026, 10, 15, 21, 0), chosen) and signal_watch.is_close(utc(2026, 10, 16, 1, 0), chosen)
      and not signal_watch.is_close(utc(2026, 10, 15, 20, 0), chosen)
      and not signal_watch.is_close(utc(2026, 10, 15, 21, 15), chosen),
      "four-hour closes at 21:00, 01:00 ... UTC while New York is on daylight time")
# 27 October: the UK has changed its clocks, New York has not -- still 21:00 UTC.
check(signal_watch.is_close(utc(2026, 10, 27, 21, 0), chosen), "the UK changing its clocks moves nothing")
# 3 November: New York on standard time -- 22:00 UTC.
check(signal_watch.is_close(utc(2026, 11, 3, 22, 0), chosen) and not signal_watch.is_close(utc(2026, 11, 3, 21, 0), chosen),
      "New York changing its clocks moves the closes to 22:00, 02:00 ... UTC")
check(signal_watch.is_close(utc(2026, 10, 15, 20, 0), dict(chosen, utc_candles=True)),
      "on UTC's hours when utc_candles is set")
check(signal_watch.next_look(utc(2026, 10, 15, 20, 52, 10))[1] == utc(2026, 10, 15, 21, 0),
      "the next look follows the next fifteen-minute close")

# ---- plans as trade_plan.read returns them, cut to what the watch reads -------------------------------
CONFIRMED = utc(2026, 10, 15, 17, 0)


def setup(side, line, entry, stop, target, ratio, confirmed_at=CONFIRMED):
    return {"side": side, "line": line, "entry": entry, "stop": stop, "target": target, "ratio": ratio,
            "confirmed_at": confirmed_at, "stage": "ready"}


def page(verdict, granularity="H4", **plans):
    words = trade_plan.GRANULARITY_WORDS[granularity]
    return {"verdict": verdict, "granularity": granularity, "timeframe": words, "headline": verdict,
            "buy": plans.get("buy"), "sell": plans.get("sell")}


FORMING = [0, 1.0, 1.0, 1.0, 1.0, "15 Oct 21:45"]


def market(name, symbol, verdict="wait", entry=None, live=True, decimals=3, forming=FORMING, **plans):
    found = dict(page(verdict, **plans), instrument=name, symbol=symbol, decimals=decimals, live=live, forming=forming,
                 price=100.0, entry=entry, rsi=None, headline=f"{name}: {verdict}", next_close="22:00")
    return found


wti_buy = setup("buy", 95.138, 98.853, 93.797, 114.022, 3.0)
wti = market("West Texas Oil", "WTICO_USD", "buy", buy=wti_buy)
copper_15 = setup("sell", 6.43318, 6.42, 6.46, 6.31, 2.75, utc(2026, 10, 15, 20, 45))
copper = market("Copper", "XCU_USD", "wait", decimals=5,
                entry=page("sell", "M15", sell=copper_15))
skipped = market("Natural Gas", "NATGAS_USD", "skip", buy=setup("buy", 3.262, 3.27, 3.2, 3.3, 0.5))
quiet = market("Gold", "XAU_USD")

said = signal_watch.words(wti, wti, "buy", wti_buy)
check("West Texas Oil: a buy setup has confirmed on the 4-hour chart, at 95.138" in said
      and "In near 98.853, stop 93.797, target 114.022, 3.0 to 1" in said and "until the next 4-hour close, 22:00 UK" in said,
      f"a four-hour setup is said with its figures and until when it is live ({said})")
said = signal_watch.words(copper, copper["entry"], "sell", copper_15)
check("on the 15-minute chart" in said and "22:00 UK" in said and "has not confirmed it yet" in said,
      f"a fifteen-minute setup says when it lapses and that the four-hour chart has not confirmed it ({said})")
check(signal_watch.live(skipped) == [] and signal_watch.live(quiet) == [], "a skip, or nothing, is no setup")

# ---- a look ------------------------------------------------------------------------------------------
spoken, shown, journalled, written = [], [], [], []
signal_watch.set_listeners(on_say=spoken.append, on_show=lambda found, command: shown.append((found["symbol"], command)))


NOW = utc(2026, 10, 15, 20, 45, 45)    # the look after the 20:45 UTC close: WTI live to 21:00, copper too


def look(results, brief=False, now=NOW):
    with patch.object(signal_desk, "scan", lambda markets, client=None: results), \
         patch.object(signal_watch.journal, "alert", journalled.append), \
         patch.object(signal_desk, "write", lambda results, every_chart=True: written.append(every_chart) or ("x", "")):
        return signal_watch.check(brief=brief, now=now)


signal_watch._told.clear()
first = look([("gold", quiet), ("copper", copper), ("crude oil", wti), ("natural gas", skipped)])
check(len(first) == 2 and spoken == first and journalled == first,
      f"both live setups said, and journalled; the skip and the quiet market not ({len(first)})")
check(shown == [("XCU_USD", "the copper plan")] and first[0].endswith("The plan is on the panel."),
      "the first one's plan opens, and that is said")
check(look([("copper", copper), ("crude oil", wti)]) == [] and len(spoken) == 2, "a setup is said once")
check(written == [], "no briefing between four-hour closes")

look([("crude oil", wti)], brief=True)
check(written == [False], "on a four-hour close a briefing is kept, charts only where there is a setup")

shut = market("Gold", "XAU_USD", "buy", live=False, buy=setup("buy", 4100.0, 4110.0, 4090.0, 4170.0, 3.0))
written.clear()
check(look([("gold", shut)], brief=True) == [] and written == [], "no price: nothing said, no briefing")
quoted = market("Gold", "XAU_USD", "buy", forming=None, buy=setup("buy", 4100.0, 4110.0, 4090.0, 4170.0, 3.0))
check(look([("gold", quoted)], brief=True) == [] and written == [],
      "a price still quoted but no candle forming -- the weekend: nothing said, no briefing")

signal_watch._told.clear()
spoken.clear()
weekend = utc(2026, 10, 17, 12, 0, 45)
check(look([("crude oil", wti)], now=weekend) == [] and spoken == [],
      "a setup whose next close has passed is never said -- JARVIS started on Saturday, Friday's setup is not news")

# ---- the briefing: charts only for markets with a setup ----------------------------------------------
drawn = []
with patch.object(signal_desk, "chart_png", lambda found, path: drawn.append(found["symbol"]) or True):
    path, text = signal_desk.write([("gold", quiet), ("crude oil", wti), ("natural gas", skipped)],
                                   now=utc(2026, 10, 15, 21, 1), every_chart=False)

check(drawn == ["WTICO_USD", "NATGAS_USD"] and path.exists() and "2026-10-15-2101-crude-oil.png" in text,
      f"charts for the setup and the skip only, and the briefing kept ({drawn})")
check("Nothing live" not in text and "West Texas Oil BUY" in text, "the briefing leads with the live setup")

# ---- settings ------------------------------------------------------------------------------------------
check(chosen["signal_markets"] == ["gold", "silver", "platinum", "copper", "crude oil", "brent crude", "natural gas"],
      "all seven markets watched by default")
with patch.object(trade_plan, "settings", lambda: dict(chosen, signal_markets=[])), \
     patch.object(signal_watch.oanda, "configured", lambda: True):
    check(signal_watch.start() is False, "signal_markets empty: no watching")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
