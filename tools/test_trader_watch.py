"""The trader board (actions/trader_watch.py, traders_panel.py), against a pretend Hyperliquid.

Checked, in a sandboxed JARVIS folder:

- the leaderboard's best recent performers are examined, small accounts and
  idle ones left out;
- fills are read page by page as Hyperliquid asks, each closing order counted
  once with its fees, and a trade id seen twice counted once;
- the verdicts: consistent (enough trades, money made in every part), too
  few, inconsistent (naming the losing part), one big trade -- and consistent
  records rank first;
- the deepest fall is measured from the running best, in dollars and as a
  share of the account then; open positions carry their leverage and how
  far price is from liquidating them;
- the board is saved and reused until stale, refreshed when it is, and
  shown through the listener, a trader by number too;
- what is said is short and the panel holds the detail;
- "show me the top traders", "show trader three" and "close the traders"
  are recognised, and nothing here can place an order;
- the panel draws the board and a trader, steps between them, and goes back.

    python tools/test_trader_watch.py
"""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import folder_organizer, trader_watch as tw  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


DAY = 86_400_000
NOW = 1_791_400_000_000            # 8 October 2026
START = NOW - 90 * DAY


def window(pnl, volume=1e6):
    return {"pnl": str(pnl), "roi": "0.1", "vlm": str(volume)}


def leader(address, account, month_pnl, volume=1e6, name=None):
    return {"ethAddress": address, "accountValue": str(account), "displayName": name,
            "windowPerformances": [["day", window(0)], ["week", window(0)], ["month", window(month_pnl, volume)],
                                   ["allTime", window(month_pnl)]]}


def closes(results, first_day=1, every_days=2.0, oid=1000):
    """Closing fills, one order each, spread over the period; each order filled in two pieces."""
    fills = []

    for index, result in enumerate(results):
        when = START + int((first_day + index * every_days) * DAY)

        for half in (0, 1):
            fills.append({"coin": "BTC", "dir": "Close Long", "closedPnl": str(result / 2), "fee": "1.0",
                          "time": when + half, "oid": oid + index, "tid": (oid + index) * 10 + half})

    return fills


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Exchange:
    """Hyperliquid's leaderboard and info endpoints, pretended."""

    def __init__(self):
        steady = [300, -100, 250] * 15                    # 45 trades, money in every part
        lucky = [-50] * 40 + [20000]                       # loses for weeks, then one huge win
        self.fills = {
            "0xsteady00000000000000000000000000000001": closes(steady),
            "0xlucky000000000000000000000000000000002": closes(lucky, every_days=2.15),
            "0xlate0000000000000000000000000000000003": closes([-400] * 20 + [900] * 20, every_days=2.2),
            "0xfew00000000000000000000000000000000004": closes([500] * 5, every_days=15),
        }
        self.board = {"leaderboardRows": [
            leader("0xsteady00000000000000000000000000000001", 250000, 9000, name="SteadyEddie"),
            leader("0xlucky000000000000000000000000000000002", 400000, 20000),
            leader("0xlate0000000000000000000000000000000003", 120000, 15000),
            leader("0xfew00000000000000000000000000000000004", 90000, 2500),
            leader("0xsmall000000000000000000000000000000005", 900, 99999),        # too small
            leader("0xidle0000000000000000000000000000000006", 900000, 50000, volume=0),   # not trading
        ]}
        self.asked = []
        self.page = 2000

    def get(self, url, timeout):
        self.asked.append(("get", url))
        return Response(self.board)

    def post(self, url, json, timeout):
        self.asked.append(("post", json["type"]))
        user = json.get("user")

        if json["type"] == "userFillsByTime":
            found = [fill for fill in self.fills.get(user, []) if fill["time"] >= json["startTime"]]
            return Response(found[:self.page])

        if json["type"] == "portfolio":
            pnl = [[START + day * DAY, str(value)] for day, value in ((0, 0), (20, 5000), (40, 1000), (60, 7000),
                                                                      (89, 9000))]
            value = [[START + day * DAY, str(100000 + day * 1000)] for day in range(0, 90, 10)]
            return Response([["allTime", {"accountValueHistory": value, "pnlHistory": pnl, "vlm": "1"}]])

        if json["type"] == "clearinghouseState":
            return Response({"marginSummary": {"accountValue": "250000"}, "assetPositions": [
                {"type": "oneWay", "position": {"coin": "ETH", "szi": "-10", "entryPx": "2500", "positionValue": "24000",
                                                "unrealizedPnl": "1000", "liquidationPx": "2640",
                                                "leverage": {"type": "cross", "value": 10}}}]})

        raise AssertionError(json)


exchange = Exchange()
client = tw.Hyperliquid(session=exchange, pause=0)

# ---- choosing and reading --------------------------------------------------------------------

rows = client.leaderboard()
chosen = tw.settings()
picked = tw.candidates(rows, chosen)
check([row["address"][:6] for row in picked] == ["0xlucky"[:6], "0xlate"[:6], "0xstea", "0xfew0"]
      or {row["address"] for row in picked} == set(exchange.fills),
      "the best recent performers are examined; small and idle accounts left out")
check(set(row["address"] for row in picked) == set(exchange.fills), "exactly the four trading accounts big enough")

exchange.page = 30
paged = client.fills("0xsteady00000000000000000000000000000001", START)
check(len(paged) == 90 and len({fill["tid"] for fill in paged}) == 90,
      "fills are read page by page, every one once (90 fills in pages of 30)")
exchange.page = 2000

trades = tw.trades_from(paged)
check(len(trades) == 45 and abs(trades[0]["pnl"] - 300) < 1e-9 and abs(trades[0]["fee"] - 2.0) < 1e-9,
      "an order filled in two pieces is one trade, its fees added")

# ---- verdicts --------------------------------------------------------------------------------

board = tw.refresh(client, now=NOW / 1000)
by_address = {trader["address"]: trader for trader in board["traders"]}
steady = by_address["0xsteady00000000000000000000000000000001"]
check(steady["verdict"] == "consistent" and steady["trades"] == 45 and all(value > 0 for value in steady["parts"]),
      f"steady: consistent, money made in every part ({steady['reason']})")
check(abs(steady["win_rate"] - 30 / 45) < 1e-9 and abs(steady["gross_won"] - 30 * 273) < 1e-6
      and abs(steady["profit_factor"] - steady["gross_won"] / steady["gross_lost"]) < 1e-9,
      "win rate and profit factor are worked out from the trades, after fees")
lucky = by_address["0xlucky000000000000000000000000000000002"]
check(lucky["verdict"] == "inconsistent" and "part" in lucky["reason"], f"lucky: inconsistent ({lucky['reason']})")
late = by_address["0xlate0000000000000000000000000000000003"]
check(late["verdict"] == "inconsistent" and "1" in late["reason"], f"late: lost money in its first part ({late['reason']})")
few = by_address["0xfew00000000000000000000000000000000004"]
check(few["verdict"] == "too few" and "5 closed trades" in few["reason"], "five trades: too few to judge")
check(board["traders"][0]["address"] == steady["address"] and board["traders"][-1]["verdict"] == "too few",
      "consistent records rank first")

one = tw.judge([{"time": START + day * DAY, "pnl": pnl, "fee": 0.0, "coin": "SOL"}
                for day, pnl in [(day, 10.0) for day in range(1, 89, 3)] + [(45, 5000.0), (80, 20.0)]],
               START, NOW, 3, 30)
check(one["verdict"] == "one big trade" and "%" in one["reason"], f"most of the profit from one trade: flagged ({one['reason']})")

# ---- the fall, and positions --------------------------------------------------------------------

dip, share = tw.deepest_dip([(1, 0.0), (2, 5000.0), (3, 1000.0), (4, 7000.0)], [(1, 100000.0), (2, 120000.0)])
check(dip == 4000.0 and abs(share - 4000 / 120000) < 1e-9, "the deepest fall: 5,000 to 1,000, as a share of the account then")
check(steady["dip"] == 4000.0, "and read from the account's own profit history")
position = steady["positions"][0]
check(position["side"] == "short" and position["leverage"] == 10 and abs(position["mark"] - 2400) < 1e-9
      and abs(position["to_liquidation"] - 0.1) < 1e-9, "open positions: side, leverage, 10% from liquidation")

# ---- saved, shown, said -------------------------------------------------------------------------

check(tw.cached()["traders"][0]["address"] == steady["address"] and os.path.exists(os.path.join(folder, tw.CACHE_NAME)),
      "the board is saved")
shown, announced = [], []
tw.set_listeners(on_board=shown.append, on_hide=lambda: shown.append("hidden"), on_say=announced.append)
said = tw.show(client=client, wait=True)
check(shown and shown[-1]["focus"] is None and "1 made money in every part" in said and "SteadyEddie" in said,
      f"shown through the listener, and said briefly ({said})")
calls = len(exchange.asked)
said = tw.show(focus=1, client=client)
check(shown[-1]["focus"] == 1 and len(exchange.asked) == calls and "Trader 1, SteadyEddie" in said,
      "a trader by number, from the saved board while it is fresh")
check("There are 4 traders" in tw.show(focus=9, client=client), "a number past the end is said, not shown")
old = tw.cached()
old["updated"] -= 7 * 3_600_000
tw._write(os.path.join(folder, tw.CACHE_NAME), old)
said = tw.show(client=client)
check("Fresh figures are on their way" in said and shown[-1]["updated"] == old["updated"],
      "a stale board is shown at once, and read again in the background")
for _ in range(100):
    if not tw._refreshing.is_set():
        break
    __import__("time").sleep(0.05)
check(len(exchange.asked) > calls and shown[-1]["updated"] > old["updated"] and announced
      and "made money in every part" in announced[-1], "then the fresh board replaces it, and is announced")
os.remove(os.path.join(folder, tw.CACHE_NAME))
said = tw.show(client=client)
check("opens when it's done" in said, "with no board yet: said that it is being read, not left in silence")
for _ in range(100):
    if not tw._refreshing.is_set():
        break
    __import__("time").sleep(0.05)
check(tw.cached() is not None and shown[-1]["focus"] is None, "and opened when it lands")
check(tw.hide() and shown[-1] == "hidden", "and put away")

# ---- what is asked --------------------------------------------------------------------------------

check(tw.wanted("show me the top traders") and tw.wanted("who are the best traders on hyperliquid")
      and tw.wanted("show trader three") and not tw.wanted("what time is it"), "asking for the board is recognised")
check(tw.which("show trader three") == 3 and tw.which("open trader number 12") == 12 and tw.which("top traders") is None,
      "a trader by number, in words or digits")
check(tw.dismissed("close the traders") and tw.dismissed("hide the trader board") and not tw.dismissed("show the traders"),
      "and putting it away")
source = (ROOT / "actions" / "trader_watch.py").read_text(encoding="utf-8")
check(not any(word in source for word in ("order(", "exchange\"", "\"action\"", "signature", "private_key")),
      "nothing here can place an order: read-only endpoints only")
check(folder_organizer.is_protected(tw.SETTINGS_NAME) and folder_organizer.is_protected(tw.CACHE_NAME),
      "its files are never tidied away")

# ---- the panel ----------------------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from traders_panel import TradersPanel  # noqa: E402

panel = TradersPanel()
panel._on_board(dict(tw.cached(), focus=None))
check(panel._index is None and panel._scroll.widget() is panel._list and not panel._back.isEnabled(),
      "the panel opens on the board")
panel._open(0)
check(panel._index == 0 and panel._scroll.widget() is panel._detail and panel._back.isEnabled()
      and not panel._previous.isEnabled(), "a trader opens, with Back")
panel._step(1)
check(panel._index == 1 and panel._previous.isEnabled(), "and the next one is a step away")
panel._to_board()
check(panel._index is None and panel._list.selected == 1, "Back returns to the board, the trader still marked")

shot = os.environ.get("TRADER_PANEL_SHOTS")

for name, focus in (("board", None), ("trader", 1)):
    panel._on_board(dict(tw.cached(), focus=focus))
    image = panel.grab()
    check(not image.isNull() and image.width() > 0, f"the {name} draws")

    if shot:
        image.save(os.path.join(shot, f"{name}.png"))
        panel._detail.grab().save(os.path.join(shot, f"{name}-content.png")) if focus else None

sys.exit(1 if failures else 0)
