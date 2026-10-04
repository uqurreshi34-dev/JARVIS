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
from datetime import datetime, timezone
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
    return {"time": time, "complete": complete, "volume": 10, "bid": side, "ask": ask}


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
                                            "openTradeCount": 0}})
        if path.endswith("/instruments"):
            return Answer(200, {"instruments": [{"name": "XAU_USD", "displayName": "Gold", "minimumTradeSize": "0.1",
                                                 "tradeUnitsPrecision": 1, "marginRate": "0.05"}]})
        if path.endswith("/pricing"):
            return Answer(200, {"prices": [{"time": "2026-10-05T11:02:03.123456789Z",
                                            "bids": [{"price": "4140.28"}], "asks": [{"price": "4140.68"}]}]})
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

check(client.summary() == {"currency": "GBP", "balance": 10000.0, "margin_available": 10000.0, "open_trades": 0},
      "the account's currency, balance and open trades")
check(client.instrument() == {"name": "XAU_USD", "display_name": "Gold", "minimum_units": 0.1, "unit_decimals": 1,
                              "margin_rate": 0.05}, "OANDA's own terms for gold, as it sends them")

bid, ask, when = client.price()
check((bid, ask) == (4140.28, 4140.68) and when == datetime(2026, 10, 5, 11, 2, 3, 123456, tzinfo=timezone.utc),
      "the price now, its time read to the microsecond from OANDA's nanoseconds")

made, ask_closes = client.candles(count=5)
check(len(made) == 2 and made[-1] == (datetime(2026, 10, 5, 10, 45, tzinfo=timezone.utc), 4139.0, 4141.0, 4138.0, 4140.0)
      and ask_closes == [4138.4, 4140.4],
      "finished candles only, on the bid, with the ask's close alongside; the forming one left out")
check(server.asked[-1][1] == {"granularity": "M15", "count": 6, "price": "BA"}, "asked for bid and ask, 15-minute candles")

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
