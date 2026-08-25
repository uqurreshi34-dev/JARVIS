"""Bitcoin and currency rates, from free keyless APIs.

CoinGecko gives the Bitcoin price in pounds with its 24 hour change.
Frankfurter serves European Central Bank reference rates, so it is asked for
both today and the previous publication to work out the direction.

Nothing here is required for JARVIS to work: if a lookup fails, the HUD
simply shows nothing rather than stale or invented numbers.
"""

import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

from actions import files


TIMEOUT = 6

# Crypto moves constantly; central bank rates publish once a day.
CRYPTO_CACHE = 120
RATES_CACHE = 3600

# How often the background refresh runs.
POLL_SECONDS = 120

_CRYPTO_URL = "https://api.coingecko.com/api/v3/simple/price"

# Past prices for reports, which CoinGecko serves free and without a key.
_HISTORY_URL = "https://api.coingecko.com/api/v3/coins/{coin}/market_chart"

# What JARVIS watches. The spoken name is what he says aloud; the id is
# what CoinGecko calls it, which is not always the same -- XRP is "ripple".
COINS = {
    "bitcoin": {"id": "bitcoin", "label": "BTC", "spoken": "Bitcoin"},
    "ethereum": {"id": "ethereum", "label": "ETH", "spoken": "Ethereum"},
    "xrp": {"id": "ripple", "label": "XRP", "spoken": "XRP"},
}

# Spoken words mapped to one of the above.
COIN_WORDS = {
    "bitcoin": "bitcoin", "bit coin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "etherium": "ethereum", "eth": "ethereum",
    "ether": "ethereum",
    "xrp": "xrp", "ripple": "xrp", "x r p": "xrp", "exrp": "xrp",
    # Speech turns the letters into words surprisingly often.
    "ex rp": "xrp", "excerpt": "xrp", "x ray p": "xrp",
}


def coin_for(word):
    """Which coin a spoken word refers to, or None."""
    return COIN_WORDS.get(" ".join((word or "").casefold().split()))


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


def crypto():
    """Every watched coin as {name: (price, percent change)}.

    One request covers all three, which matters: CoinGecko's free tier is
    generous but not unlimited, and asking three times would be wasteful.
    """
    cached = _cached("crypto", CRYPTO_CACHE)

    if cached:
        return cached

    ids = ",".join(coin["id"] for coin in COINS.values())

    payload = _get(
        _CRYPTO_URL,
        {
            "ids": ids,
            "vs_currencies": "gbp",
            "include_24hr_change": "true",
        },
    )

    if not payload:
        return _cached("crypto", RATES_CACHE * 24) or {}

    found = {}

    for name, coin in COINS.items():
        entry = (payload or {}).get(coin["id"])

        if not entry:
            continue

        try:
            found[name] = (
                float(entry["gbp"]),
                float(entry.get("gbp_24h_change") or 0.0),
            )
        except (TypeError, KeyError, ValueError):
            continue

    if not found:
        return _cached("crypto", RATES_CACHE * 24) or {}

    return _store("crypto", found)


def bitcoin():
    """Bitcoin in pounds as (price, percent change), or (None, None).

    Kept because the HUD and the spoken summary already call it.
    """
    return crypto().get("bitcoin", (None, None))


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


# What "last 24 hours", "last week" and "last month" mean to CoinGecko,
# which takes a number of days.
PERIODS = {
    "day": {"days": 1, "spoken": "the last 24 hours"},
    "week": {"days": 7, "spoken": "the last week"},
    "month": {"days": 30, "spoken": "the last month"},
}

PERIOD_WORDS = {
    "24 hours": "day", "24 hour": "day", "day": "day", "today": "day",
    "last 24 hours": "day", "last day": "day", "yesterday": "day",
    "week": "week", "last week": "week", "7 days": "week",
    "month": "month", "last month": "month", "30 days": "month",
}


def period_for(word):
    """Which period a spoken phrase refers to, or None."""
    return PERIOD_WORDS.get(" ".join((word or "").casefold().split()))


def history(name, period="day"):
    """Past prices for a coin as a list of (when, price), oldest first.

    CoinGecko serves this free and without a key. Returns an empty list
    when unavailable, so a report can say so rather than inventing data.
    """
    coin = COINS.get((name or "").casefold())
    window = PERIODS.get(period, PERIODS["day"])

    if not coin:
        return []

    key = f"history:{name}:{period}"

    cached = _cached(key, RATES_CACHE)

    if cached:
        return cached

    payload = _get(
        _HISTORY_URL.format(coin=coin["id"]),
        {"vs_currency": "gbp", "days": window["days"]},
    )

    points = (payload or {}).get("prices") or []

    readings = []

    for entry in points:
        try:
            stamp, price = entry[0], float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue

        readings.append((
            datetime.fromtimestamp(stamp / 1000.0),
            price,
        ))

    if not readings:
        return []

    return _store(key, readings)


def summarise(readings):
    """Turn a series of prices into the figures a report needs."""
    if not readings:
        return None

    prices = [price for _when, price in readings]

    first = prices[0]
    last = prices[-1]

    highest = max(prices)
    lowest = min(prices)

    change = ((last - first) / first * 100.0) if first else 0.0

    # How far it swung, as a share of the average: a rough measure of how
    # jumpy the period was.
    average = sum(prices) / len(prices)
    spread = ((highest - lowest) / average * 100.0) if average else 0.0

    return {
        "open": first,
        "close": last,
        "high": highest,
        "low": lowest,
        "change": change,
        "spread": spread,
        "readings": len(prices),
        "from": readings[0][0],
        "to": readings[-1][0],
    }


def spoken_price(price):
    """A price said the way a person would say it.

    Reading "40,132.87" aloud digit by digit is unusable, so it is rounded
    to something a listener can actually hold on to.
    """
    try:
        value = float(price)
    except (TypeError, ValueError):
        return "an unknown amount"

    if value >= 1000:
        return f"{value / 1000:.1f} thousand pounds"

    if value >= 10:
        return f"{value:.0f} pounds"

    return f"{value:.2f} pounds"


THRESHOLD_FILE = "market-alerts.txt"

# Used when nothing has been set for a coin.
DEFAULT_THRESHOLD = 3.0

# The range a threshold may take. The floor is low enough to be useful for
# a quiet pair and high enough that a rounding wobble is not an event.
MIN_THRESHOLD = 0.01
MAX_THRESHOLD = 90.0


def _threshold_path():
    base = files.root()

    return os.path.join(base, THRESHOLD_FILE) if base else None


def thresholds():
    """How far each coin must move before JARVIS speaks, as a percentage."""
    settings = {}

    path = _threshold_path()

    if not path or not os.path.exists(path):
        return settings

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue

                name, _, value = line.partition(":")

                try:
                    settings[name.strip().casefold()] = abs(float(value))
                except ValueError:
                    continue

    except OSError as error:
        print(f"[JARVIS] could not read the alert settings: {error}")

    return settings


def threshold_for(name):
    """The percentage move that matters for one coin."""
    return thresholds().get(name, DEFAULT_THRESHOLD)


def set_threshold(name, percent):
    """Remember how far a coin must move. Returns True on success."""
    name = (name or "").casefold()

    if name not in COINS:
        return False

    try:
        percent = abs(float(percent))
    except (TypeError, ValueError):
        return False

    if not MIN_THRESHOLD <= percent <= MAX_THRESHOLD:
        return False

    settings = thresholds()
    settings[name] = percent

    path = _threshold_path()

    if not path:
        return False

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "# How far each coin must move before JARVIS mentions it.\n"
                "# One per line, as a percentage.\n"
            )

            for coin, value in sorted(settings.items()):
                handle.write(f"{coin}: {value:g}\n")

        return True

    except OSError as error:
        print(f"[JARVIS] could not save the alert settings: {error}")
        return False


def describe_thresholds():
    """Spoken summary of what he is watching for."""
    settings = thresholds()

    parts = []

    for name, coin in COINS.items():
        percent = settings.get(name, DEFAULT_THRESHOLD)
        suffix = "" if name in settings else " by default"

        parts.append(f"{coin['spoken']} at {percent:g} percent{suffix}")

    return f"I'm watching {', '.join(parts)}, sir."


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
