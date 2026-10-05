"""OANDA's v20 REST API: a demo (practice) account, read and -- later -- traded by JARVIS.

OANDA lets a program trade an account over plain HTTPS with a token, no
terminal running, and gold from 0.1 units (a tenth of an ounce). This
module is the one place JARVIS talks to it.

Practice only. The host is OANDA's practice server, fixed here; a live
account cannot be reached through this module at all, whatever .env says.
Real money would need its own deliberate change, not a setting.

Settings, in .env:

    OANDA_API_TOKEN     the token from your OANDA demo account
                        (My Account -> Manage API Access -> Generate)
    OANDA_ACCOUNT_ID    optional; without it, the first account the token
                        can see is used

The token is a password: it is sent only to OANDA, never printed, never
logged, never written anywhere.
"""

import os
from datetime import datetime, timedelta, timezone


PRACTICE_HOST = "https://api-fxpractice.oanda.com"

GOLD = "XAU_USD"

TIMEOUT = 20


class OandaError(Exception):
    """Something OANDA refused or could not answer, in words fit to say."""


def configured():
    return bool(os.environ.get("OANDA_API_TOKEN", "").strip())


class Client:
    """A practice account. Requests go through [session] (requests.Session by default)."""

    def __init__(self, token=None, account_id=None, session=None):
        self._token = (token if token is not None else os.environ.get("OANDA_API_TOKEN", "")).strip()
        self._account = (account_id if account_id is not None else os.environ.get("OANDA_ACCOUNT_ID", "")).strip()

        if not self._token:
            raise OandaError("OANDA_API_TOKEN is not set in .env")

        if session is None:
            import requests

            session = requests.Session()

        self._session = session

    # ---- the wire --------------------------------------------------------------------------

    def _get(self, path, params=None):
        return self._send("get", path, params=params or {})

    def _send(self, method, path, **request):
        headers = {"Authorization": f"Bearer {self._token}", "Accept-Datetime-Format": "RFC3339"}

        if "json" in request:
            headers["Content-Type"] = "application/json"

        try:
            response = getattr(self._session, method)(PRACTICE_HOST + path, headers=headers, timeout=TIMEOUT, **request)
        except Exception as error:
            raise OandaError(f"OANDA could not be reached ({type(error).__name__})") from None

        if response.status_code == 401:
            raise OandaError("OANDA refused the token: check OANDA_API_TOKEN is the demo account's")

        if not 200 <= response.status_code < 300:
            try:
                said = response.json().get("errorMessage", "")
            except Exception:
                said = ""

            raise OandaError(f"OANDA answered {response.status_code}" + (f": {said}" if said else ""))

        return response.json()

    # ---- the account ----------------------------------------------------------------------

    def account_id(self):
        """The account to use: OANDA_ACCOUNT_ID, or the first the token can see."""
        if not self._account:
            accounts = self._get("/v3/accounts").get("accounts") or []

            if not accounts:
                raise OandaError("the token can see no OANDA account")

            self._account = accounts[0]["id"]

        return self._account

    def summary(self):
        """{"currency", "balance", "margin_available", "open_trades"} of the account."""
        account = self._get(f"/v3/accounts/{self.account_id()}/summary")["account"]
        return {
            "currency": account.get("currency", ""),
            "balance": float(account.get("balance", 0)),
            "margin_available": float(account.get("marginAvailable", 0)),
            "open_trades": int(account.get("openTradeCount", 0)),
        }

    def instrument(self, name=GOLD):
        """OANDA's own terms for [name]: smallest trade, decimal places allowed, margin rate."""
        found = self._get(f"/v3/accounts/{self.account_id()}/instruments", {"instruments": name}).get("instruments")

        if not found:
            raise OandaError(f"this account cannot trade {name}")

        details = found[0]
        return {
            "name": details.get("name", name),
            "display_name": details.get("displayName", name),
            "minimum_units": float(details.get("minimumTradeSize", 0)),
            "unit_decimals": int(details.get("tradeUnitsPrecision", 0)),
            "price_decimals": int(details.get("displayPrecision", 2)),
            "margin_rate": float(details.get("marginRate", 0)),
        }

    # ---- prices ---------------------------------------------------------------------------

    def price(self, name=GOLD):
        """(bid, ask, time) now."""
        prices = self._get(f"/v3/accounts/{self.account_id()}/pricing", {"instruments": name}).get("prices") or []

        if not prices or not prices[0].get("bids") or not prices[0].get("asks"):
            raise OandaError(f"no price for {name} just now (the market may be closed)")

        quote = prices[0]
        return float(quote["bids"][0]["price"]), float(quote["asks"][0]["price"]), parse_time(quote.get("time"))

    def candles(self, name=GOLD, granularity="M15", count=300):
        """The last [count] finished candles, oldest first, as (start, open, high, low, close) on the bid.

        Bid prices, as the backtest uses, with the ask's close alongside in
        a second list: (candles, ask closes). The candle still forming is
        left out.
        """
        answer = self._get(f"/v3/instruments/{name}/candles",
                           {"granularity": granularity, "count": count + 1, "price": "BA"})
        made, ask_closes = [], []

        for candle in answer.get("candles") or []:
            if not candle.get("complete"):
                continue

            bid, ask = candle["bid"], candle["ask"]
            made.append((parse_time(candle["time"]), float(bid["o"]), float(bid["h"]), float(bid["l"]), float(bid["c"])))
            ask_closes.append(float(ask["c"]))

        return made[-count:], ask_closes[-count:]

    def history(self, start, end, name=GOLD, granularity="M15", page=5000):
        """Every finished candle from [start] to [end] (UTC), oldest first: (candles, ask closes), as candles() gives.

        Asked for a page at a time, as OANDA allows at most 5,000 candles a request.
        """
        made, ask_closes = [], []
        since = start

        while since < end:
            answer = self._get(f"/v3/instruments/{name}/candles",
                               {"granularity": granularity, "price": "BA", "count": page,
                                "from": since.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")})
            found = [candle for candle in answer.get("candles") or [] if candle.get("complete")]

            for candle in found:
                moment = parse_time(candle["time"])

                if moment >= end or (made and moment <= made[-1][0]):
                    continue

                bid, ask = candle["bid"], candle["ask"]
                made.append((moment, float(bid["o"]), float(bid["h"]), float(bid["l"]), float(bid["c"])))
                ask_closes.append(float(ask["c"]))

            if not found or parse_time(found[-1]["time"]) <= since:
                break

            since = parse_time(found[-1]["time"]) + timedelta(seconds=1)

        return made, ask_closes


    # ---- trades ---------------------------------------------------------------------------

    def market_order(self, units, stop, target, tag, name=GOLD, price_decimals=2, comment=""):
        """Buy (units > 0) or sell (units < 0) at the market, the stop and target attached.

        The stop and target are placed with the order, on OANDA's servers,
        so they hold whether or not JARVIS is running. Returns the opened
        trade as {"id", "units", "price", "time"}; an order OANDA cancels
        (the market closed, not enough margin) is an OandaError naming why.
        """
        def price(value):
            return f"{value:.{price_decimals}f}"

        order = {
            "type": "MARKET",
            "instrument": name,
            "units": f"{units:g}",
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": price(stop), "timeInForce": "GTC"},
            "clientExtensions": {"tag": tag, "comment": comment[:120]},
            "tradeClientExtensions": {"tag": tag, "comment": comment[:120]},
        }

        # A trade that rides a trend has no target: its stop, moved behind it, takes the profit.
        if target is not None:
            order["takeProfitOnFill"] = {"price": price(target), "timeInForce": "GTC"}
        answer = self._send("post", f"/v3/accounts/{self.account_id()}/orders", json={"order": order})
        filled = answer.get("orderFillTransaction") or {}
        opened = filled.get("tradeOpened")

        if not opened:
            cancelled = answer.get("orderCancelTransaction") or {}
            raise OandaError(f"the order was not filled ({cancelled.get('reason', 'no reason given').replace('_', ' ').lower()})")

        return {"id": opened["tradeID"], "units": float(opened["units"]), "price": float(opened["price"]),
                "time": parse_time(filled.get("time"))}

    def trades(self, tag=None, state="OPEN"):
        """Trades in [state] ("OPEN", "CLOSED", "ALL"), those JARVIS tagged [tag] only if given."""
        found = self._get(f"/v3/accounts/{self.account_id()}/trades", {"state": state, "count": 50}).get("trades") or []
        return [_trade(trade) for trade in found
                if tag is None or (trade.get("clientExtensions") or {}).get("tag") == tag]

    def trade(self, trade_id):
        return _trade(self._get(f"/v3/accounts/{self.account_id()}/trades/{trade_id}")["trade"])

    def closed_by(self, trade):
        """What closed a trade: "target", "stop" or "closed" (by hand, or by JARVIS), from OANDA's own record."""
        if not trade.get("closing"):
            return "closed"

        reason = self._get(f"/v3/accounts/{self.account_id()}/transactions/{trade['closing'][-1]}") \
            .get("transaction", {}).get("reason", "")
        return {"TAKE_PROFIT_ORDER": "target", "STOP_LOSS_ORDER": "stop"}.get(reason, "closed")

    def move_stop(self, trade_id, price, price_decimals=2):
        """Move an open trade's stop to [price], at OANDA."""
        self._send("put", f"/v3/accounts/{self.account_id()}/trades/{trade_id}/orders",
                   json={"stopLoss": {"price": f"{price:.{price_decimals}f}", "timeInForce": "GTC"}})

    def close_trade(self, trade_id):
        """Close a trade at the market now; its realised result in the account's currency."""
        answer = self._send("put", f"/v3/accounts/{self.account_id()}/trades/{trade_id}/close", json={"units": "ALL"})
        filled = answer.get("orderFillTransaction") or {}

        if not filled:
            cancelled = answer.get("orderCancelTransaction") or {}
            raise OandaError(f"the trade was not closed ({cancelled.get('reason', 'no reason given').replace('_', ' ').lower()})")

        return float(filled.get("pl", 0))


def _trade(trade):
    """One of OANDA's trades as JARVIS keeps it."""
    def order_price(name):
        order = trade.get(name) or {}
        return float(order["price"]) if order.get("price") else None

    return {
        "id": trade["id"],
        "instrument": trade.get("instrument", ""),
        "state": trade.get("state", ""),
        "units": float(trade.get("initialUnits", trade.get("currentUnits", 0))),
        "price": float(trade.get("price", 0)),
        "opened": parse_time(trade.get("openTime")),
        "closed": parse_time(trade.get("closeTime")),
        "close_price": float(trade["averageClosePrice"]) if trade.get("averageClosePrice") else None,
        "result": float(trade.get("realizedPL", 0)),
        "stop": order_price("stopLossOrder"),
        "target": order_price("takeProfitOrder"),
        "comment": (trade.get("clientExtensions") or {}).get("comment", ""),
        "closing": list(trade.get("closingTransactionIDs") or []),
    }


def parse_time(text):
    """OANDA's RFC 3339 time ("2026-10-02T13:15:00.000000000Z") as a UTC datetime."""
    if not text:
        return None

    text = text.rstrip("Z")
    whole, _, fraction = text.partition(".")
    moment = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

    if fraction:
        moment = moment.replace(microsecond=int(fraction[:6].ljust(6, "0")))

    return moment
