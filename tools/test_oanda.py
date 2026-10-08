"""OANDA's API (actions/oanda.py) and the account check (tools/oanda_check.py), against a pretend OANDA.

No network and no real token. Checked:

- only the practice server is ever asked, and live trading is not reachable;
- the token goes in the Authorization header and nowhere else, and the
  check never prints it;
- without OANDA_ACCOUNT_ID the first account the token sees is used;
- a refused token, an error and an unreachable server are said in words;
- OANDA's own gold terms, the price and the finished candles are read
  as OANDA sends them (times to the nanosecond, the forming candle left out);
- the check reports all of it, and whether the candles are fresh.

    python tools/test_oanda.py
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
from tools import oanda_check  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


TOKEN = "pretend-token-0123456789"


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def candle(time, close, complete=True):
    side = {"o": f"{close - 1:.2f}", "h": f"{close + 1:.2f}", "l": f"{close - 2:.2f}", "c": f"{close:.2f}"}
    ask = {key: f"{float(value) + 0.4:.2f}" for key, value in side.items()}
    mid = {key: f"{float(value) + 0.2:.2f}" for key, value in side.items()}
    return {"time": time, "complete": complete, "volume": 10, "bid": side, "ask": ask, "mid": mid}


class Oanda:
    """OANDA's practice server as it answers, with a log of what was asked."""

    def __init__(self, status=200):
        self.asked, self.status = [], status

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append((url, dict(params or {}), dict(headers or {})))

        if self.status != 200:
            return Answer(self.status, {"errorMessage": "Insufficient authorization"})

        path = url.split(".com", 1)[1]

        if path == "/v3/accounts":
            return Answer(200, {"accounts": [{"id": "101-004-1234567-001"}, {"id": "101-004-1234567-002"}]})
        if path.endswith("/summary"):
            return Answer(200, {"account": {"currency": "GBP", "balance": "10000.0000", "marginAvailable": "10000",
                                            "openTradeCount": 0, "pl": "13.2600"}})
        if path.endswith("/instruments"):
            return Answer(200, {"instruments": [{"name": "XAU_USD", "displayName": "Gold", "minimumTradeSize": "0.1",
                                                 "tradeUnitsPrecision": 1, "displayPrecision": 3,
                                                 "marginRate": "0.05"}]})
        if path.endswith("/pricing"):
            return Answer(200, {"prices": [{"time": "2026-10-05T11:02:03.123456789Z",
                                            "bids": [{"price": "4140.28"}], "asks": [{"price": "4140.68"}]}]})
        if path.endswith("/trades"):
            return Answer(200, {"trades": [
                {"id": "7", "state": "CLOSED", "initialUnits": "1.0", "price": "4140.400", "openTime": "2026-10-05T09:45:01Z",
                 "closeTime": "2026-10-05T11:02:00Z", "averageClosePrice": "4170.400", "realizedPL": "22.3456",
                 "clientExtensions": {"tag": "jarvis-gold"}, "closingTransactionIDs": ["41"]},
                {"id": "8", "state": "OPEN", "initialUnits": "-2.0", "price": "4150.000", "openTime": "2026-10-05T12:00:00Z",
                 "stopLossOrder": {"price": "4160.000"}, "takeProfitOrder": {"price": "4120.000"}},
            ]})
        if path.endswith("/transactions/41"):
            return Answer(200, {"transaction": {"id": "41", "type": "ORDER_FILL", "reason": "TAKE_PROFIT_ORDER"}})
        if "/candles" in path:
            return Answer(200, {"candles": [candle("2026-10-05T10:30:00.000000000Z", 4138.0),
                                            candle("2026-10-05T10:45:00.000000000Z", 4140.0),
                                            candle("2026-10-05T11:00:00.000000000Z", 4141.0, complete=False)]})

        return Answer(404, {"errorMessage": "no such path"})


# ---- the client ----------------------------------------------------------------------------------

source = (ROOT / "actions" / "oanda.py").read_text(encoding="utf-8")
check('PRACTICE_HOST = "https://api-fxpractice.oanda.com"' in source and "fxtrade" not in source,
      "only OANDA's practice server is known: a live account cannot be reached through this module")

server = Oanda()
client = oanda.Client(token=TOKEN, account_id="", session=server)
check(client.account_id() == "101-004-1234567-001", "without OANDA_ACCOUNT_ID, the first account the token sees")
check(all(url.startswith(oanda.PRACTICE_HOST) for url, _params, _headers in server.asked)
      and server.asked[0][2].get("Authorization") == f"Bearer {TOKEN}"
      and all(TOKEN not in url and TOKEN not in str(params) for url, params, _headers in server.asked),
      "every request goes to the practice server, the token only in the Authorization header")

check(client.summary() == {"currency": "GBP", "balance": 10000.0, "margin_available": 10000.0, "open_trades": 0,
                           "realized": 13.26},
      "the account's currency, balance, open trades, and what closed trades have made on it")
check(client.instrument() == {"name": "XAU_USD", "display_name": "Gold", "minimum_units": 0.1, "unit_decimals": 1,
                              "price_decimals": 3, "margin_rate": 0.05}, "OANDA's own terms for gold, as it sends them")

bid, ask, when = client.price()
check((bid, ask) == (4140.28, 4140.68) and when == datetime(2026, 10, 5, 11, 2, 3, 123456, tzinfo=timezone.utc),
      "the price now, its time read to the microsecond from OANDA's nanoseconds")

made, ask_closes = client.candles(count=5)
check(len(made) == 2 and made[-1] == (datetime(2026, 10, 5, 10, 45, tzinfo=timezone.utc), 4139.0, 4141.0, 4138.0, 4140.0)
      and ask_closes == [4138.4, 4140.4],
      "finished candles only, on the bid, with the ask's close alongside; the forming one left out")
check(server.asked[-1][1] == {"granularity": "M15", "count": 6, "price": "BA"}, "asked for bid and ask, 15-minute candles")
made, _asks = client.candles(granularity="H4", count=5, mid=True)
check(made[-1][4] == 4140.2 and server.asked[-1][1]["price"] == "MBA",
      "at mid prices when asked: halfway between bid and ask, as OANDA's own chart draws them")
forming = client.forming(granularity="H4", mid=True)
check(forming is None or forming[4] == 4141.2, "and the forming candle the same way")

try:
    oanda.Client(token=TOKEN, account_id="x", session=Oanda(status=401)).summary()
    said = ""
except oanda.OandaError as error:
    said = str(error)

check("refused the token" in said and TOKEN not in said, f"a refused token is said plainly ({said!r})")


class Unreachable:
    def get(self, *args, **kwargs):
        raise ConnectionError("down")


try:
    oanda.Client(token=TOKEN, account_id="x", session=Unreachable()).summary()
    said = ""
except oanda.OandaError as error:
    said = str(error)

check("could not be reached" in said, "an unreachable server is said plainly")

try:
    oanda.Client(token="", session=Oanda())
    said = ""
except oanda.OandaError as error:
    said = str(error)

check("OANDA_API_TOKEN" in said, "no token: says which setting is missing")

# ---- a year of history, a page at a time ------------------------------------------------------------


class Pages(Oanda):
    """Two pages of candles, then nothing new: as OANDA answers a long history."""

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append((url, dict(params or {}), dict(headers or {})))
        since = oanda.parse_time(params["from"])
        # OANDA starts from the first candle at or after "from", on the quarter hour.
        aligned = since.replace(minute=since.minute - since.minute % 15, second=0, microsecond=0)
        first = aligned if aligned == since else aligned + timedelta(minutes=15)
        times = [first + timedelta(minutes=15 * step) for step in range(3)]
        times = [moment for moment in times if moment < datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)]
        return Answer(200, {"candles": [candle(moment.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), 4100.0 + moment.minute)
                                        for moment in times]})


pages = Pages()
made, asks = oanda.Client(token=TOKEN, account_id="a", session=pages).history(
    datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 5, 10, 45, tzinfo=timezone.utc), page=3)
check([moment.minute for moment, *_ in made] == [0, 15, 30] and len(asks) == 3,
      "a history stops at its end, every candle once")
made, asks = oanda.Client(token=TOKEN, account_id="a", session=pages).history(
    datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc), page=3)
check([moment.strftime("%H:%M") for moment, *_ in made] == ["10:00", "10:15", "10:30", "10:45"]
      and pages.asked[-1][1]["count"] == 3 and pages.asked[-1][1]["price"] == "BA",
      "and pages on until OANDA has nothing newer, no candle twice")

# ---- trading -------------------------------------------------------------------------------------


class Trading(Oanda):
    def __init__(self, fill=True):
        super().__init__()
        self.sent, self.fill = [], fill

    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append(("post", url, json, dict(headers or {})))

        if not self.fill:
            return Answer(201, {"orderCancelTransaction": {"reason": "MARKET_HALTED"}})

        return Answer(201, {"orderFillTransaction": {"time": "2026-10-05T09:45:01.5Z",
                                                     "tradeOpened": {"tradeID": "7", "units": "1.0", "price": "4140.412"}}})

    def put(self, url, json=None, headers=None, timeout=None):
        self.sent.append(("put", url, json, dict(headers or {})))
        return Answer(200, {"orderFillTransaction": {"pl": "-3.5000"}} if url.endswith("/close") else {})


trading = Trading()
client = oanda.Client(token=TOKEN, account_id="101-004-1", session=trading)
opened = client.market_order(-1.0, 4160.0, 4120.0, "jarvis-gold", price_decimals=3, comment="sell on the band")
method, url, body, headers = trading.sent[-1]
check(url == oanda.PRACTICE_HOST + "/v3/accounts/101-004-1/orders" and body["order"]["type"] == "MARKET"
      and body["order"]["units"] == "-1" and body["order"]["stopLossOnFill"]["price"] == "4160.000"
      and body["order"]["takeProfitOnFill"]["price"] == "4120.000" and body["order"]["timeInForce"] == "FOK"
      and body["order"]["tradeClientExtensions"]["tag"] == "jarvis-gold",
      "a market order to the practice server, the stop and target attached at OANDA, tagged as JARVIS's")
check(opened == {"id": "7", "units": 1.0, "price": 4140.412,
                 "time": datetime(2026, 10, 5, 9, 45, 1, 500000, tzinfo=timezone.utc)}, "and the trade it opened")

client.market_order(0.01, 30200.0, None, "jarvis-trend", name="NAS100_USD", price_decimals=1)
method, url, body, headers = trading.sent[-1]
check(body["order"]["instrument"] == "NAS100_USD" and body["order"]["stopLossOnFill"]["price"] == "30200.0"
      and "takeProfitOnFill" not in body["order"] and body["order"]["units"] == "0.01",
      "an order with no target: only its stop goes on at OANDA, for a trade a moving stop will close")

try:
    oanda.Client(token=TOKEN, account_id="a", session=Trading(fill=False)).market_order(1, 1, 2, "jarvis-gold")
    said = ""
except oanda.OandaError as error:
    said = str(error)

check(said == "the order was not filled (market halted)", f"an order OANDA cancels says why ({said!r})")

mine = client.trades("jarvis-gold", state="ALL")
check([trade["id"] for trade in mine] == ["7"] and mine[0]["result"] == 22.3456 and mine[0]["close_price"] == 4170.4,
      "only JARVIS's tagged trades, with their result")
check(client.closed_by(mine[0]) == "target", "what closed it, from OANDA's own record: the target")
check(client.closed_by({"closing": []}) == "closed", "and a trade with no closing record is simply closed")
other = client.trades(state="OPEN")
check(other[1]["stop"] == 4160.0 and other[1]["target"] == 4120.0 and other[1]["units"] == -2.0,
      "an open trade's stop and target as OANDA holds them")

client.move_stop("8", 4150.0, 3)
check(trading.sent[-1][1].endswith("/trades/8/orders") and trading.sent[-1][2] == {"stopLoss": {"price": "4150.000", "timeInForce": "GTC"}},
      "a stop moved at OANDA")
check(client.close_trade("8") == -3.5 and trading.sent[-1][2] == {"units": "ALL"}, "and a trade closed, its result back")
check(all(TOKEN not in str(entry[1]) + str(entry[2]) for entry in trading.sent), "the token never in a URL or a body")

# ---- the check -----------------------------------------------------------------------------------

printed = io.StringIO()

with contextlib.redirect_stdout(printed):
    code = oanda_check.main(client=oanda.Client(token=TOKEN, account_id="a", session=Oanda()),
                            now=datetime(2026, 10, 5, 11, 2, tzinfo=timezone.utc))

output = printed.getvalue()
check(code == 0 and "GBP 10,000.00" in output and "smallest trade 0.1 units" in output and "spread $0.40" in output,
      "the check reports the account, gold's terms and the spread")
check("closed 2 minute(s) ago" in output and "Fresh" in output, "and that the last candle is fresh")
check(TOKEN not in output, "and never prints the token")

printed = io.StringIO()

with contextlib.redirect_stdout(printed):
    code = oanda_check.main(client=oanda.Client(token=TOKEN, account_id="a", session=Oanda(status=401)))

check(code == 1 and printed.getvalue().startswith("Not yet: OANDA refused the token"), "a refused token: says so and stops")

sys.exit(1 if failures else 0)
