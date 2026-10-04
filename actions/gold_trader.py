"""JARVIS's gold trader: the tested strategy, on an OANDA demo account, on its own.

Every fifteen minutes in the trading window it reads OANDA's finished
candles, works out the trend and the setup exactly as the backtest does
(actions/gold_strategy.py, the version that held up in both halves of the
year), and either does nothing -- most of the time -- or places one trade
with its stop and target attached on OANDA's servers. Every trade is
announced and logged, with its result when it closes.

No model is asked anything: each decision is arithmetic on the candles, so
the trader runs the same whichever language model JARVIS has, or none.

The account is OANDA's practice server only (actions/oanda.py): demo money.

Trading practice it keeps to:

- the stop and target go on with the order, held by OANDA, so they work
  even if JARVIS or the PC is off; with breakeven in the chosen rules, the
  stop moves to the entry once the trade is 1 R up;
- one trade at a time, a fixed size, at most so many trades a day, and it
  stands down for the day after so many losses;
- no trade when the spread is wider than usual, nor on a candle that is
  not fresh (JARVIS started late, or the market paused);
- nothing at weekends, and anything still open on Friday evening is closed
  rather than carried over the weekend's gap;
- every trade logged (gold-trades.csv in the JARVIS folder, which opens in
  Excel), and the log is only ever added to.

Settings, in gold-trader.json in the JARVIS folder (written with these
defaults the first time, and off until you turn it on):

    enabled              false until you switch it on
    units                ounces per trade (1 = Fortrade's 0.01 lot)
    days                 the days it trades
    session_hours        [10, 14]: candles closing 10:00 to 14:00 UK time
    max_spread           the widest spread, in dollars, it will trade into
    max_trades_per_day   3
    max_losses_per_day   2: after this many it stands down until tomorrow
    friday_close         "20:00": anything open is closed then, UK time

    python tools/gold_trader.py --on | --off | --status | --once
"""

import csv
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from actions import files, gold_strategy, journal, oanda


SETTINGS_NAME = "gold-trader.json"
LOG_NAME = "gold-trades.csv"

# Marks JARVIS's own trades on the account, so a trade you place by hand is
# never touched or counted.
TAG = "jarvis-gold"

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

DEFAULTS = {
    "enabled": False,
    "units": 1,
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "session_hours": [10, 14],
    "max_spread": 1.0,
    "max_trades_per_day": 3,
    "max_losses_per_day": 2,
    "friday_close": "20:00",
}

# A size no setting can exceed: a typing slip in the settings must not
# become a position fifty times the intended one.
MOST_UNITS = 10

# A candle older than this when JARVIS looks at it is not traded on.
FRESH_SECONDS = 180

# How often it looks.
POLL_SECONDS = 30

# Enough history for the 200-candle average and the squeeze window.
HISTORY = 300

LOG_COLUMNS = ["trade", "opened (UK)", "side", "ounces", "entry", "stop", "target", "closed (UK)", "exit",
               "result", "how", "spread at entry"]


# ---- settings ---------------------------------------------------------------------------------

def _path(name):
    base = files.root()
    return os.path.join(base, name) if base else None


def settings():
    """The settings, defaults filled in and nonsense refused; written out the first time."""
    path = _path(SETTINGS_NAME)
    found = {}

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                found = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[JARVIS] gold trader: {SETTINGS_NAME} could not be read ({error}); not trading")
            return dict(DEFAULTS, enabled=False)
    elif path:
        _write_settings(DEFAULTS)

    chosen = dict(DEFAULTS)
    chosen.update({key: value for key, value in found.items() if key in DEFAULTS})

    try:
        chosen["units"] = min(MOST_UNITS, max(0.0, float(chosen["units"])))
        chosen["max_spread"] = max(0.0, float(chosen["max_spread"]))
        chosen["max_trades_per_day"] = max(0, int(chosen["max_trades_per_day"]))
        chosen["max_losses_per_day"] = max(0, int(chosen["max_losses_per_day"]))
        start, end = (int(hour) for hour in chosen["session_hours"])
        chosen["session_hours"] = (start, end)
        hour, minute = (int(part) for part in str(chosen["friday_close"]).split(":"))
        chosen["friday_close"] = (hour, minute)
        chosen["days"] = [day for day in DAYS if day in set(chosen["days"])]
        chosen["enabled"] = chosen["enabled"] is True
    except (TypeError, ValueError):
        print(f"[JARVIS] gold trader: a setting in {SETTINGS_NAME} is not understood; not trading")
        return dict(DEFAULTS, enabled=False, session_hours=(10, 14), friday_close=(20, 0))

    return chosen


def _write_settings(values):
    path = _path(SETTINGS_NAME)

    if not path:
        return

    temporary = path + ".tmp"

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2)

    os.replace(temporary, path)


def switch(on):
    """Turn trading on or off in the settings file, keeping everything else."""
    path = _path(SETTINGS_NAME)
    current = dict(DEFAULTS)

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                current.update(json.load(handle))
        except ValueError as error:
            raise ValueError(f"{SETTINGS_NAME} is not valid JSON ({error}); fix or delete it first") from None

    current["enabled"] = bool(on)
    _write_settings(current)


# ---- the log ----------------------------------------------------------------------------------

def logged():
    """The rows of the log, oldest first."""
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
    return gold_strategy.uk_time(moment).strftime("%Y-%m-%d %H:%M") if moment else ""


def status():
    """How the trader has done, in a sentence or two."""
    rows = [row for row in logged() if row.get("result")]
    chosen = settings()
    state = "on" if chosen["enabled"] else "off"

    if not rows:
        return f"The gold trader is {state}, sir, and has no finished trades yet."

    results = [float(row["result"]) for row in rows]
    wins = sum(1 for value in results if value > 0)
    return (f"The gold trader is {state}, sir. {len(rows)} finished trade{'s' if len(rows) != 1 else ''}, "
            f"{wins} won, {sum(results):+.2f} in the demo account's currency overall.")


# ---- the trader -------------------------------------------------------------------------------

class GoldTrader:
    def __init__(self, client_factory=None, clock=None):
        self._client_factory = client_factory or oanda.Client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._client = None
        self._gold = None
        self._listener = None
        self._stop = threading.Event()
        self._thread = None
        self._last_candle = None
        self._stood_down = None
        self._entries = {}   # trade id -> (spread at entry), for the log

    def set_listener(self, listener):
        self._listener = listener

    def _say(self, text):
        print(f"[JARVIS] gold trader: {text}")

        if self._listener:
            try:
                self._listener(text)
            except Exception as error:
                print(f"[JARVIS] gold trader could not announce: {error}")

    def start(self):
        if not oanda.configured():
            return

        if not settings()["enabled"]:
            print(f"[JARVIS] gold trader is off (turn it on in {SETTINGS_NAME}, or python tools/gold_trader.py --on)")
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gold-trader", daemon=True)
        self._thread.start()
        print("[JARVIS] gold trader started (OANDA demo)")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except oanda.OandaError as error:
                print(f"[JARVIS] gold trader: {error}")
            except Exception as error:
                print(f"[JARVIS] gold trader failed a check: {type(error).__name__}: {error}")

            self._stop.wait(POLL_SECONDS)

    def _account(self):
        if self._client is None:
            self._client = self._client_factory()
            self._gold = self._client.instrument()

        return self._client

    # ---- each look ------------------------------------------------------------------------

    def tick(self):
        """One look: settle what has closed, close for the weekend if due, and maybe trade. Returns what it did."""
        chosen = settings()

        if not chosen["enabled"]:
            return "off"

        now = self._clock()
        local = gold_strategy.uk_time(now)
        day = DAYS[local.weekday()]

        if day in ("Sat", "Sun"):
            return "weekend"

        client = self._account()
        mine = client.trades(TAG, state="OPEN")
        self._settle_closed(client)

        if day == "Fri" and (local.hour, local.minute) >= chosen["friday_close"]:
            for trade in mine:
                result = client.close_trade(trade["id"])
                self._say(f"Closing the gold trade for the weekend, sir: {result:+.2f}.")
                self._settle_closed(client, how="closed for the weekend")
            return "friday close"

        if day not in chosen["days"]:
            return "not a trading day"

        # The candle that has just finished, and whether it is fresh.
        minutes = now.minute - now.minute % gold_strategy.CANDLE_MINUTES
        closed_at = now.replace(minute=minutes, second=0, microsecond=0)

        if closed_at == self._last_candle:
            return "waiting"

        self._last_candle = closed_at

        if (now - closed_at).total_seconds() > FRESH_SECONDS:
            return "stale"

        if not gold_strategy.in_session(closed_at, chosen["session_hours"]):
            return "outside hours"

        if mine:
            self._protect(client, mine)
            return "in a trade"

        today = self._today(client, local.date())

        if len(today) >= chosen["max_trades_per_day"]:
            return "enough trades today"

        if sum(1 for trade in today if trade["state"] == "CLOSED" and trade["result"] < 0) >= chosen["max_losses_per_day"]:
            if self._stood_down != local.date():
                self._stood_down = local.date()
                self._say("Standing down from gold for the rest of the day, sir: the loss limit is reached.")
            return "stood down"

        candles, _asks = client.candles(count=HISTORY)

        if not candles or candles[-1][0] + timedelta(minutes=gold_strategy.CANDLE_MINUTES) != closed_at:
            return "no fresh candle"

        bands, rsis, squeezes, _atrs, averages = gold_strategy.indicators(candles)
        side = gold_strategy.signal(len(candles) - 1, candles, bands, rsis, squeezes, gold_strategy.CHOSEN, averages)

        if not side:
            return "quiet"

        return self._open(client, side, chosen)

    def _protect(self, client, mine):
        """With breakeven in the rules: once a trade is 1 R up, its stop goes to the entry, at OANDA."""
        rules = gold_strategy.CHOSEN

        if not rules.breakeven:
            return

        bid, ask, _when = client.price()

        for trade in mine:
            buying = trade["units"] > 0
            gained = (bid - trade["price"]) if buying else (trade["price"] - ask)
            already = trade["stop"] is not None and (trade["stop"] >= trade["price"] if buying else trade["stop"] <= trade["price"])

            if gained >= rules.stop and not already:
                client.move_stop(trade["id"], trade["price"], self._gold["price_decimals"])
                self._say(f"The gold trade is {rules.stop:g} dollars up, sir: its stop is now at the entry, "
                          f"{trade['price']:.2f}, so it can no longer lose.")

    def _today(self, client, today):
        return [trade for trade in client.trades(TAG, state="ALL")
                if trade["opened"] and gold_strategy.uk_time(trade["opened"]).date() == today]

    def _open(self, client, side, chosen):
        bid, ask, _when = client.price()
        spread = ask - bid

        if spread > chosen["max_spread"]:
            journal.write("gold", f"{side} skipped", f"spread ${spread:.2f} over ${chosen['max_spread']:.2f}")
            return "spread too wide"

        units = round(max(self._gold["minimum_units"], chosen["units"]), self._gold["unit_decimals"])

        if units <= 0:
            return "no size"

        rules = gold_strategy.CHOSEN

        if rules.exits != "fixed":
            self._say(f"The chosen gold rules exit by '{rules.exits}', which the trader cannot place yet, sir; no trade.")
            return "exits not supported"

        entry = ask if side == "buy" else bid
        stop = entry - rules.stop if side == "buy" else entry + rules.stop
        target = entry + rules.target if side == "buy" else entry - rules.target
        trade = client.market_order(units if side == "buy" else -units, stop, target, TAG,
                                    price_decimals=self._gold["price_decimals"],
                                    comment=f"{side} on the band, trend and RSI")
        self._entries[trade["id"]] = spread
        verb = "Bought" if side == "buy" else "Sold"
        self._say(f"Gold, sir: {verb.lower()} {units:g} ounce{'s' if units != 1 else ''} at {trade['price']:.2f}, "
                  f"stop {stop:.2f}, target {target:.2f}. Demo account.")
        journal.write("gold", f"{verb} {units:g} oz at {trade['price']:.2f}", f"stop {stop:.2f}, target {target:.2f}")
        return side

    def _settle_closed(self, client, how=None):
        """Log, once each, the trades of JARVIS's that have closed, and say how they went."""
        done = {row["trade"] for row in logged()}

        for trade in client.trades(TAG, state="CLOSED"):
            if trade["id"] in done:
                continue

            reached = how or client.closed_by(trade)
            _log({
                "trade": trade["id"], "opened (UK)": _uk(trade["opened"]),
                "side": "buy" if trade["units"] > 0 else "sell", "ounces": f"{abs(trade['units']):g}",
                "entry": f"{trade['price']:.2f}", "stop": f"{trade['stop']:.2f}" if trade["stop"] else "",
                "target": f"{trade['target']:.2f}" if trade["target"] else "", "closed (UK)": _uk(trade["closed"]),
                "exit": f"{trade['close_price']:.2f}" if trade["close_price"] is not None else "",
                "result": f"{trade['result']:.2f}", "how": reached,
                "spread at entry": f"{self._entries.pop(trade['id']):.2f}" if trade["id"] in self._entries else "",
            })

            if how is None:
                self._say(f"The gold trade closed at its {reached}, sir: {trade['result']:+.2f}."
                          if reached in ("target", "stop") else
                          f"The gold trade has closed, sir: {trade['result']:+.2f}.")


trader = GoldTrader()
