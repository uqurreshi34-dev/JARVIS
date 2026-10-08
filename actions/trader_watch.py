"""The traders worth watching on Hyperliquid, judged on their whole record rather than their best month.

Copy-trading leaderboards rank by recent returns, and a leaderboard of
thousands always has someone on top by luck: high leverage, one big win,
nothing repeatable. Hyperliquid is different in one way that matters --
every account's trades and positions are public, read from the exchange's
own API ([info] and the stats leaderboard) -- so a record can be checked,
not just a headline.

This reads that public record and judges it the way the gold strategies
were judged (tools/gold_backtest.py): enough trades to mean something, and
money made in every part of the period, separately. It also measures what a
headline hides: the deepest fall from a peak, how much of the profit one
trade made, and how close open positions sit to liquidation.

It only ever reads what has already happened on the exchange. It places no
orders, watches no pending transactions, and follows nobody: there is no
mempool reading and no front-running here, by design.

    "show me the top traders"     refresh if stale, open the trader board
    "show trader three"           one trader's record in full, with the working
    "close the traders"           put the board away

Settings live in trader-watch.json in the JARVIS folder (written the first
time); results in trader-watch-cache.json. See tools/trader_watch.py.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from actions import files, journal
from actions.file_hologram import number_in


SETTINGS_NAME = "trader-watch.json"
CACHE_NAME = "trader-watch-cache.json"

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
SOURCE = "Hyperliquid"

DEFAULTS = {
    # Accounts smaller than this (in USD) are left out: a few hundred dollars
    # doubled is noise, not a record.
    "min_account_usd": 50000,
    # How many of the leaderboard's best recent performers are examined in full.
    "candidates": 25,
    # How far back each record is read, and in how many parts it is judged.
    "days": 90,
    "parts": 3,
    # Fewer closed trades than this in the period and nothing can be concluded.
    "min_trades": 30,
    # The board refreshes itself when asked for if older than this.
    "refresh_hours": 6,
}

# Hyperliquid keeps only the most recent this-many fills for any account.
FILLS_KEPT = 10000

# One trade making more than this share of the net profit is flagged: the
# record may be one lucky bet rather than a method.
CONCENTRATED = 0.5

# Between requests, so a refresh of twenty-five accounts never hammers the API.
# Hyperliquid limits by request weight per minute per address; when it says
# 429 the request is tried again after the wait it asks for, or a doubling
# one, up to RETRIES times.
PAUSE_SECONDS = 0.5
TIMEOUT_SECONDS = 30
RETRIES = 6
LONGEST_WAIT = 30

_DAY_MS = 86_400_000


class WatchError(Exception):
    """The exchange could not be read."""


# ---- settings and cache ----------------------------------------------------------------

def _path(name):
    base = files.root()
    return os.path.join(base, name) if base else None


def settings():
    """The settings, defaults filled in; written out the first time. Nonsense is replaced by the default."""
    path = _path(SETTINGS_NAME)
    saved = {}

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[JARVIS] trader watch: {SETTINGS_NAME} could not be read ({error}); using the defaults")
            saved = {}

    chosen = dict(DEFAULTS)

    for name, default in DEFAULTS.items():
        value = saved.get(name, default)

        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            value = default

        chosen[name] = type(default)(value)

    chosen["parts"] = min(chosen["parts"], 12)

    if path and not os.path.exists(path):
        _write(path, chosen)

    return chosen


def _write(path, data):
    temporary = f"{path}.part"

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)

    os.replace(temporary, path)


def cached():
    """The last board worked out, or None."""
    path = _path(CACHE_NAME)

    if not path or not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as handle:
            board = json.load(handle)
    except (OSError, ValueError):
        return None

    return board if isinstance(board, dict) and isinstance(board.get("traders"), list) else None


# ---- the exchange --------------------------------------------------------------------------

class Hyperliquid:
    """Hyperliquid's public, read-only endpoints. No key, no account, nothing that can trade."""

    def __init__(self, session=None, pause=PAUSE_SECONDS):
        if session is None:
            import requests

            session = requests.Session()

        self._session = session
        self._pause = pause

    def _send(self, method, url, body=None):
        for attempt in range(RETRIES):
            try:
                if method == "post":
                    response = self._session.post(url, json=body, timeout=TIMEOUT_SECONDS)
                else:
                    response = self._session.get(url, timeout=TIMEOUT_SECONDS)
            except Exception as error:
                raise WatchError(f"{SOURCE} could not be reached ({type(error).__name__})") from None

            if response.status_code == 429:
                if attempt == RETRIES - 1:
                    break

                time.sleep(_wait(response, attempt))
                continue

            if response.status_code != 200:
                raise WatchError(f"{SOURCE} answered {response.status_code}")

            time.sleep(self._pause)

            try:
                return response.json()
            except ValueError:
                raise WatchError(f"{SOURCE} sent something that is not JSON") from None

        raise WatchError(f"{SOURCE} is limiting requests; try again shortly")

    def info(self, body):
        return self._send("post", INFO_URL, body)

    def leaderboard(self):
        """[{address, name, account, windows: {day|week|month|allTime: {pnl, roi, volume}}}]."""
        answer = self._send("get", LEADERBOARD_URL)
        rows = []

        for row in (answer or {}).get("leaderboardRows") or []:
            windows = {}

            for name, figures in row.get("windowPerformances") or []:
                windows[name] = {"pnl": _float(figures.get("pnl")), "roi": _float(figures.get("roi")),
                                 "volume": _float(figures.get("vlm"))}

            rows.append({"address": row.get("ethAddress", ""), "name": row.get("displayName") or "",
                         "account": _float(row.get("accountValue")), "windows": windows})

        return rows

    def fills(self, address, start_ms, end_ms=None):
        """Every fill since [start_ms], oldest first, paging as Hyperliquid asks (up to what it keeps)."""
        found, cursor = [], int(start_ms)

        while len(found) < FILLS_KEPT:
            body = {"type": "userFillsByTime", "user": address, "startTime": cursor, "aggregateByTime": True}

            if end_ms is not None:
                body["endTime"] = int(end_ms)

            page = sorted(self.info(body) or [], key=lambda fill: fill.get("time", 0))

            # A page's size is the exchange's to choose (its documentation names
            # both 500 and 2000), so a short page is not taken as the last:
            # only an empty one, or one reaching the end of the period, is.
            if not page:
                break

            found.extend(page)
            cursor = page[-1]["time"] + 1

            if end_ms is not None and cursor > end_ms:
                break

        # Pages may overlap at their edges; a fill's trade id is unique.
        unique = {fill.get("tid", id(fill)): fill for fill in found}
        return sorted(unique.values(), key=lambda fill: fill.get("time", 0))

    def portfolio(self, address):
        """{period: {"value": [(ms, usd)], "pnl": [(ms, usd)]}} -- the account's own history."""
        periods = {}

        for name, data in self.info({"type": "portfolio", "user": address}) or []:
            periods[name] = {
                "value": [(int(when), _float(amount)) for when, amount in data.get("accountValueHistory") or []],
                "pnl": [(int(when), _float(amount)) for when, amount in data.get("pnlHistory") or []],
            }

        return periods

    def positions(self, address):
        """The open positions now, each with its leverage and how far price is from liquidating it."""
        state = self.info({"type": "clearinghouseState", "user": address}) or {}
        found = []

        for entry in state.get("assetPositions") or []:
            position = entry.get("position") or {}
            size = _float(position.get("szi"))

            if not size:
                continue

            value = abs(_float(position.get("positionValue")))
            mark = value / abs(size) if size else 0.0
            liquidation = position.get("liquidationPx")
            liquidation = _float(liquidation) if liquidation not in (None, "") else None
            leverage = (position.get("leverage") or {}).get("value")

            found.append({
                "coin": position.get("coin", ""), "side": "long" if size > 0 else "short", "size": abs(size),
                "entry": _float(position.get("entryPx")), "mark": mark, "value": value,
                "unrealised": _float(position.get("unrealizedPnl")),
                "leverage": float(leverage) if leverage else None,
                "liquidation": liquidation,
                "to_liquidation": abs(mark - liquidation) / mark if liquidation and mark else None,
            })

        account = _float(((state.get("marginSummary") or {}).get("accountValue")))
        return found, account


def _wait(response, attempt):
    """How long to wait after a 429: what the exchange asks for, or a doubling wait, never over LONGEST_WAIT."""
    asked = (getattr(response, "headers", None) or {}).get("Retry-After")

    try:
        return min(LONGEST_WAIT, max(1.0, float(asked)))
    except (TypeError, ValueError):
        return min(LONGEST_WAIT, 3 * 2 ** attempt)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---- judging a record ----------------------------------------------------------------------

def trades_from(fills):
    """Closed trades: the fills of each closing order added together, with the fees of every fill it took.

    One order filled in pieces is one decision, so it counts once.
    """
    closing = {}

    for fill in fills:
        closed = _float(fill.get("closedPnl"))

        if not closed and not str(fill.get("dir", "")).startswith("Close"):
            continue

        key = fill.get("oid", fill.get("tid"))
        trade = closing.setdefault(key, {"time": fill.get("time", 0), "coin": fill.get("coin", ""), "pnl": 0.0,
                                         "fee": 0.0, "direction": fill.get("dir", "")})
        trade["pnl"] += closed
        trade["fee"] += _float(fill.get("fee"))
        trade["time"] = max(trade["time"], fill.get("time", 0))

    return sorted(closing.values(), key=lambda trade: trade["time"])


def market(coin):
    """A coin as people know it: Hyperliquid names a spot pair by its index ("@142")."""
    return f"spot #{coin[1:]}" if coin.startswith("@") else coin


def judge(trades, parts, min_trades):
    """The record's figures, every one with the numbers it came from, so the panel can show the working.

    [parts] is the profit made in each part of the period, from the account's own profit history: it
    covers the whole period even when Hyperliquid no longer keeps the trades of its early weeks. The
    trade figures are from the closed trades it does keep.
    """
    nets = [trade["pnl"] - trade["fee"] for trade in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]
    gross_won, gross_lost = sum(wins), -sum(losses)
    net = sum(nets)
    best = max(nets) if nets else 0.0

    enough = len(trades) >= min_trades
    every_part = all(value > 0 for value in parts)
    concentrated = net > 0 and best / net > CONCENTRATED

    if not enough:
        verdict, reason = "too few", f"only {len(trades)} closed trades; {min_trades} needed to judge"
    elif not every_part:
        losing = [index + 1 for index, value in enumerate(parts) if value <= 0]
        verdict = "inconsistent"
        reason = (f"made nothing or lost money in part{'s' if len(losing) > 1 else ''}"
                  f" {', '.join(map(str, losing))} of {len(parts)}")
    elif concentrated:
        verdict, reason = "one big trade", f"one trade made {best / net:.0%} of the profit from closed trades"
    else:
        verdict, reason = "consistent", f"made money in all {len(parts)} parts, over {len(trades)} trades"

    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "gross_won": gross_won, "gross_lost": gross_lost,
        "profit_factor": gross_won / gross_lost if gross_lost else None,
        "net": net, "fees": sum(trade["fee"] for trade in trades),
        "average_win": gross_won / len(wins) if wins else 0.0,
        "average_loss": gross_lost / len(losses) if losses else 0.0,
        "best": best, "worst": min(nets) if nets else 0.0,
        "best_share": best / net if net > 0 else None,
        "parts": list(parts), "coins": coin_table(trades),
        "verdict": verdict, "reason": reason,
    }


def coin_table(trades):
    """[[market, closed trades, net]] for the markets traded most."""
    table = {}

    for trade in trades:
        row = table.setdefault(market(trade["coin"]), [0, 0.0])
        row[0] += 1
        row[1] += trade["pnl"] - trade["fee"]

    ordered = sorted(table.items(), key=lambda item: -item[1][0])
    return [[name, count, round(net, 2)] for name, (count, net) in ordered[:8]]


def curve_at(curve, when):
    """The running profit at [when], read between the points either side; before the first point, nothing."""
    if not curve or when <= curve[0][0]:
        return 0.0 if not curve or when < curve[0][0] else curve[0][1]

    for (then, before), (later, after) in zip(curve, curve[1:]):
        if then <= when <= later:
            return before + (after - before) * ((when - then) / (later - then) if later > then else 1.0)

    return curve[-1][1]


def curve_parts(curve, start_ms, end_ms, parts):
    """The profit made in each of [parts] equal parts of the period, from the profit history."""
    edges = [start_ms + (end_ms - start_ms) * index / parts for index in range(parts + 1)]
    return [curve_at(curve, later) - curve_at(curve, earlier) for earlier, later in zip(edges, edges[1:])]


# How long a position is typically held decides the style's name.
_TIMEFRAMES = ((3_600_000, "Scalper", "minutes"), (_DAY_MS, "Day trader", "hours"),
               (14 * _DAY_MS, "Swing trader", "days"), (None, "Position trader", "weeks"))

# What the profile of wins and losses is called, and when.
LOPSIDED_WINS = 0.65
LOPSIDED_LOSSES = 0.45
BIG_WINNERS = 1.5
SPECIALIST = 0.5
STEPS = 0.5
OPEN_LOSSES = 0.1
_EPSILON = 1e-9


def duration(ms):
    minutes = ms / 60_000

    if minutes < 90:
        return f"{minutes:.0f} minutes"

    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} hours"

    return f"{minutes / 1440:.1f} days"


def positions_from(fills):
    """Every position opened and closed in [fills]: side, how long it was held, how it was built.

    Each fill says the position before it (startPosition), so a position is followed from flat to flat.
    One already open when the fills begin is skipped until it is flat: its start is not known.
    """
    held, live = [], {}

    for fill in fills:
        coin = fill.get("coin", "")
        before = _float(fill.get("startPosition"))
        size = _float(fill.get("sz"))
        after = before + (size if fill.get("side") == "B" else -size)
        price, when = _float(fill.get("px")), fill.get("time", 0)
        state = live.get(coin)

        if state is None:
            if abs(after) <= _EPSILON:
                continue

            state = live[coin] = {"known": abs(before) <= _EPSILON, "side": 1 if after > 0 else -1, "opened": when,
                                  "entry": price, "size": abs(after), "adds": 0, "worse": 0}
            continue

        flat = abs(after) <= _EPSILON
        flipped = not flat and (after > 0) != (state["side"] > 0)

        if flat or flipped:
            if state["known"]:
                held.append({"coin": coin, "side": "long" if state["side"] > 0 else "short",
                             "held": when - state["opened"], "adds": state["adds"], "worse": state["worse"]})

            del live[coin]

            if flipped:
                live[coin] = {"known": True, "side": 1 if after > 0 else -1, "opened": when, "entry": price,
                              "size": abs(after), "adds": 0, "worse": 0}
        elif abs(after) > abs(before):
            if state["known"]:
                state["adds"] += 1
                state["worse"] += (price < state["entry"]) if state["side"] > 0 else (price > state["entry"])

            state["entry"] = (state["entry"] * abs(before) + price * size) / abs(after)
            state["size"] = abs(after)
        else:
            state["size"] = abs(after)

    return held


def style(fills, record, positions, covered_days, period_pnl):
    """How this trader trades, read from what they did: [[name, the numbers behind it]].

    It describes behaviour, not intent -- the same habits can come from many different reasons.
    """
    held = positions_from(fills)
    said = []

    if held:
        times = sorted(position["held"] for position in held)
        middle = times[len(times) // 2]
        name, unit = next((name, unit) for limit, name, unit in _TIMEFRAMES if limit is None or middle < limit)
        said.append([name, f"holds a position for {duration(middle)} (the middle of {len(held)} positions);"
                           f" typically {unit}"])

        long_share = sum(1 for position in held if position["side"] == "long") / len(held)
        direction = "Mostly long" if long_share >= 0.7 else "Mostly short" if long_share <= 0.3 else "Both ways"
        said.append([direction, f"{long_share:.0%} of positions long, {1 - long_share:.0%} short"])

    if record["trades"]:
        win_rate = record["win_rate"]
        ratio = record["average_win"] / record["average_loss"] if record["average_loss"] else None

        if win_rate >= LOPSIDED_WINS and ratio is not None and ratio < 1:
            name = "Many small wins"
            detail = (f"wins {win_rate:.0%} of trades, but an average loss is bigger than an average win"
                      f" ({money(record['average_loss'])} against {money(record['average_win'])})")
        elif win_rate < LOPSIDED_LOSSES and ratio is not None and ratio >= BIG_WINNERS:
            name = "Few big winners"
            detail = (f"wins only {win_rate:.0%} of trades, but an average win is {ratio:.1f} times an average"
                      f" loss: the shape of trend following")
        else:
            name = "Balanced"
            detail = f"wins {win_rate:.0%} of trades" + (f"; average win {ratio:.2f} times average loss" if ratio else "")

        said.append([name, detail])
        said.append(["Pace", f"{record['trades'] / max(covered_days, 1):.1f} closed trades a day"])

    if held:
        adds = sum(position["adds"] for position in held)
        worse = sum(position["worse"] for position in held)

        if adds / len(held) >= STEPS:
            said.append(["Builds in steps", f"{adds / len(held):.1f} additions to each position on average"])

            if worse * 2 >= adds:
                said.append(["Adds to losers", f"{worse} of {adds} additions were at a worse price than the"
                                                f" average entry (averaging down): risky if a move keeps going"])

    coins = record["coins"]

    if coins:
        top, count, _net = coins[0]
        share = count / max(record["trades"], 1)
        said.append(["Specialist" if share >= SPECIALIST else "Spread out",
                     f"{share:.0%} of trades in {top}" if share >= SPECIALIST
                     else f"{len(coins)}+ markets; the most traded, {top}, is {share:.0%} of trades"])

    unrealised = sum(position["unrealised"] for position in positions)

    if unrealised < 0 and abs(unrealised) >= OPEN_LOSSES * max(abs(period_pnl), 1.0):
        said.append(["Holding losers", f"{money(unrealised)} of losses still open now, across"
                                       f" {sum(1 for position in positions if position['unrealised'] < 0)} positions"])

    return said


def deepest_dip(pnl, value):
    """The furthest the running profit fell from its best: (USD, share of the account at that best, from, to)."""
    peak, peak_time, worst, worst_share, span = None, None, 0.0, 0.0, (None, None)

    for when, amount in pnl:
        if peak is None or amount > peak:
            peak, peak_time = amount, when

        fall = peak - amount

        if fall > worst:
            worst, span = fall, (peak_time, when)
            account = _value_at(value, peak_time)
            worst_share = fall / account if account > 0 else 0.0

    return worst, worst_share, span[0], span[1]


def _value_at(value, when):
    """The account's value at or just before [when]."""
    before = [amount for moment, amount in value if moment <= when]
    return before[-1] if before else (value[0][1] if value else 0.0)


def _window(series, start_ms):
    """[series] from [start_ms] on, measured from the first point in it."""
    inside = [(when, amount) for when, amount in series if when >= start_ms]

    if not inside:
        return []

    base = inside[0][1]
    return [(when, amount - base) for when, amount in inside]


def examine(client, row, chosen, now_ms):
    """One trader's record over the period, judged, with how they trade."""
    start_ms = now_ms - chosen["days"] * _DAY_MS
    fills = client.fills(row["address"], start_ms, now_ms)
    trades = trades_from(fills)

    history = client.portfolio(row["address"])
    period = history.get("allTime") or history.get("month") or {"pnl": [], "value": []}
    pnl = _window(period["pnl"], start_ms)
    value = [(when, amount) for when, amount in period["value"] if when >= start_ms]
    dip, dip_share, dip_from, dip_to = deepest_dip(pnl, value)
    period_pnl = pnl[-1][1] if pnl else 0.0

    record = judge(trades, curve_parts(pnl, start_ms, now_ms, chosen["parts"]), chosen["min_trades"])
    positions, account = client.positions(row["address"])

    # Hyperliquid keeps only an account's latest fills: the trade figures may start after the period does.
    covered_from = fills[0].get("time", start_ms) if fills else start_ms
    covered_days = max((now_ms - covered_from) / _DAY_MS, 1.0)

    return {
        "address": row["address"], "name": row["name"], "account": account or row["account"],
        "month_pnl": row["windows"].get("month", {}).get("pnl", 0.0),
        "month_roi": row["windows"].get("month", {}).get("roi", 0.0),
        "curve": [[when, round(amount, 2)] for when, amount in pnl],
        "trade_points": [[trade["time"], round(trade["pnl"] - trade["fee"], 2)] for trade in trades],
        "period_pnl": period_pnl,
        "dip": dip, "dip_share": dip_share, "dip_from": dip_from, "dip_to": dip_to,
        "fills_capped": len(fills) >= FILLS_KEPT, "covered_from": covered_from,
        "positions": [dict(position, coin=market(position["coin"])) for position in positions],
        **record,
        "style": style(fills, record, positions, covered_days, period_pnl),
    }


def candidates(rows, chosen):
    """The leaderboard's best recent performers among accounts big enough to mean something."""
    big = [row for row in rows if row["account"] >= chosen["min_account_usd"]
           and row["windows"].get("month", {}).get("volume", 0.0) > 0]
    big.sort(key=lambda row: -row["windows"].get("month", {}).get("pnl", 0.0))
    return big[:chosen["candidates"]]


_VERDICT_ORDER = {"consistent": 0, "one big trade": 1, "inconsistent": 2, "too few": 3}


def rank(traders):
    """Consistent records first, then by profit over the period."""
    return sorted(traders, key=lambda trader: (_VERDICT_ORDER.get(trader["verdict"], 9),
                                               -trader.get("period_pnl", trader["net"])))


# ---- the board -------------------------------------------------------------------------------

_lock = threading.Lock()
_refreshing = threading.Event()
_board_listener = None
_hide_listener = None
_say_listener = None


def set_listeners(on_board=None, on_hide=None, on_say=None):
    """Who is shown the board (a dict, with "focus" naming a trader or None), who puts it away, and who
    says what a refresh found when it finishes after the question was answered."""
    global _board_listener, _hide_listener, _say_listener

    if on_board is not None:
        _board_listener = on_board

    if on_hide is not None:
        _hide_listener = on_hide

    if on_say is not None:
        _say_listener = on_say


def refresh(client=None, now=None, report=None):
    """Read the exchange afresh and save the board. Returns it."""
    chosen = settings()
    client = client or Hyperliquid()
    now_ms = int((now or time.time()) * 1000)
    shortlist = candidates(client.leaderboard(), chosen)
    traders = []

    for index, row in enumerate(shortlist, 1):
        if report:
            report(f"{index}/{len(shortlist)} {row['name'] or row['address']}")

        try:
            traders.append(examine(client, row, chosen, now_ms))
        except WatchError as error:
            print(f"[JARVIS] trader watch: {row['address']} skipped ({error})")

    board = {
        "source": SOURCE, "updated": now_ms, "days": chosen["days"], "parts": chosen["parts"],
        "min_trades": chosen["min_trades"], "looked_at": len(shortlist), "traders": rank(traders),
    }

    path = _path(CACHE_NAME)

    if path:
        with _lock:
            _write(path, board)

    journal.write("traders", f"{SOURCE} board refreshed: {len(traders)} traders,"
                  f" {sum(1 for trader in traders if trader['verdict'] == 'consistent')} consistent")
    return board


def stale(board, now=None):
    if not board:
        return True

    age_hours = ((now or time.time()) * 1000 - board.get("updated", 0)) / 3_600_000
    return age_hours >= settings()["refresh_hours"]


def show(focus=None, client=None, wait=False):
    """Open the board and say what it holds. Returns the sentence.

    Reading twenty-five records takes a minute or more, far too long to hold
    a spoken answer. So a stale board is refreshed in the background: the
    last one is shown meanwhile, if there is one, and the fresh one replaces
    it and is announced when it lands. [wait] makes the refresh finish first.
    """
    board = cached()
    note = ""

    if stale(board):
        started = _refresh_in_background(focus, client)

        if wait and started:
            started.join()
            board = cached() or board
        elif not board:
            return (f"Reading the top {SOURCE} traders now, sir. The board opens when it's done,"
                    f" in a minute or two.")
        else:
            note = " Fresh figures are on their way."

    if not board:
        return f"I couldn't read {SOURCE}, sir."

    if focus is not None and not 1 <= focus <= len(board["traders"]):
        return f"There are {len(board['traders'])} traders on the board, sir."

    if _board_listener:
        _board_listener(dict(board, focus=focus))

    return describe(board, focus) + note


def _refresh_in_background(focus, client):
    """Start one refresh, unless one is already running; the thread, or None."""
    if _refreshing.is_set():
        return None

    _refreshing.set()

    def run():
        try:
            board = refresh(client)
        except WatchError as error:
            print(f"[JARVIS] trader watch: {error}")

            if _say_listener:
                _say_listener(f"I couldn't read {SOURCE} for the trader board, sir: {error}.")

            return
        finally:
            _refreshing.clear()

        shown = focus if focus is not None and 1 <= focus <= len(board["traders"]) else None

        if _board_listener:
            _board_listener(dict(board, focus=shown))

        if _say_listener:
            _say_listener(describe(board, shown))

    thread = threading.Thread(target=run, name="trader-watch", daemon=True)
    thread.start()
    return thread


def hide():
    if _hide_listener:
        _hide_listener()
        return True

    return False


def describe(board, focus=None):
    """A sentence or two for the voice; the panel carries the detail."""
    traders = board["traders"]

    if not traders:
        return f"None of the {SOURCE} traders I looked at could be read, sir."

    if focus is not None:
        trader = traders[focus - 1]
        how = ", ".join(name.lower() for name, _detail in (trader.get("style") or [])[:3])
        return (f"Trader {focus}, {short_name(trader)}: {trader['reason']}. Made {money(trader['period_pnl'])} over"
                f" {board['days']} days, deepest fall {money(trader['dip'])}." + (f" Style: {how}." if how else "")
                + " The working is on the board, sir.")

    good = [trader for trader in traders if trader["verdict"] == "consistent"]

    if not good:
        return (f"Of {len(traders)} top {SOURCE} traders, none made money in every part of the last"
                f" {board['days']} days with enough trades to judge, sir. The board shows why.")

    best = good[0]
    return (f"Of {len(traders)} top {SOURCE} traders, {len(good)} made money in every part of the last"
            f" {board['days']} days. The strongest, {short_name(best)}, made {money(best['period_pnl'])} over"
            f" {best['trades']} trades. Details on the board, sir.")


def short_name(trader):
    if trader.get("name"):
        return trader["name"]

    address = trader.get("address", "")
    return f"{address[:6]}...{address[-4:]}" if len(address) > 12 else address


def money(value):
    sign = "-" if value < 0 else ""
    value = abs(value)

    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:.2f}m"

    if value >= 10_000:
        return f"{sign}${value / 1000:.0f}k"

    return f"{sign}${value:,.0f}"


def updated_text(board):
    moment = datetime.fromtimestamp(board.get("updated", 0) / 1000, tz=timezone.utc)
    return moment.strftime("%d %b %H:%M UTC")


# ---- what is asked -------------------------------------------------------------------------

_TRADERS = re.compile(r"\b(?:(?:top|best|lead|copy)\s+traders?|traders?\s+(?:board|watch)|hyperliquid)\b")
_ONE = re.compile(r"\btrader\s+(?:number\s+)?(\w+(?:\s+\w+)?)")
_DISMISS = re.compile(r"\b(?:close|hide|dismiss|shut|put away)\b.*\b(?:traders?|trader board|hyperliquid)\b")


def wanted(command):
    text = (command or "").casefold()
    return bool(_TRADERS.search(text) or which(text))


def dismissed(command):
    return bool(_DISMISS.search((command or "").casefold()))


def which(command):
    """The trader asked about by number ("trader three", "trader number 2"), or None."""
    found = _ONE.search((command or "").casefold())

    if not found:
        return None

    return number_in(found.group(1).split())
