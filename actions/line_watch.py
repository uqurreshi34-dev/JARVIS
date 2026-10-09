"""Watching support and resistance: when price reaches one, JARVIS says so and opens the plan.

For the markets named in trade-plan.json's `alert_markets` (gold and silver unless changed), after
every fifteen-minute candle closes, the plan is read as "shall I buy or sell gold" reads it
(actions/trade_plan.py): the four-hour chart's support and resistance, and the fifteen-minute
candles. A finished candle reaching a line -- within `gold_strategy.BOUNCE_TOUCH_ATR` of the
four-hour ATR, as the backtest's bounce counts it -- is a hit, judged as the backtest judges it:

    bounce        closed back beyond the line, the bounce's colour, its wick on the line's side the
                  longer: buyers (at support) or sellers (at resistance) won the candle
    pushed back   closed back beyond the line, but the other wick the longer: the other side fought
                  back, so wait
    through       closed beyond the line: a break may be starting -- the four-hour close decides
    testing       on the line, neither

JARVIS says which, what to wait for each way, and where a stop would sit: beyond the candle's wick
by the plan's `stop_atr` of the fifteen-minute ATR, room for the noise around a line. A line is
said once when reached and once more if a bounce candle follows; it is said again only after price
has moved `alert_clear_atr` four-hour ATRs away from it. Nothing is traded: this watches and says.
"""

import threading
from datetime import datetime, timedelta, timezone

from actions import gold_strategy, oanda, trade_plan

# How long after a fifteen-minute candle closes before OANDA is asked for it.
SETTLE_SECONDS = 20

_said = {}            # (instrument, kind, line) -> the signals already said about that line
_last_seen = {}       # instrument -> the start (ms) of the last fifteen-minute candle judged
_say = None
_show = None
_stop = threading.Event()
_thread = None


def set_listeners(on_say=None, on_show=None):
    """on_say(text): speaks an unprompted announcement. on_show(found, spoken): opens the plan's panel."""
    global _say, _show
    _say, _show = on_say, on_show


def hit(found):
    """What the last finished fifteen-minute candle did at support or resistance, or None.

    {"kind": "support" or "resistance", "line", "signal", "candle": (open, high, low, close), "stop",
    "decimals"} -- the line it reached nearest, if it reached one.
    """
    entry = found.get("entry")

    if not entry or not entry.get("candles") or not found.get("atr"):
        return None

    _ms, opened, high, low, closed, _label = entry["candles"][-1]
    touch = gold_strategy.BOUNCE_TOUCH_ATR * found["atr"]
    room = found["settings"]["stop_atr"] * (entry.get("atr") or found["atr"])
    reached = [(kind, line) for kind, line in (("support", found.get("support")),
                                               ("resistance", found.get("resistance")))
               if line is not None and low <= line + touch and high >= line - touch]

    if not reached:
        return None

    kind, line = min(reached, key=lambda pair: abs(closed - pair[1]))
    side, wick_won = gold_strategy._reclaims((None, opened, high, low, closed),
                                             line if kind == "support" else None,
                                             line if kind == "resistance" else None, touch)

    if side:
        signal = "bounce" if wick_won else "pushed back"
    elif (kind == "support" and closed < line - touch) or (kind == "resistance" and closed > line + touch):
        signal = "through"
    else:
        signal = "testing"

    stop = low - room if kind == "support" else high + room
    return {"kind": kind, "line": line, "signal": signal, "candle": (opened, high, low, closed),
            "stop": stop, "decimals": found["decimals"]}


def words(found, reached):
    """What JARVIS says about [reached], a hit(): where, what the candle showed, what to wait for, the stop."""
    d = reached["decimals"]
    places = 0 if reached["line"] >= 1000 else d

    def price(value):
        return f"{value:,.{places}f}"

    support = reached["kind"] == "support"
    line, stop = price(reached["line"]), price(reached["stop"])
    back, beyond = ("above", "below") if support else ("below", "above")
    near_wick, far_wick = ("lower", "upper") if support else ("upper", "lower")
    winners, others = ("buyers", "sellers") if support else ("sellers", "buyers")
    bounce_side, break_side = ("buy", "sell") if support else ("sell", "buy")
    timeframe = found.get("entry_timeframe") or "15-minute"
    opening = f"{found['instrument']} has reached {reached['kind']} at {line}, sir."
    signal = reached["signal"]

    if signal == "bounce":
        middle = (f" That {timeframe} candle closed back {back} it with its {near_wick} wick the longer: {winners}"
                  f" took it back -- the bounce the rule looks for. A {bounce_side} would put its stop at {stop},"
                  f" {'under' if support else 'over'} the wick with room.")
    elif signal == "pushed back":
        middle = (f" That {timeframe} candle closed back {back} it, but with its {far_wick} wick the longer: {others}"
                  f" pushed back, so wait. A bounce is a {timeframe} candle closing {back} {line} with its"
                  f" {near_wick} wick the longer; its stop would sit {beyond} that wick with room, about {stop} from"
                  f" here.")
    elif signal == "through":
        middle = (f" That {timeframe} candle closed {beyond} it. A {break_side} needs a {found['timeframe']} close"
                  f" {beyond} {line}, then the retest; until then it may yet bounce.")
    else:
        middle = (f" Wait for a {timeframe} candle to close back {back} {line} with its {near_wick} wick the longer"
                  f" -- a long {far_wick} wick means {others} pushed back, so wait. A stop would sit {beyond} the"
                  f" wick with room, about {stop} from here. A {found['timeframe']} close {beyond} it would be a"
                  f" break instead.")

    return opening + middle + " The plan is on the panel."


def _armed(instrument, reached, found):
    """Whether [reached] is worth saying: a line first reached, or a bounce after it was. Clears lines price
    has left."""
    clear = trade_plan.settings().get("alert_clear_atr", 1.0) * found["atr"]
    closed = reached["candle"][3] if reached else found["entry"]["candles"][-1][4]

    for key in [key for key in _said if key[0] == instrument]:
        if abs(closed - key[2]) > clear:
            del _said[key]

    if not reached:
        return False

    key = (instrument, reached["kind"], round(reached["line"], reached["decimals"]))
    told = _said.setdefault(key, set())

    if not told or (reached["signal"] == "bounce" and "bounce" not in told):
        told.add(reached["signal"])
        return True

    return False


def check(client=None):
    """One look at every watched market: say and show any line reached since the last look. Returns what was
    said, a list of sentences."""
    chosen = trade_plan.settings()
    said = []

    for spoken in chosen.get("alert_markets") or ():
        try:
            found = trade_plan.read(spoken, client)
        except trade_plan.PlanError as error:
            print(f"[JARVIS] line watch: {spoken} could not be read ({error})")
            continue

        entry = found.get("entry")

        if not entry or not entry.get("candles") or not found.get("live"):
            continue

        latest = entry["candles"][-1][0]

        if _last_seen.get(found["symbol"]) == latest:
            continue

        _last_seen[found["symbol"]] = latest
        reached = hit(found)

        if not _armed(found["symbol"], reached, found):
            continue

        sentence = words(found, reached)
        found["alert"] = sentence
        said.append(sentence)

        if _show:
            _show(found, spoken)

        if _say:
            _say(sentence)

    return said


def next_look(now):
    """When to look next: [SETTLE_SECONDS] after the next fifteen-minute close."""
    boundary = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 15) + timedelta(minutes=15)
    return boundary + timedelta(seconds=SETTLE_SECONDS)


def _run():
    while not _stop.is_set():
        now = datetime.now(timezone.utc)
        wait = (next_look(now) - now).total_seconds()

        if _stop.wait(max(1.0, wait)):
            return

        try:
            check()
        except Exception as error:   # noqa: BLE001 - a bad look must never stop the watching
            print(f"[JARVIS] line watch: {error}")


def start():
    """Watch in the background while JARVIS runs, if OANDA is set up and a market is named."""
    global _thread

    if _thread or not oanda.configured() or not trade_plan.settings().get("alert_markets"):
        return False

    _stop.clear()
    _thread = threading.Thread(target=_run, name="line-watch", daemon=True)
    _thread.start()
    return True


def stop():
    global _thread
    _stop.set()
    _thread = None
