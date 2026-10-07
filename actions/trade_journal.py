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

    "how are my trades going"         spoken, with the gold trader's record too
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


def summary(currency=""):
    """How your own trades have gone, in a sentence or three."""
    kept = rows()
    counted = [row for row in kept if row.get("counted", "yes").strip().casefold() != "no"]
    left_out = len(kept) - len(counted)
    aside = f" ({left_out} left out, as you marked them)" if left_out else ""

    if not counted:
        return f"You have no finished trades of your own in the journal yet, sir{aside}."

    results = [float(row["result"]) for row in counted]
    wins = sum(1 for value in results if value > 0)
    money = f"{sum(results):+.2f}{' ' + currency if currency else ''}"
    said = [f"{len(counted)} finished trade{'s' if len(counted) != 1 else ''} of your own{aside}, sir: "
            f"{wins} won, {len(counted) - wins} lost, {money} overall."]

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
        said.append(f"That's too few to judge the strategy by: about {ENOUGH_TRADES - len(counted)} more before the"
                    f" numbers mean much.")

    return " ".join(said)


def report():
    """For JARVIS to say: your own trades, brought up to date, then the gold trader's record."""
    currency, stale = "", ""

    if oanda.configured():
        try:
            client = oanda.Client()
            sync(client)
            currency = client.summary().get("currency", "")
        except oanda.OandaError as error:
            print(f"[JARVIS] trade journal: {error}")
            stale = " OANDA couldn't be reached, so trades closed since I last looked aren't in it yet."

    return f"{summary(currency)}{stale} {gold_trader.status()}"
