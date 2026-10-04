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
from datetime import datetime, timezone


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
        try:
            response = self._session.get(
                PRACTICE_HOST + path,
                params=params or {},
                headers={"Authorization": f"Bearer {self._token}", "Accept-Datetime-Format": "RFC3339"},
                timeout=TIMEOUT,
            )
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
