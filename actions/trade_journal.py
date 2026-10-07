"""Your own trades at OANDA -- the ones placed by hand -- kept in a journal and judged by their numbers.

A strategy cannot be judged on one trade, or on a screenshot of someone
else's. It takes twenty or thirty, each recorded the same way: where it
went in, where its stop and target were, how it came out. JARVIS does the
recording. Every trade on the OANDA demo account that his own traders did
not place (theirs carry a tag; one placed by hand on the website or the
phone has none) is copied, once, into manual-trades.csv in the JARVIS
folder when it has closed:

    trade, instrument, side, units, opened and closed (UK time), entry, stop,
    target, exit, result (in the account's currency), how it closed (target,
    stop, or by hand), R, counted

R is the result measured in the trade's own risk -- the distance from the
entry to the stop it closed with: -1 R is a full stop-out, +3 R a 1:3 target
reached. That makes trades of any size, and any stop, comparable.

A trade that should not count -- placed by mistake, say -- stays in the
file with "counted" changed to "no". Deleting its row would only bring it
back on the next look.

    "how are my trades going"         the account's money from closed trades, OANDA's own figure, split
                                      into the gold trader's and yours; then your strategy, judged on the
                                      trades you have not marked out; then the gold trader's record
    python tools/trade_journal.py     the same, with every trade listed
"""

import csv
import os
import threading

from actions import files, gold_strategy, gold_trader, oanda, trend_trader


JOURNAL_NAME = "manual-trades.csv"

COLUMNS = ["trade", "instrument", "side", "units", "opened (UK)", "closed (UK)", "entry", "stop", "target", "exit",
           "result", "how", "R", "counted"]

# Trades JARVIS's own traders placed, by the tag each puts on its trades.
OWN_TAGS = frozenset({gold_trader.TAG, trend_trader.TAG})

# How far back OANDA is asked: its most recent closed trades, up to its own limit.
LOOK_BACK = 500

# A gap this size between OANDA's figure and the logs' is said (financing, a trade from before the logs); smaller is rounding.
OTHER_AT_LEAST = 0.05

# Fewer finished trades than this and the numbers are said with a warning: too few to judge a strategy by.
ENOUGH_TRADES = 30

_lock = threading.Lock()


def path():
    base = files.root()
    return os.path.join(base, JOURNAL_NAME) if base else None


def rows():
    """The journal's rows, oldest first."""
    where = path()

    if not where or not os.path.exists(where):
        return []

    with open(where, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _uk(moment):
    return gold_strategy.uk_time(moment).strftime("%Y-%m-%d %H:%M") if moment else ""


def r_multiple(side, entry, stop, exit_price):
    """The result in units of the trade's risk (entry to stop), or None without a stop to measure by."""
    if stop is None or exit_price is None or entry == stop:
        return None

    moved = exit_price - entry if side == "buy" else entry - exit_price
    return moved / abs(entry - stop)


def _row(trade, how):
    side = "buy" if trade["units"] > 0 else "sell"
    r = r_multiple(side, trade["price"], trade["stop"], trade["close_price"])
    price = lambda value: f"{value:.5f}".rstrip("0").rstrip(".") if value is not None else ""

    return {
        "trade": trade["id"], "instrument": trade["instrument"], "side": side, "units": f"{abs(trade['units']):g}",
        "opened (UK)": _uk(trade["opened"]), "closed (UK)": _uk(trade["closed"]),
        "entry": price(trade["price"]), "stop": price(trade["stop"]), "target": price(trade["target"]),
        "exit": price(trade["close_price"]), "result": f"{trade['result']:.2f}", "how": how,
        "R": f"{r:.2f}" if r is not None else "", "counted": "yes",
    }


def sync(client=None):
    """Copy into the journal, once each, the trades placed by hand that have closed. Returns how many were added."""
    if client is None:
        if not oanda.configured():
            return 0

        client = oanda.Client()

    with _lock:
        done = {row["trade"] for row in rows()}
        new = [trade for trade in client.trades(state="CLOSED", count=LOOK_BACK)
               if trade["tag"] not in OWN_TAGS and trade["id"] not in done]

        if not new:
            return 0

        where = path()

        if not where:
            return 0

        # OANDA lists the newest first; the journal reads oldest first.
        new.sort(key=lambda trade: (trade["closed"] or trade["opened"], int(trade["id"])))
        fresh = not os.path.exists(where)

        with open(where, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)

            if fresh:
                writer.writeheader()

            for trade in new:
                writer.writerow(_row(trade, client.closed_by(trade)))

    return len(new)


def _counted(row):
    return row.get("counted", "yes").strip().casefold() != "no"


def _total(logged):
    return sum(float(row["result"]) for row in logged if row.get("result"))


def money(realized=None, currency=""):
    """Where the account's money from closed trades came from, the parts adding up to OANDA's own figure.

    [realized] is OANDA's Realized P/L for the account; without it (OANDA out of reach), the logs alone.
    Your own trades count here in full, marked or not: a trade left out of judging the strategy still
    made or lost that money.
    """
    unit = f" {currency}" if currency else ""
    parts = [("the gold trader", _total(gold_trader.logged())), ("your own trades", _total(rows()))]
    trend = trend_trader.logged()

    if trend:
        parts.append(("the trend trader", _total(trend)))

    listed = ", ".join(f"{name} {value:+.2f}" for name, value in parts)

    if realized is None:
        return f"From the logs, sir: {listed}{unit}."

    other = realized - sum(value for _name, value in parts)
    rest = f", and {other:+.2f} from anything else, such as financing" if abs(other) >= OTHER_AT_LEAST else ""
    return f"Closed trades have made {realized:+.2f}{unit} on the account, sir: {listed}{rest}."


def summary():
    """How your own strategy is doing, judged on the trades you have not marked out: wins, R, how they closed."""
    kept = rows()
    counted = [row for row in kept if _counted(row)]
    left_out = len(kept) - len(counted)
    aside = f", leaving out the {left_out} you marked" if left_out else ""

    if not counted:
        return f"There are no finished trades of your own to judge your strategy by yet{aside}."

    wins = sum(1 for row in counted if float(row["result"]) > 0)
    said = [f"Judging your strategy{aside}: {len(counted)} trade{'s' if len(counted) != 1 else ''}, {wins} won,"
            f" {len(counted) - wins} lost."]
    measured = [float(row["R"]) for row in counted if row.get("R")]

    if measured:
        won = [value for value in measured if value > 0]
        lost = [value for value in measured if value <= 0]
        parts = []

        if won:
            parts.append(f"winners averaged {sum(won) / len(won):+.1f} R")

        if lost:
            parts.append(f"losers {sum(lost) / len(lost):+.1f} R")

        said.append(f"Measured in risk, {' and '.join(parts)}: {sum(measured) / len(measured):+.2f} R a trade on average.")

    reached = {how: sum(1 for row in counted if row.get("how") == how) for how in ("target", "stop", "closed")}
    said.append(f"{reached['target']} reached the target, {reached['stop']} the stop, and {reached['closed']} were"
                f" closed by hand.")

    if len(counted) < ENOUGH_TRADES:
        said.append(f"That's too few to judge it by: about {ENOUGH_TRADES - len(counted)} more before the numbers"
                    f" mean much.")

    return " ".join(said)


def _bot_record():
    logged = [row for row in gold_trader.logged() if row.get("result")]
    state = "on" if gold_trader.settings()["enabled"] else "off"
    wins = sum(1 for row in logged if float(row["result"]) > 0)
    return f"The gold trader is {state}: {len(logged)} finished trade{'s' if len(logged) != 1 else ''}, {wins} won."


def report():
    """For JARVIS to say: the account's money from closed trades and where it came from, then each record."""
    realized, currency, stale = None, "", ""

    if oanda.configured():
        try:
            client = oanda.Client()
            sync(client)
            account = client.summary()
            realized, currency = account["realized"], account.get("currency", "")
        except oanda.OandaError as error:
            print(f"[JARVIS] trade journal: {error}")
            stale = " OANDA couldn't be reached, so trades closed since I last looked aren't counted yet."

    return f"{money(realized, currency)}{stale} {summary()} {_bot_record()}"
