"""JARVIS's trend trader: the tested daily trend-follower, on an OANDA demo account, on its own.

A second strategy beside the gold trader (actions/gold_trader.py), and
independent of it: its own settings, its own log, and its own tag at OANDA
("jarvis-trend"), so neither ever touches the other's trades -- nor any
trade placed by hand.

Once each day, when OANDA's daily candle has closed (17:00 New York), it
reads the daily candles of its instrument (the Nasdaq 100 by default) and,
exactly as the backtest does (actions/trend_strategy.py):

- with no trade open, takes a breakout of the last N days' high (or low,
  unless set to buys only), its first stop 2 ATR away, held at OANDA, and
  no target -- the stop, moved behind the trend, takes the profit;
- with one open, moves its stop up behind the best close (the "atr"
  exits), only ever forward, or closes it on a close beyond the opposite
  channel (the "channel" exits).

Trades last weeks and are held over weekends; the stop stays at OANDA
throughout, JARVIS running or not. Every trade is announced and logged,
with its result when it closes. No model is asked anything: each decision
is arithmetic on the candles. The account is OANDA's practice server only
(actions/oanda.py): demo money.

Settings, in trend-trader.json in the JARVIS folder (written with these
defaults the first time, and off until you turn it on):

    enabled            false until you switch it on
    instrument         "NAS100_USD": OANDA's name for what to trade
    setup              "trend-20-atr-long": which tested version, by the backtest's key
    risk_percent       1: each trade's first stop risks this share of the balance (null: fixed units)
    practice_balance   null, or e.g. 400: size as if the account held this, plus this trader's own results
    units              units a trade when risk_percent is null
    fresh_hours        6: a new trade only on a daily candle closed this recently, not one
                       JARVIS finds on starting days later

    python tools/trend_trader.py --on | --off | --status | --once | --set name=value
"""

import csv
import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from actions import files, journal, oanda, trend_strategy
from actions.gold_strategy import uk_time


SETTINGS_NAME = "trend-trader.json"
LOG_NAME = "trend-trades.csv"

TAG = "jarvis-trend"

DEFAULTS = {
    "enabled": False,
    "instrument": "NAS100_USD",
    "setup": "trend-20-atr-long",
    "risk_percent": 1,
    "practice_balance": None,
    "units": 0.01,
    "fresh_hours": 6,
}

# Ceilings no setting passes: a typing slip must not become a position or a risk many times the intended.
MOST_UNITS = 10
MOST_RISK_PERCENT = 5

# How often it looks for a newly closed daily candle.
POLL_SECONDS = 300

# Days of history: the longest channel tested, the ATR, and room to spare.
HISTORY = 200

LOG_COLUMNS = ["trade", "opened (UK)", "instrument", "side", "units", "entry", "first stop", "closed (UK)", "exit",
               "result", "how"]


# ---- settings ---------------------------------------------------------------------------------

def _path(name):
    base = files.root()
    return os.path.join(base, name) if base else None


def _understood(found):
    """The settings [found], defaults filled in and each made the type it should be; None if one cannot be."""
    chosen = dict(DEFAULTS)
    chosen.update({key: value for key, value in found.items() if key in DEFAULTS})

    try:
        chosen["enabled"] = chosen["enabled"] is True
        chosen["instrument"] = str(chosen["instrument"]).upper()
        chosen["setup"] = str(chosen["setup"]).strip()
        chosen["units"] = min(MOST_UNITS, max(0.0, float(chosen["units"])))
        chosen["fresh_hours"] = max(1, int(chosen["fresh_hours"]))

        for name in ("risk_percent", "practice_balance"):
            chosen[name] = None if chosen[name] is None else max(0.0, float(chosen[name])) or None

        if chosen["risk_percent"] is not None:
            chosen["risk_percent"] = min(MOST_RISK_PERCENT, chosen["risk_percent"])

        if not re.fullmatch(r"[A-Z0-9]{2,10}_[A-Z]{3}", chosen["instrument"]) \
                or chosen["setup"] not in trend_strategy.REGISTRY:
            return None
    except (TypeError, ValueError, AttributeError):
        return None

    return chosen


def settings():
    """The settings, defaults filled in and nonsense refused; written out the first time."""
    path = _path(SETTINGS_NAME)
    found = {}

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                found = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[JARVIS] trend trader: {SETTINGS_NAME} could not be read ({error}); not trading")
            return dict(DEFAULTS, enabled=False)
    elif path:
        _write_settings(DEFAULTS)

    if path and isinstance(found, dict) and found and set(DEFAULTS) != set(found):
        _write_settings({key: found.get(key, value) for key, value in DEFAULTS.items()})

    chosen = _understood(found) if isinstance(found, dict) else None

    if chosen is None:
        print(f"[JARVIS] trend trader: a setting in {SETTINGS_NAME} is not understood; not trading")
        return dict(DEFAULTS, enabled=False)

    return chosen


def _write_settings(values):
    path = _path(SETTINGS_NAME)

    if not path:
        return

    temporary = path + ".tmp"

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2)

    os.replace(temporary, path)


def change(values):
    """Set [values] in the settings file, keeping everything else; refused if the result would not be understood."""
    path = _path(SETTINGS_NAME)
    current = dict(DEFAULTS)

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                current.update(json.load(handle))
        except ValueError as error:
            raise ValueError(f"{SETTINGS_NAME} is not valid JSON ({error}); fix or delete it first") from None

    current.update(values)

    if _understood(current) is None:
        raise ValueError(f"that would leave {SETTINGS_NAME} with a setting not understood; nothing changed")

    _write_settings(current)


def switch(on):
    change({"enabled": bool(on)})


# ---- the log ----------------------------------------------------------------------------------

def logged():
    path = _path(LOG_NAME)

    if not path or not os.path.exists(path):
        return []

    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _log(row):
    path = _path(LOG_NAME)

    if not path:
        return

    new = not os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS)

        if new:
            writer.writeheader()

        writer.writerow(row)


def _uk(moment):
    return uk_time(moment).strftime("%Y-%m-%d %H:%M") if moment else ""


def status():
    """How the trend trader has done, in a sentence or two."""
    rows = [row for row in logged() if row.get("result")]
    chosen = settings()
    state = "on" if chosen["enabled"] else "off"

    if not rows:
        return f"The trend trader is {state}, sir, and has no finished trades yet."

    results = [float(row["result"]) for row in rows]
    wins = sum(1 for value in results if value > 0)
    return (f"The trend trader is {state}, sir. {len(rows)} finished trade{'s' if len(rows) != 1 else ''}, "
            f"{wins} won, {sum(results):+.2f} in the demo account's currency overall.")


# ---- the trader -------------------------------------------------------------------------------

def _from_comment(comment):
    """(rules key, first stop distance) from a trade's comment at OANDA, or (None, None)."""
    key, risk = None, None

    for part in str(comment or "").split():
        if part.startswith("risk="):
            try:
                risk = float(part[5:])
            except ValueError:
                pass
        elif part in trend_strategy.REGISTRY:
            key = part

    return key, risk


class TrendTrader:
    def __init__(self, client_factory=None, clock=None):
        self._client_factory = client_factory or oanda.Client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._client = None
        self._terms = {}
        self._listener = None
        self._stop = threading.Event()
        self._thread = None
        self._last_day = None

    def set_listener(self, listener):
        self._listener = listener

    def _say(self, text):
        print(f"[JARVIS] trend trader: {text}")

        if self._listener:
            try:
                self._listener(text)
            except Exception as error:
                print(f"[JARVIS] trend trader could not announce: {error}")

    def start(self):
        if not oanda.configured():
            return

        if not settings()["enabled"]:
            print(f"[JARVIS] trend trader is off (turn it on in {SETTINGS_NAME}, or python tools/trend_trader.py --on)")
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trend-trader", daemon=True)
        self._thread.start()
        print("[JARVIS] trend trader started (OANDA demo)")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except oanda.OandaError as error:
                print(f"[JARVIS] trend trader: {error}")
            except Exception as error:
                print(f"[JARVIS] trend trader failed a check: {type(error).__name__}: {error}")

            self._stop.wait(POLL_SECONDS)

    def _account(self):
        if self._client is None:
            self._client = self._client_factory()

        return self._client

    def _instrument(self, client, name):
        if name not in self._terms:
            self._terms[name] = client.instrument(name)

        return self._terms[name]

    # ---- each look ------------------------------------------------------------------------

    def tick(self):
        """One look: log what has closed, then -- once per newly closed day -- manage the open trade or maybe open one."""
        chosen = settings()

        if not chosen["enabled"]:
            return "off"

        client = self._account()
        self._settle_closed(client)
        candles, _asks = client.candles(name=chosen["instrument"], granularity="D", count=HISTORY)

        if not candles:
            return "no candles"

        if candles[-1][0] == self._last_day:
            return "waiting"

        self._last_day = candles[-1][0]
        rules = trend_strategy.REGISTRY[chosen["setup"]]
        atrs = trend_strategy.average_true_range(candles, trend_strategy.ATR_LENGTH)
        last = len(candles) - 1
        mine = [trade for trade in client.trades(TAG, state="OPEN")]

        if mine:
            return self._manage(client, chosen, mine[0], candles, atrs, last)

        closed_at = candles[-1][0] + timedelta(days=1)

        if (self._clock() - closed_at).total_seconds() > chosen["fresh_hours"] * 3600:
            return "stale"

        side = trend_strategy.signal(last, candles, rules)

        if not side:
            return "quiet"

        if not atrs[last]:
            return "no ATR yet"

        return self._open(client, chosen, rules, side, atrs[last])

    def _manage(self, client, chosen, trade, candles, atrs, last):
        """Move the open trade's stop behind its best close, or close it on the opposite channel, as its rules say."""
        key, _risk = _from_comment(trade.get("comment"))
        rules = trend_strategy.REGISTRY.get(key, trend_strategy.REGISTRY[chosen["setup"]])
        buying = trade["units"] > 0
        side = "buy" if buying else "sell"
        terms = self._instrument(client, chosen["instrument"])

        if rules.exit == "channel":
            if trend_strategy._channel_exit(last, candles, SimpleNamespace(side=side), rules):
                result = client.close_trade(trade["id"])
                journal.write("trend", f"closed {trade['id']}", f"a close beyond the {rules.exit_days}-day channel")
                self._say(f"{terms['display_name']}, sir: the trend has turned, so the trade is closed: {result:+.2f}.")
                self._settle_closed(client, how="channel exit")
                return "closed"
            return "holding"

        if not atrs[last]:
            return "holding"

        # The best close since the trade opened -- the entry itself until a day closes beyond it -- as the backtest keeps it.
        since = [trade["price"]] + [candle[4] for candle in candles if candle[0] + timedelta(days=1) > trade["opened"]]

        if buying:
            moved = max(since) - trend_strategy.TRAIL_ATR * atrs[last]
            better = trade["stop"] is None or moved > trade["stop"]
        else:
            moved = min(since) + trend_strategy.TRAIL_ATR * atrs[last]
            better = trade["stop"] is None or moved < trade["stop"]

        if not better:
            return "holding"

        client.move_stop(trade["id"], moved, terms["price_decimals"])
        journal.write("trend", f"stop {trade['id']}", f"moved to {moved:.{terms['price_decimals']}f}")
        self._say(f"{terms['display_name']}, sir: the trend trade's stop is now {moved:,.{terms['price_decimals']}f}, "
                  f"behind its best close.")
        return "stop moved"

    def _open(self, client, chosen, rules, side, atr):
        terms = self._instrument(client, chosen["instrument"])
        bid, ask, _when = client.price(chosen["instrument"])
        entry = ask if side == "buy" else bid
        distance = trend_strategy.FIRST_STOP_ATR * atr
        stop = entry - distance if side == "buy" else entry + distance
        sized = self._size(client, chosen, terms, distance)

        if isinstance(sized, str):
            return sized

        units, risked = sized
        trade = client.market_order(units if side == "buy" else -units, stop, None, TAG, name=chosen["instrument"],
                                    price_decimals=terms["price_decimals"],
                                    comment=f"{rules.key()} risk={distance:.{terms['price_decimals']}f}")
        verb = "bought" if side == "buy" else "sold"
        self._say(f"{terms['display_name']}, sir, a trend {side}: {verb} {units:g} at {trade['price']:,.{terms['price_decimals']}f}, "
                  f"stop {stop:,.{terms['price_decimals']}f}, no target -- the stop follows the trend{risked}. Demo account.")
        journal.write("trend", f"{verb} {units:g} {chosen['instrument']} at {trade['price']}", f"stop {stop:.2f} ({rules.key()})")
        return side

    def _size(self, client, chosen, terms, distance):
        """(units, words on the risk) for the next trade, or a reason not to trade -- as the gold trader sizes."""
        decimals, smallest = terms["unit_decimals"], terms["minimum_units"]

        def down(value):
            return math.floor(value * 10 ** decimals + 1e-9) / 10 ** decimals

        if not chosen["risk_percent"]:
            return round(max(smallest, chosen["units"]), decimals), ""

        account = client.summary()
        currency = account["currency"]

        if chosen["practice_balance"]:
            balance = chosen["practice_balance"] + sum(float(row["result"]) for row in logged() if row.get("result"))
        else:
            balance = account["balance"]

        dollars_per_unit = _dollars_per(client, currency)
        allowed = balance * chosen["risk_percent"] / 100
        units = min(MOST_UNITS, down(allowed * dollars_per_unit / distance))

        if units < smallest:
            if smallest * distance / dollars_per_unit > 2 * allowed:
                self._say(f"A trend setup on {terms['display_name']}, sir, but even the smallest trade would risk more than"
                          f" twice {chosen['risk_percent']:g}% of {balance:,.0f} {currency}; no trade.")
                return "too small to size"
            units = smallest

        return units, f", risking {units * distance / dollars_per_unit:.2f} {currency}"

    def _settle_closed(self, client, how=None):
        """Log, once each, the trades of this trader's that have closed, and say how they went."""
        done = {row["trade"] for row in logged()}

        for trade in client.trades(TAG, state="CLOSED"):
            if trade["id"] in done:
                continue

            reached = how or client.closed_by(trade)
            _key, risk = _from_comment(trade.get("comment"))
            first_stop = ""

            if risk is not None:
                first_stop = f"{trade['price'] - risk if trade['units'] > 0 else trade['price'] + risk:.2f}"

            _log({
                "trade": trade["id"], "opened (UK)": _uk(trade["opened"]), "instrument": trade.get("instrument", ""),
                "side": "buy" if trade["units"] > 0 else "sell", "units": f"{abs(trade['units']):g}",
                "entry": f"{trade['price']:.2f}", "first stop": first_stop, "closed (UK)": _uk(trade["closed"]),
                "exit": f"{trade['close_price']:.2f}" if trade["close_price"] is not None else "",
                "result": f"{trade['result']:.2f}", "how": reached,
            })

            if how is None:
                self._say(f"The trend trade has closed at its {reached}, sir: {trade['result']:+.2f}.")


def _dollars_per(client, currency):
    """How many dollars one unit of the account's currency buys, at OANDA's mid price."""
    if currency == "USD":
        return 1.0

    try:
        bid, ask, _when = client.price(f"{currency}_USD")
        return (bid + ask) / 2
    except oanda.OandaError:
        bid, ask, _when = client.price(f"USD_{currency}")
        return 2 / (bid + ask)


trader = TrendTrader()
