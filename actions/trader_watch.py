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
PAUSE_SECONDS = 0.35
TIMEOUT_SECONDS = 30
RETRIES = 3

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

            if response.status_code == 429 and attempt < RETRIES - 1:
                time.sleep(2 ** attempt * 2)
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


def judge(trades, start_ms, end_ms, parts, min_trades):
    """The record's figures, every one with the numbers it came from, so the panel can show the working."""
    nets = [trade["pnl"] - trade["fee"] for trade in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]
    gross_won, gross_lost = sum(wins), -sum(losses)
    net = sum(nets)

    span = (end_ms - start_ms) / parts
    by_part = [0.0] * parts

    for trade, value in zip(trades, nets):
        index = min(parts - 1, max(0, int((trade["time"] - start_ms) // span))) if span > 0 else 0
        by_part[index] += value

    best = max(nets) if nets else 0.0
    coins = {}

    for trade, value in zip(trades, nets):
        coins[trade["coin"]] = coins.get(trade["coin"], 0.0) + value

    enough = len(trades) >= min_trades
    every_part = all(value > 0 for value in by_part)
    concentrated = net > 0 and best / net > CONCENTRATED

    if not enough:
        verdict, reason = "too few", f"only {len(trades)} closed trades; {min_trades} needed to judge"
    elif not every_part:
        losing = [index + 1 for index, value in enumerate(by_part) if value <= 0]
        verdict = "inconsistent"
        reason = f"lost money in part{'s' if len(losing) > 1 else ''} {', '.join(map(str, losing))} of {parts}"
    elif concentrated:
        verdict, reason = "one big trade", f"one trade made {best / net:.0%} of the profit"
    else:
        verdict, reason = "consistent", f"made money in all {parts} parts, over {len(trades)} trades"

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
        "parts": by_part, "coins": sorted(coins.items(), key=lambda item: -abs(item[1]))[:5],
        "verdict": verdict, "reason": reason,
    }


def deepest_dip(pnl, value):
    """The furthest the running profit fell from its best, in USD and as a share of the account at that best."""
    peak, peak_time, worst, worst_share = None, None, 0.0, 0.0

    for when, amount in pnl:
        if peak is None or amount > peak:
            peak, peak_time = amount, when

        fall = peak - amount

        if fall > worst:
            worst = fall
            account = _value_at(value, peak_time)
            worst_share = fall / account if account > 0 else 0.0

    return worst, worst_share


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
    """One trader's record over the period, judged."""
    start_ms = now_ms - chosen["days"] * _DAY_MS
    fills = client.fills(row["address"], start_ms, now_ms)
    trades = trades_from(fills)
    record = judge(trades, start_ms, now_ms, chosen["parts"], chosen["min_trades"])

    history = client.portfolio(row["address"])
    period = history.get("allTime") or history.get("month") or {"pnl": [], "value": []}
    pnl = _window(period["pnl"], start_ms)
    value = [(when, amount) for when, amount in period["value"] if when >= start_ms]
    dip, dip_share = deepest_dip(pnl, value)

    positions, account = client.positions(row["address"])

    return {
        "address": row["address"], "name": row["name"], "account": account or row["account"],
        "month_pnl": row["windows"].get("month", {}).get("pnl", 0.0),
        "month_roi": row["windows"].get("month", {}).get("roi", 0.0),
        "curve": [[when, round(amount, 2)] for when, amount in pnl],
        "trade_points": [[trade["time"], round(trade["pnl"] - trade["fee"], 2)] for trade in trades],
        "dip": dip, "dip_share": dip_share,
        "fills_capped": len(fills) >= FILLS_KEPT,
        "positions": positions,
        **record,
    }


def candidates(rows, chosen):
    """The leaderboard's best recent performers among accounts big enough to mean something."""
    big = [row for row in rows if row["account"] >= chosen["min_account_usd"]
           and row["windows"].get("month", {}).get("volume", 0.0) > 0]
    big.sort(key=lambda row: -row["windows"].get("month", {}).get("pnl", 0.0))
    return big[:chosen["candidates"]]


_VERDICT_ORDER = {"consistent": 0, "one big trade": 1, "inconsistent": 2, "too few": 3}


def rank(traders):
    """Consistent records first, then by net profit over the period."""
    return sorted(traders, key=lambda trader: (_VERDICT_ORDER.get(trader["verdict"], 9), -trader["net"]))


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
        return (f"Trader {focus}, {short_name(trader)}: {trader['reason']}. Net {money(trader['net'])} over"
                f" {board['days']} days, deepest fall {money(trader['dip'])}. The working is on the board, sir.")

    good = [trader for trader in traders if trader["verdict"] == "consistent"]

    if not good:
        return (f"Of {len(traders)} top {SOURCE} traders, none made money in every part of the last"
                f" {board['days']} days with enough trades to judge, sir. The board shows why.")

    best = good[0]
    return (f"Of {len(traders)} top {SOURCE} traders, {len(good)} made money in every part of the last"
            f" {board['days']} days. The strongest, {short_name(best)}, netted {money(best['net'])} over"
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
