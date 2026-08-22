"""Bitcoin and currency rates, from free keyless APIs.

CoinGecko gives the Bitcoin price in pounds with its 24 hour change.
Frankfurter serves European Central Bank reference rates, so it is asked for
both today and the previous publication to work out the direction.

Nothing here is required for JARVIS to work: if a lookup fails, the HUD
simply shows nothing rather than stale or invented numbers.
"""

import threading
import time
from datetime import date, timedelta

import requests


TIMEOUT = 6

# Crypto moves constantly; central bank rates publish once a day.
CRYPTO_CACHE = 120
RATES_CACHE = 3600

# How often the background refresh runs.
POLL_SECONDS = 120

_CRYPTO_URL = "https://api.coingecko.com/api/v3/simple/price"
_RATES_URL = "https://api.frankfurter.app"

_session = requests.Session()
_session.headers.update({"User-Agent": "JARVIS/1.0"})

_lock = threading.Lock()
_cache = {}


def _get(url, params=None):
    try:
        response = _session.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()

        return response.json()

    except (requests.RequestException, ValueError) as error:
        print(f"[JARVIS] market lookup failed: {error}")
        return None


def _cached(key, max_age):
    entry = _cache.get(key)

    if entry and time.monotonic() - entry[0] < max_age:
        return entry[1]

    return None


def _store(key, value):
    _cache[key] = (time.monotonic(), value)

    return value


def bitcoin():
    """Bitcoin in pounds as (price, percent change), or (None, None)."""
    cached = _cached("bitcoin", CRYPTO_CACHE)

    if cached:
        return cached

    payload = _get(
        _CRYPTO_URL,
        {
            "ids": "bitcoin",
            "vs_currencies": "gbp",
            "include_24hr_change": "true",
        },
    )

    try:
        entry = payload["bitcoin"]
        price = float(entry["gbp"])
        change = float(entry.get("gbp_24h_change") or 0.0)

    except (TypeError, KeyError, ValueError):
        return _cached("bitcoin", RATES_CACHE) or (None, None)

    return _store("bitcoin", (price, change))


def rates():
    """Pounds against dollars and euros, with the change since last time.

    Returns {"USD": (rate, change), "EUR": (rate, change)}, or an empty
    dictionary when unavailable.
    """
    cached = _cached("rates", RATES_CACHE)

    if cached:
        return cached

    latest = _get(f"{_RATES_URL}/latest", {"from": "GBP", "to": "USD,EUR"})

    if not latest or "rates" not in latest:
        return _cached("rates", RATES_CACHE * 24) or {}

    # The previous week's rates give a direction without a second live call
    # being wasted when the market is closed.
    since = (date.today() - timedelta(days=7)).isoformat()
    earlier = _get(f"{_RATES_URL}/{since}", {"from": "GBP", "to": "USD,EUR"})

    previous = (earlier or {}).get("rates") or {}

    result = {}

    for code in ("USD", "EUR"):
        try:
            rate = float(latest["rates"][code])
        except (TypeError, KeyError, ValueError):
            continue

        was = previous.get(code)

        try:
            change = ((rate - float(was)) / float(was)) * 100 if was else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            change = 0.0

        result[code] = (rate, change)

    if not result:
        return {}

    return _store("rates", result)


def snapshot():
    """Everything the HUD needs, as a list of (label, text, change)."""
    rows = []

    price, change = bitcoin()

    if price is not None:
        if price >= 1000:
            text = f"{price / 1000:.1f}k"
        else:
            text = f"{price:.0f}"

        rows.append(("BTC", f"£{text}", change))

    for code, symbol in (("USD", "$"), ("EUR", "€")):
        pair = rates().get(code)

        if pair:
            rate, change = pair
            rows.append((code, f"{symbol}{rate:.3f}", change))

    return rows


class MarketMonitor:
    """Refreshes market data in the background and reports it."""

    def __init__(self):
        self._listener = None
        self._stop = threading.Event()
        self._thread = None

    def set_listener(self, listener):
        """Register a callable taking the snapshot list."""
        self._listener = listener

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                rows = snapshot()

                if rows and self._listener:
                    self._listener(rows)

            except Exception as error:
                print(f"[JARVIS] market refresh failed: {error}")

            self._stop.wait(POLL_SECONDS)


market_monitor = MarketMonitor()


def describe():
    """Spoken summary, for when the user asks outright."""
    rows = snapshot()

    if not rows:
        return "I couldn't reach the markets, sir."

    parts = []

    for label, text, change in rows:
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
        name = {"BTC": "Bitcoin", "USD": "the dollar",
                "EUR": "the euro"}.get(label, label)

        if label == "BTC":
            parts.append(
                f"Bitcoin is {text}, {direction} {abs(change):.1f} percent")
        else:
            parts.append(f"{name} at {text}")

    return ". ".join(parts) + ", sir."
