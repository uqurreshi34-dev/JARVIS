"""JARVIS's gold trader: the tested strategy, on an OANDA demo account, on its own.

Every fifteen minutes in the trading window it reads OANDA's finished
candles, works out the trend and looks for each kind of setup it has been
told to trade (the "setups" setting: a bounce off a band, a pullback to the
20-candle average, a squeeze breaking out), exactly as the backtest does
(actions/gold_strategy.py), and either does nothing or places one trade
with its stop and target attached on OANDA's servers -- fixed, or sized by
how lively gold is (ATR). Every trade is
announced and logged, with its result when it closes.

No model is asked anything: each decision is arithmetic on the candles, so
the trader runs the same whichever language model JARVIS has, or none.

The account is OANDA's practice server only (actions/oanda.py): demo money.

Trading practice it keeps to:

- the stop and target go on with the order, held by OANDA, so they work
  even if JARVIS or the PC is off; with breakeven in the chosen rules, the
  stop moves to the entry once the trade is 1 R up;
- one trade at a time (or up to max_open_trades, never one against
  another, as a demo account without hedging nets them), a fixed size, at
  most so many trades a day, and it stands down for the day after so many
  losses in a row;
- with announce_forming, it says when a candle could be the first of a
  setup, a candle before the trade would come, so a chart watched by hand
  can be checked against the same rules -- though not on a candle that has
  just opened a trade, which says only the trade;
- no trade when the spread is wider than usual, nor on a candle that is
  not fresh (JARVIS started late, or the market paused);
- nothing at weekends, and anything still open on Friday evening is closed
  rather than carried over the weekend's gap;
- no new trade within half an hour either side of a high-impact US release
  (actions/gold_news.py, the economic calendar), and none at all if the
  calendar cannot be read -- it does not assume the coast is clear;
- every trade logged (gold-trades.csv in the JARVIS folder, which opens in
  Excel), and the log is only ever added to.

Settings, in gold-trader.json in the JARVIS folder (written with these
defaults the first time, and off until you turn it on):

    enabled              false until you switch it on
    units                ounces per trade (1 = Fortrade's 0.01 lot), when not sized by risk
    risk_percent         null, or e.g. 1: size each trade so its stop risks this share of the account
    practice_balance     null, or e.g. 400: size as if the account held this, plus or minus the
                         trader's own results so far -- a small account practised on a big demo
    days                 the days it trades
    session_hours        [10, 14]: candles closing 10:00 to 14:00, on session_zone's clock
    session_zone         "uk", or "new_york": whose clock session_hours are in; New York's
                         follows the US session through the weeks when only one country
                         has changed its clocks (e.g. [8, 12] New York is 13:00-17:00 UK,
                         and 12:00-16:00 UK in late October and late March)
    max_spread           the widest spread, in dollars, it will trade into
    max_trades_per_day   null: every setup that meets the rules; or a number
    max_open_trades      1: trades open at once, all the same way (up to MOST_OPEN)
    announce_forming     true: say when a setup may be forming, a candle early
    max_losses_in_a_row  2: after this many losses in a row it stands down until tomorrow
                         (null: never)
    setups               ["bounce-1:3-be"]: which tested setups to trade, by the backtest's key;
                         the backtest names those worth trading. A key ending "-room2" adds
                         the room rule: no trade with under 2 R of clear space before the
                         first four-hour support or resistance line in its way (the lines the
                         trade plan draws), said aloud when one is skipped
    friday_close         "20:00": anything open is closed then, UK time
    news_filter          true: stand aside around high-impact news
    calendar_url, news_currencies, news_impact, news_minutes_before,
    news_minutes_after   which news, and how long either side (gold_news.py)

    python tools/gold_trader.py --on | --off | --status | --once
"""

import csv
import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone

from actions import files, gold_news, gold_strategy, journal, oanda


SETTINGS_NAME = "gold-trader.json"
LOG_NAME = "gold-trades.csv"

# Marks JARVIS's own trades on the account, so a trade you place by hand is
# never touched or counted.
TAG = "jarvis-gold"

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

DEFAULTS = {
    "enabled": False,
    "units": 1,
    "risk_percent": None,
    "practice_balance": None,
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "session_hours": [10, 14],
    "session_zone": "uk",
    "max_spread": 1.0,
    "max_trades_per_day": None,
    "max_open_trades": 1,
    "announce_forming": True,
    "setups": ["bounce-1:3-be"],
    "max_losses_in_a_row": 2,
    "friday_close": "20:00",
    "news_filter": True,
    "calendar_url": gold_news.CALENDAR_URL,
    "news_currencies": ["USD"],
    "news_impact": ["High"],
    "news_minutes_before": 30,
    "news_minutes_after": 30,
}

# A size no setting can exceed: a typing slip in the settings must not
# become a position fifty times the intended one.
MOST_UNITS = 10

# Nor so many trades open at once.
MOST_OPEN = 5

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

    # Settings added since the file was written appear in it, with their
    # defaults, so every one can be seen and changed, and retired ones go;
    # nothing set is changed.
    if path and found and set(DEFAULTS) != set(found):
        _write_settings({key: found.get(key, value) for key, value in DEFAULTS.items()})

    chosen = _understood(found)

    if chosen is None:
        print(f"[JARVIS] gold trader: a setting in {SETTINGS_NAME} is not understood; not trading")
        return dict(DEFAULTS, enabled=False, session_hours=(10, 14), friday_close=(20, 0))

    return chosen


def _understood(found):
    """The settings [found], defaults filled in and each made the type it should be; None if one cannot be."""
    chosen = dict(DEFAULTS)
    chosen.update({key: value for key, value in found.items() if key in DEFAULTS})

    try:
        chosen["units"] = min(MOST_UNITS, max(0.0, float(chosen["units"])))

        for name in ("risk_percent", "practice_balance"):
            chosen[name] = None if chosen[name] is None else max(0.0, float(chosen[name])) or None

        for name in ("max_trades_per_day", "max_losses_in_a_row"):
            chosen[name] = None if chosen[name] is None else max(1, int(chosen[name]))

        chosen["max_open_trades"] = min(MOST_OPEN, max(1, int(chosen["max_open_trades"])))
        chosen["announce_forming"] = chosen["announce_forming"] is not False
        chosen["max_spread"] = max(0.0, float(chosen["max_spread"]))
        start, end = (int(hour) for hour in chosen["session_hours"])
        chosen["session_hours"] = (start, end)

        if chosen["session_zone"] not in gold_strategy.ZONES or not 0 <= start < end <= 24:
            return None
        hour, minute = (int(part) for part in str(chosen["friday_close"]).split(":"))
        chosen["friday_close"] = (hour, minute)
        chosen["days"] = [day for day in DAYS if day in set(chosen["days"])]
        chosen["enabled"] = chosen["enabled"] is True
        chosen["news_filter"] = chosen["news_filter"] is not False
        chosen["news_minutes_before"] = max(0, int(chosen["news_minutes_before"]))
        chosen["news_minutes_after"] = max(0, int(chosen["news_minutes_after"]))
        chosen["news_currencies"] = [str(value) for value in chosen["news_currencies"]]
        chosen["news_impact"] = [str(value) for value in chosen["news_impact"]]
        chosen["calendar_url"] = str(chosen["calendar_url"])
        setups = chosen["setups"]
        chosen["setups"] = [str(key).strip() for key in (setups.split(",") if isinstance(setups, str) else setups)]

        if not chosen["setups"] or any(gold_strategy.REGISTRY.get(key) is None
                                       or gold_strategy.REGISTRY[key].exits not in gold_strategy.TRADEABLE_EXITS
                                       for key in chosen["setups"]):
            return None
    except (TypeError, ValueError, AttributeError):
        return None

    return chosen


def traded_rules():
    """The rules of the setups gold-trader.json lists, read without writing anything (the backtest marks them).

    The strategy's CHOSEN when the file is missing, unreadable or not
    understood -- as the trader would not trade then either.
    """
    path = _path(SETTINGS_NAME)

    try:
        with open(path, encoding="utf-8") as handle:
            chosen = _understood(json.load(handle))
    except (OSError, TypeError, ValueError, AttributeError):
        chosen = None

    return active_rules(chosen) if chosen else [gold_strategy.CHOSEN]


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
    change({"enabled": bool(on)})


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

    previous = dict(current)
    current.update(values)
    _write_settings(current)

    if _understood(current) is None:
        _write_settings(previous)
        raise ValueError(f"that would leave {SETTINGS_NAME} with a setting not understood; nothing changed")


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

def active_rules(chosen):
    """The rules of each setup the settings list, in their order."""
    return [gold_strategy.REGISTRY[key] for key in chosen["setups"]]


def _from_comment(comment):
    """(rules key, first stop distance) from a trade's comment at OANDA, or (None, None)."""
    key, risk = None, None

    for part in str(comment or "").split():
        if part.startswith("risk="):
            try:
                risk = float(part[5:])
            except ValueError:
                pass
        elif part in gold_strategy.REGISTRY:
            key = part

    return key, risk


def _losses_in_a_row(trades):
    """How many of the latest closed trades in a row lost; a win or a breakeven ends the run."""
    run = 0

    for trade in sorted((trade for trade in trades if trade["state"] == "CLOSED" and trade["closed"]),
                        key=lambda trade: trade["closed"], reverse=True):
        if trade["result"] >= 0:
            break
        run += 1

    return run


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
        self._briefed = None
        self._no_calendar = None
        self._entries = {}   # trade id -> (spread at entry), for the log
        self._looked = (None, None)   # (candle closing time, (candles, indicators) or None)
        self._forming_due = None      # (client, settings, candle closing time) to look at once decided
        self._lines = (None, None)    # (four-hour candle it was read after, (lines, ATR)) for the room rule

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

    # What tick() returns when it has opened a trade.
    OPENED = ("buy", "sell")

    def tick(self):
        """One look: settle what has closed, close for the weekend if due, and maybe trade. Returns what it did.

        A setup that may be forming is said only once the candle's decision
        is made, and not on a candle that has just opened a trade: that
        candle has been acted on, and a second, early warning beside the
        trade would read as a contradiction of it.
        """
        self._forming_due = None
        decided = self._decide()
        due, self._forming_due = self._forming_due, None

        if due and decided not in self.OPENED:
            self._forming(*due)

        return decided

    def _decide(self):
        """tick()'s look, without the warning of a setup forming; returns what it did."""
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

        if not gold_strategy.in_session(closed_at, chosen["session_hours"], chosen["session_zone"]):
            return "outside hours"

        if mine:
            self._protect(client, mine)

        if chosen["announce_forming"]:
            self._forming_due = (client, chosen, closed_at)

        if len(mine) >= chosen["max_open_trades"]:
            return "in a trade"

        today = self._today(client, local.date())

        if chosen["max_trades_per_day"] is not None and len(today) >= chosen["max_trades_per_day"]:
            return "enough trades today"

        if chosen["max_losses_in_a_row"] is not None and _losses_in_a_row(today) >= chosen["max_losses_in_a_row"]:
            if self._stood_down != local.date():
                self._stood_down = local.date()
                self._say("Standing down from gold for the rest of the day, sir: the loss limit is reached.")
            return "stood down"

        if chosen["news_filter"]:
            standing = self._news(now, local, chosen)

            if standing:
                return standing

        looked = self._look(client, closed_at)

        if looked is None:
            return "no fresh candle"

        candles, (bands, rsis, squeezes, atrs, averages, fast) = looked
        last = len(candles) - 1

        # Each kind of setup traded, in the order listed; the first to see one takes it.
        for rules in active_rules(chosen):
            side = gold_strategy.signal(last, candles, bands, rsis, squeezes, rules, averages, fast)

            if side and any((trade["units"] > 0) != (side == "buy") for trade in mine):
                journal.write("gold", f"{side} skipped", "a trade the other way is open")
                return "other way open"

            if side:
                return self._open(client, side, chosen, rules, atrs[last])

        return "quiet"

    def _look(self, client, closed_at):
        """(candles, indicators) up to the candle that closed at [closed_at], once per candle; None if OANDA
        has not finished it."""
        if self._looked[0] != closed_at:
            wanted = max([HISTORY] + [gold_strategy.history_needed(rules) for rules in active_rules(settings())])
            candles, _asks = client.candles(count=wanted)
            fresh = candles and candles[-1][0] + timedelta(minutes=gold_strategy.CANDLE_MINUTES) == closed_at
            self._looked = (closed_at, (candles, gold_strategy.indicators(candles)) if fresh else None)

        return self._looked[1]

    def _forming(self, client, chosen, closed_at):
        """Say so when the candle just closed could be the first of a setup the trader trades.

        Only when the confirming candle would still close in the hours, and
        never on a candle that opened a trade (tick). Otherwise said whether
        or not a trade could follow (one open, the news, a stand down), as it
        is for a chart watched by hand as much as for the trader.
        """
        confirms_at = closed_at + timedelta(minutes=gold_strategy.CANDLE_MINUTES)

        if not gold_strategy.in_session(confirms_at, chosen["session_hours"], chosen["session_zone"]):
            return

        looked = self._look(client, closed_at)

        if looked is None:
            return

        candles, (bands, _rsis, squeezes, _atrs, averages, fast) = looked
        last = len(candles) - 1

        for rules in active_rules(chosen):
            side = gold_strategy.forming(last, candles, bands, squeezes, rules, averages, fast)

            if side:
                self._say(f"Gold, sir: a possible {rules.setup} {side} on the {_uk(closed_at)[-5:]} candle, "
                          f"closing at {candles[last][4]:.2f}. The {_uk(confirms_at)[-5:]} candle decides.")
                return

    def _news(self, now, local, chosen):
        """"news" or "no calendar" when the trader should stand aside; None when clear.

        The first look of each day also says which releases fall in the
        window, so a quiet hour is not a mystery.
        """
        try:
            found = gold_news.events(chosen["calendar_url"])
        except gold_news.CalendarError as error:
            if self._no_calendar != local.date():
                self._no_calendar = local.date()
                self._say(f"I can't read the economic calendar, sir ({error}), so no new gold trades until I can.")
            return "no calendar"

        if self._briefed != local.date():
            self._briefed = local.date()
            start, end = chosen["session_hours"]
            zoned = gold_strategy.ZONES[chosen["session_zone"]](now)
            offset = zoned - now
            day = zoned.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            window_start = (day + timedelta(hours=start)).replace(tzinfo=timezone.utc) - offset \
                - timedelta(minutes=chosen["news_minutes_after"])
            window_end = (day + timedelta(hours=end)).replace(tzinfo=timezone.utc) - offset \
                + timedelta(minutes=chosen["news_minutes_before"])
            ahead = gold_news.today(window_start, window_end, chosen, found)

            if ahead:
                named = "; ".join(f"{title} at {gold_strategy.uk_time(when):%H:%M}" for when, _currency, _impact, title in ahead)
                self._say(f"Gold today, sir: {named}. No new gold trades from {chosen['news_minutes_before']} "
                          f"minutes before each to {chosen['news_minutes_after']} after.")

        event = gold_news.blocking(now, chosen, found)

        if event:
            journal.write("gold", "standing aside", f"{event[3]} at {gold_strategy.uk_time(event[0]):%H:%M} UK")
            return "news"

        return None

    def _protect(self, client, mine):
        """With breakeven in a trade's rules: once it is 1 R up, its stop goes to the entry, at OANDA.

        Each trade carries its rules' key and its first stop distance in its
        comment at OANDA, so this holds across restarts of JARVIS.
        """
        prices = None

        for trade in mine:
            key, risk = _from_comment(trade.get("comment", ""))
            rules = gold_strategy.REGISTRY.get(key, gold_strategy.CHOSEN)

            if not rules.breakeven:
                continue

            risk = risk or rules.stop
            bid, ask, _when = prices = prices or client.price()
            buying = trade["units"] > 0
            gained = (bid - trade["price"]) if buying else (trade["price"] - ask)
            already = trade["stop"] is not None and (trade["stop"] >= trade["price"] if buying else trade["stop"] <= trade["price"])

            if gained >= risk and not already:
                client.move_stop(trade["id"], trade["price"], self._gold["price_decimals"])
                self._say(f"The gold trade is {risk:.2f} dollars up, sir: its stop is now at the entry, "
                          f"{trade['price']:.2f}, so it can no longer lose.")

    def _today(self, client, today):
        return [trade for trade in client.trades(TAG, state="ALL")
                if trade["opened"] and gold_strategy.uk_time(trade["opened"]).date() == today]

    def _open(self, client, side, chosen, rules, atr):
        bid, ask, _when = client.price()
        spread = ask - bid

        if spread > chosen["max_spread"]:
            journal.write("gold", f"{side} skipped", f"spread ${spread:.2f} over ${chosen['max_spread']:.2f}")
            return "spread too wide"

        if rules.exits not in gold_strategy.TRADEABLE_EXITS:
            self._say(f"The gold rules {rules.key()} exit by '{rules.exits}', which the trader cannot place, sir; no trade.")
            return "exits not supported"

        entry = ask if side == "buy" else bid
        levels = gold_strategy.exits_for(rules, entry, side, atr)

        if levels is None:
            return "no ATR yet"

        stop, target, distance = levels

        if rules.room:
            blocked = self._blocked(client, rules, side, entry, distance)

            if blocked:
                return blocked

        sized = self._size(client, chosen, distance)

        if isinstance(sized, str):
            return sized

        units, risked = sized
        trade = client.market_order(units if side == "buy" else -units, stop, target, TAG,
                                    price_decimals=self._gold["price_decimals"],
                                    comment=f"{rules.key()} risk={distance:.2f}")
        self._entries[trade["id"]] = spread
        verb = "Bought" if side == "buy" else "Sold"
        self._say(f"Gold, sir, a {rules.setup}: {verb.lower()} {units:g} ounce{'s' if units != 1 else ''} at {trade['price']:.2f}, "
                  f"stop {stop:.2f}, target {target:.2f}{risked}. Demo account.")
        journal.write("gold", f"{verb} {units:g} oz at {trade['price']:.2f} ({rules.key()})",
                      f"stop {stop:.2f}, target {target:.2f}")
        return side

    def _four_hour_lines(self, client):
        """(lines, four-hour ATR) from OANDA's finished four-hour candles, read once per candle."""
        block = gold_strategy.line_block(self._clock())

        if self._lines[0] != block:
            candles, _asks = client.candles(oanda.GOLD, f"H{gold_strategy.LINE_HOURS}", gold_strategy.LINE_CANDLES)
            self._lines = (block, gold_strategy.chart_lines_from(candles))

        return self._lines[1]

    def _blocked(self, client, rules, side, entry, distance):
        """Why a trade has no room before the next four-hour line, or None when it has enough."""
        try:
            lines, atr = self._four_hour_lines(client)
        except oanda.OandaError as error:
            journal.write("gold", f"{side} skipped", f"the four-hour lines could not be read ({error})")
            return "no lines"

        enough, room, line = gold_strategy.room_for(rules, side, entry, distance, lines, atr)

        if enough:
            return None

        kind = "resistance" if side == "buy" else "support"
        self._say(f"Gold, sir: a {rules.setup} {side} at {entry:.2f}, skipped: {kind} at {line['price']:.2f} leaves"
                  f" {max(room, 0.0):.1f} R of room, under the {rules.room:g} R the rules ask for.")
        journal.write("gold", f"{side} skipped", f"{kind} {line['price']:.2f}, room {room:.2f} R")
        return "no room"

    def _size(self, client, chosen, distance):
        """(units, words on the risk) for the next trade, or a reason not to trade.

        By risk: the stop's distance times the units is to lose no more
        than risk_percent of the balance -- the real one, or the practice
        balance plus the trader's own results -- converted from dollars to
        the account's currency at OANDA's price. Rounded down to what OANDA
        allows, and within MOST_UNITS. If even OANDA's smallest trade would
        risk more than twice the share, there is no trade.
        """
        decimals, smallest = self._gold["unit_decimals"], self._gold["minimum_units"]

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

        dollars_per_unit = self._dollars_per(client, currency)
        allowed = balance * chosen["risk_percent"] / 100
        units = min(MOST_UNITS, down(allowed * dollars_per_unit / distance))

        if units < smallest:
            if smallest * distance / dollars_per_unit > 2 * allowed:
                self._say(f"A gold setup, sir, but even the smallest trade would risk more than twice "
                          f"{chosen['risk_percent']:g}% of {balance:,.0f} {currency}; no trade.")
                return "too small to size"
            units = smallest

        risked = units * distance / dollars_per_unit
        return units, f", risking {risked:.2f} {currency}"

    def _dollars_per(self, client, currency):
        """How many dollars one unit of the account's currency buys, at OANDA's mid price."""
        if currency == "USD":
            return 1.0

        try:
            bid, ask, _when = client.price(f"{currency}_USD")
            return (bid + ask) / 2
        except oanda.OandaError:
            bid, ask, _when = client.price(f"USD_{currency}")
            return 2 / (bid + ask)

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
