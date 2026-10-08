"""Should I buy or sell gold, silver, oil...? The plan, worked out on the chart, with every figure shown.

Asked "shall I buy or sell gold" -- or silver, oil, copper, any market in the
"markets" setting -- JARVIS does not guess where the price goes. It reads
OANDA's four-hour candles for that market and lays out the rules this trading
is done by -- a line breaks, price comes back to test it, a candle confirms --
with the exact prices for each step and where it stands on each:

    the lines     support and resistance found from the chart itself: a swing
                  high is a candle higher than the [swing_strength] either side
                  of it (a swing low, lower) that price then clearly turned from,
                  moving [bounce_atr] ATRs away; swings within [merge_atr] ATRs of
                  each other are one line, a line needs [min_touches] of them,
                  and the more swings at a line, the more it has been
                  respected. Your own lines (your_levels, by market) are drawn
                  and used alongside.
    the plans     one to buy and one to sell. Buying: a candle closes above
                  the resistance (the break), price comes back down to it (the
                  retest), and a candle closes back above it (the confirmation)
                  with RSI above 50 -- momentum with buyers -- but under
                  rsi_high, not already overbought. Selling is the mirror image.
                  A break more than [retest_candles] candles old with no retest
                  has run away, and is not chased.
    the figures   entry at the confirmation's close (the line, until then); the
                  stop beyond the retest's wick by [stop_atr] ATRs (beyond the
                  line, until a wick exists); the target [target_buffer_atr]
                  ATRs short of the next line, so price need not reach the line
                  itself -- in ATRs, so it suits gold and natural gas alike;
                  reward : risk worked out, and under [min_reward] the plan
                  says skip. With no line in the way, the target is
                  [fallback_reward] times the risk.

Every market is read the same way; only its prices differ, written to OANDA's
own decimal places for it. Rules, not a forecast: it says what has to happen
before a trade and where, never that it will. The answer opens in its own
panel (trade_plan_panel.py): the chart with the lines and each plan's path
drawn on it, RSI beneath, the plans step by step, and the working. Read-only:
it reads candles and the price, and places nothing.

Settings in trade-plan.json in the JARVIS folder:

    markets              {"gold": "XAU_USD", "silver": "XAG_USD", ...}: what you call a
                         market, and OANDA's name for it; add any instrument OANDA offers
    your_levels          {"XAU_USD": [4114, 4229]}: your own lines, by OANDA's name
    entry_granularity    "M15": a second page timing the trade on these candles against the
                         lines above (null: none); entry_candles, entry_shown_candles,
                         entry_retest_candles as for the chart's own
    granularity, utc_candles, mid_prices, candles, shown_candles, swing_strength, merge_atr, min_touches, bounce_atr,
    stop_atr, target_buffer_atr, min_reward, fallback_reward, retest_candles,
    touch_atr, rsi_high, rsi_low
"""

import json
import os
import re
from datetime import timedelta, timezone

from actions import chart_lines, files, gold_strategy, oanda


SETTINGS_NAME = "trade-plan.json"

DEFAULTS = {
    # What you call each market, and OANDA's name for it. Longer names are matched first, so
    # "crude oil" is not read as "oil" alone; add any instrument your OANDA account offers.
    "markets": {
        "gold": "XAU_USD", "silver": "XAG_USD", "platinum": "XPT_USD", "palladium": "XPD_USD",
        "copper": "XCU_USD", "oil": "WTICO_USD", "crude oil": "WTICO_USD", "crude": "WTICO_USD",
        "us oil": "WTICO_USD", "wti": "WTICO_USD", "brent": "BCO_USD", "brent crude": "BCO_USD",
        "natural gas": "NATGAS_USD", "nat gas": "NATGAS_USD", "corn": "CORN_USD", "wheat": "WHEAT_USD",
        "soybeans": "SOYBN_USD", "soya beans": "SOYBN_USD", "sugar": "SUGAR_USD",
    },
    # OANDA's candle size, and how many are read and shown.
    "granularity": "H4",
    # false: OANDA's own day, starting 17:00 New York time -- the four-hour candles TradingView draws for
    # OANDA (in summer 21:00, 01:00, 05:00 ... UTC). true: on UTC's hours (00:00, 04:00 ...) instead.
    "utc_candles": False,
    # true: prices halfway between bid and ask, as OANDA's own chart (trade.oanda.com) draws them, so a
    # candle here is the same candle there, colour and all; false: the bid, as the gold trader trades.
    "mid_prices": True,
    "candles": 200,
    "shown_candles": 60,
    # The second page: the same plan timed on shorter candles, against the lines drawn on the chart above
    # -- the lines from four-hour candles, the moment from fifteen-minute ones. null: no second page.
    "entry_granularity": "M15",
    "entry_candles": 200,
    "entry_shown_candles": 96,
    "entry_retest_candles": 12,
    # A swing high is higher than this many candles either side of it.
    "swing_strength": 3,
    # Swings this many ATRs apart or closer are one line, and a line needs this many swings: price
    # turning at a price once is a swing, turning there again makes it a line.
    "merge_atr": 0.5,
    "min_touches": 2,
    # A swing counts only when price clearly turned from it: moved at least this many ATRs away within
    # twice swing_strength candles. Dips that barely lift are not where traders draw their lines.
    "bounce_atr": 1.0,
    # Your own lines, by OANDA's name for the market: always drawn and used.
    "your_levels": {},
    # The stop: beyond the retest's wick by this many ATRs.
    "stop_atr": 1.0,
    # The target: this many ATRs short of the next line (on gold, about $3).
    "target_buffer_atr": 0.2,
    # Under this reward : risk, the plan says skip.
    "min_reward": 2.0,
    # With no line in the way, the target is this many times the risk.
    "fallback_reward": 3.0,
    # A break older than this many candles, never retested, has run away.
    "retest_candles": 6,
    # Within this many ATRs of the line counts as reaching it.
    "touch_atr": 0.3,
    # RSI above this is overbought, below rsi_low oversold.
    "rsi_high": 70,
    "rsi_low": 30,
}

_INSTRUMENT = re.compile(r"^[A-Z0-9]+_[A-Z0-9]+$")

GRANULARITY_SECONDS = {"M15": 900, "M30": 1800, "H1": 3600, "H2": 7200, "H4": 14400, "H8": 28800, "D": 86400}
GRANULARITY_WORDS = {"M15": "15-minute", "M30": "30-minute", "H1": "1-hour", "H2": "2-hour", "H4": "4-hour",
                     "H8": "8-hour", "D": "daily"}

# RSI's middle: above it momentum is with buyers, below it with sellers.
RSI_MIDDLE = 50


class PlanError(Exception):
    """The chart could not be read."""


# ---- settings ------------------------------------------------------------------------------

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
            print(f"[JARVIS] trade plan: {SETTINGS_NAME} could not be read ({error}); using the defaults")
            saved = {}

    chosen = dict(DEFAULTS)

    for name, default in DEFAULTS.items():
        value = saved.get(name, default)

        if name == "granularity":
            chosen[name] = value if value in GRANULARITY_SECONDS else default
        elif name == "entry_granularity":
            chosen[name] = value if value is None or value in GRANULARITY_SECONDS else default
        elif name == "markets":
            given = value if isinstance(value, dict) else {}
            chosen[name] = {" ".join(str(spoken).casefold().split()): instrument
                            for spoken, instrument in given.items()
                            if isinstance(instrument, str) and _INSTRUMENT.match(instrument) and str(spoken).strip()}
            chosen[name] = chosen[name] or dict(default)
        elif name in ("utc_candles", "mid_prices"):
            chosen[name] = value if isinstance(value, bool) else default
        elif name == "your_levels":
            given = value if isinstance(value, dict) else {}
            chosen[name] = {instrument: [float(level) for level in levels
                                         if isinstance(level, (int, float)) and not isinstance(level, bool)
                                         and level > 0]
                            for instrument, levels in given.items()
                            if isinstance(instrument, str) and isinstance(levels, list)}
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            chosen[name] = default
        else:
            chosen[name] = type(default)(value)

    chosen["shown_candles"] = min(chosen["shown_candles"], chosen["candles"])
    chosen["entry_shown_candles"] = min(chosen["entry_shown_candles"], chosen["entry_candles"])
    chosen["rsi_low"], chosen["rsi_high"] = sorted((chosen["rsi_low"], chosen["rsi_high"]))

    if path and not os.path.exists(path):
        temporary = f"{path}.part"

        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(chosen, handle, indent=1)

        os.replace(temporary, path)

    return chosen


# ---- the lines -------------------------------------------------------------------------------

def levels(candles, atr, chosen, yours=()):
    """The lines, lowest first, as actions/chart_lines.py finds them with these settings."""
    found = chart_lines.levels(candles, chosen["merge_atr"] * atr, chosen["swing_strength"], chosen["min_touches"],
                               yours, chosen["bounce_atr"] * atr)

    # Where price turned, by time rather than by place in this list, so any chart can mark them.
    return [dict(line, swings=[[_ms(candles[index][0]), price] for index, price in line["swings"]])
            for line in found]


# ---- a plan for one side ----------------------------------------------------------------------

def _beyond(value, line, side):
    """Whether [value] is past [line] on the side a trade of [side] wants: above for a buy."""
    return value > line if side == "buy" else value < line


def _break_of(candles, line, side, window):
    """The index of a recent close through [line] that has held since, or None."""
    closes = [candle[4] for candle in candles]
    first = max(1, len(candles) - window)

    for index in range(len(candles) - 1, first - 1, -1):
        if _beyond(closes[index], line, side) and not _beyond(closes[index - 1], line, side):
            held = all(_beyond(close, line, side) for close in closes[index:])
            return index if held else None

    return None


def _retest_of(candles, line, side, broke, touch):
    """The first candle after the break to come back to [line], or None."""
    for index in range(broke + 1, len(candles)):
        reach = candles[index][3] if side == "buy" else candles[index][2]

        if (reach <= line + touch) if side == "buy" else (reach >= line - touch):
            return index

    return None


def _confirmation_of(candles, line, side, retest):
    """The first candle from the retest on to close back beyond [line] in the trade's direction, or None."""
    for index in range(retest, len(candles)):
        _start, opened, _high, _low, closed = candles[index][:5]

        if _beyond(closed, line, side) and (closed > opened if side == "buy" else closed < opened):
            return index

    return None


def side_plan(side, line, candles, atr, rsis, lines, chosen, broke=None, decimals=2, line_atr=None):
    """The plan for one side at [line]: where it stands, its steps, and its figures with their working.

    Prices are written to [decimals] places, the market's own. [atr] is these candles' own, for the
    retest's reach and the stop; [line_atr], the ATR of the chart the lines came from (these candles',
    unless given), for how far short of a line the target sits and how far apart lines are.
    """
    d = decimals
    line_atr = line_atr or atr
    touch = chosen["touch_atr"] * atr
    buffer = chosen["target_buffer_atr"] * line_atr
    word = "above" if side == "buy" else "below"
    last = len(candles) - 1
    retest = _retest_of(candles, line["price"], side, broke, touch) if broke is not None else None
    confirmed = _confirmation_of(candles, line["price"], side, retest) if retest is not None else None
    price = line["price"]

    if confirmed is not None:
        stage = "ready" if confirmed == last else "passed"
    elif retest is not None:
        stage = "confirm"
    elif broke is not None:
        stage = "retest"
    else:
        stage = "break"

    steps = [
        {"name": "Break", "done": broke is not None,
         "text": f"a {GRANULARITY_WORDS[chosen['granularity']]} candle closes {word} {price:,.{d}f}"},
        {"name": "Retest", "done": retest is not None,
         "text": f"price comes back to {price:,.{d}f} (within {touch:,.{d}f})"},
        {"name": "Confirmation", "done": confirmed is not None,
         "text": f"a candle closes back {word} {price:,.{d}f}, {'green' if side == 'buy' else 'red'}, with RSI"
                 + (f" between {RSI_MIDDLE} and {chosen['rsi_high']}" if side == "buy"
                    else f" between {chosen['rsi_low']} and {RSI_MIDDLE}")},
    ]

    # The figures: from the candles once they exist, from the line until then.
    sign = 1 if side == "buy" else -1
    entry = candles[confirmed][4] if confirmed is not None else price
    entry_working = (f"the confirmation candle's close, {entry:,.{d}f}" if confirmed is not None
                     else f"at the line, {price:,.{d}f}, once confirmed")

    if retest is not None:
        tested = candles[retest:(confirmed if confirmed is not None else retest) + 1]
        wick = min(candle[3] for candle in tested) if side == "buy" else max(candle[2] for candle in tested)
        stop = wick - sign * chosen["stop_atr"] * atr
        stop_working = (f"the retest's wick {wick:,.{d}f} {'-' if side == 'buy' else '+'} {chosen['stop_atr']:g} ATR"
                        f" ({chosen['stop_atr'] * atr:,.{d}f}) = {stop:,.{d}f}")
    else:
        stop = price - sign * chosen["stop_atr"] * atr
        stop_working = (f"the line {price:,.{d}f} {'-' if side == 'buy' else '+'} {chosen['stop_atr']:g} ATR"
                        f" ({chosen['stop_atr'] * atr:,.{d}f}) = {stop:,.{d}f}; beyond the retest's wick once it shows")

    risk = abs(entry - stop)
    # The first line in the trade's way: any line past the entry (other than the one traded), however
    # close -- one right under a sell's entry leaves it no room at all, and the plan must say so rather
    # than aim through it at a line further on.
    ahead = [other for other in lines if _beyond(other["price"], entry, side)
             and abs(other["price"] - price) > chosen["merge_atr"] * line_atr]
    following = min(ahead, key=lambda other: abs(other["price"] - entry)) if ahead else None
    kind = "resistance" if side == "buy" else "support"

    if following and not _beyond(following["price"] - sign * buffer, entry, side):
        target = entry
        target_working = (f"no room: the next {kind}, {following['price']:,.{d}f}, is within"
                          f" {chosen['target_buffer_atr']:g} ATR ({buffer:,.{d}f}) of the entry, {entry:,.{d}f}")
    elif following:
        target = following["price"] - sign * buffer
        target_working = (f"the next {kind} {following['price']:,.{d}f}"
                          f" {'-' if side == 'buy' else '+'} {chosen['target_buffer_atr']:g} ATR ({buffer:,.{d}f})"
                          f" = {target:,.{d}f}")
    else:
        target = entry + sign * chosen["fallback_reward"] * risk
        target_working = (f"no line {word} in the last {len(candles)} candles: {chosen['fallback_reward']:g} times"
                          f" the risk, {entry:,.{d}f} {'+' if side == 'buy' else '-'} {chosen['fallback_reward']:g} x"
                          f" {risk:,.{d}f} = {target:,.{d}f}")

    reward = sign * (target - entry)
    ratio = reward / risk if risk > 0 else 0.0
    rsi_now = rsis[confirmed] if confirmed is not None else None
    rsi_ok = None

    if rsi_now is not None:
        rsi_ok = (RSI_MIDDLE < rsi_now < chosen["rsi_high"]) if side == "buy" else \
            (chosen["rsi_low"] < rsi_now < RSI_MIDDLE)

    return {
        "side": side, "line": price, "touches": line["touches"], "yours": line["yours"], "stage": stage,
        "broke_at": candles[broke][0] if broke is not None else None,
        "steps": steps, "entry": round(entry, d), "entry_working": entry_working,
        "stop": round(stop, d), "stop_working": stop_working,
        "target": round(target, d), "target_working": target_working, "next_line": following["price"] if following else None,
        "risk": round(risk, d), "reward": round(reward, d), "decimals": d, "ratio": round(ratio, 2),
        "ratio_working": f"({abs(target - entry):,.{d}f}) / ({risk:,.{d}f}) = {ratio:.2f} : 1",
        "worth": round(ratio, 2) >= chosen["min_reward"], "rsi": rsi_now, "rsi_ok": rsi_ok,
        "no_room": bool(following) and reward <= 0, "kind_ahead": kind,
        "confirmed": confirmed, "passed": None,
    }


def _choose_line(side, price, lines, candles, chosen):
    """The line a [side] plan trades: one broken recently and holding, else the next one to break."""
    window = chosen["retest_candles"]
    behind = [line for line in lines if not _beyond(line["price"], price, side) or line["price"] == price]
    behind.sort(key=lambda line: abs(line["price"] - price))

    for line in behind:
        broke = _break_of(candles, line["price"], side, window)

        if broke is not None:
            return line, broke

    ahead = [line for line in lines if _beyond(line["price"], price, side)]
    return (min(ahead, key=lambda line: abs(line["price"] - price)), None) if ahead else (None, None)


# ---- the whole plan ------------------------------------------------------------------------------

def rsi_zone(value, chosen):
    if value is None:
        return "not enough candles to read"

    if value >= chosen["rsi_high"]:
        return "overbought: buyers may be spent, so a buy now is late"

    if value <= chosen["rsi_low"]:
        return "oversold: sellers may be spent, so a sell now is late -- and not a buy on its own"

    if value >= RSI_MIDDLE:
        return "above 50: momentum is with buyers"

    return "below 50: momentum is with sellers"


def _ms(moment):
    return int(moment.timestamp() * 1000)


def _uk(moment, form="%d %b %H:%M"):
    return gold_strategy.uk_time(moment.astimezone(timezone.utc)).strftime(form)


def plan(candles, price, chosen, market, live=True, lines=None, line_atr=None, lines_from=None):
    """The plan from [candles] (oldest first, finished ones only) and the price now.

    [market]: {"name", "instrument", "decimals"} -- what it is called, OANDA's name, and its decimal places.
    [lines], [line_atr] and [lines_from]: lines drawn on another chart, its ATR and its timeframe's words --
    the four-hour chart's lines, timed on fifteen-minute candles -- instead of lines from these candles.
    """
    d = market["decimals"]
    if len(candles) < max(30, 2 * chosen["swing_strength"] + 2):
        raise PlanError("too few candles to read the chart")

    atrs = gold_strategy.average_true_range(candles)
    rsis = gold_strategy.rsi([candle[4] for candle in candles])
    atr = atrs[-1] or (sum(candle[2] - candle[3] for candle in candles[-14:]) / 14)
    if lines is None:
        lines = levels(candles, atr, chosen, chosen["your_levels"].get(market["instrument"], ()))
    else:
        # Their swings were on the other chart's candles: no index here.
        lines = [dict(line, last=-1) for line in lines]

    resistance = min((line for line in lines if line["price"] > price), key=lambda line: line["price"], default=None)
    support = max((line for line in lines if line["price"] < price), key=lambda line: line["price"], default=None)

    plans = {}

    for side in ("buy", "sell"):
        line, broke = _choose_line(side, price, lines, candles, chosen)
        plans[side] = side_plan(side, line, candles, atr, rsis, lines, chosen, broke, d, line_atr) if line else None

        # A setup that has been and gone is history: the plan is for the next line to break, with the
        # one that passed kept beside it.
        if plans[side] and plans[side]["stage"] == "passed":
            gone = plans[side]
            ahead = [other for other in lines if _beyond(other["price"], max(price, gone["line"]) if side == "buy"
                                                         else min(price, gone["line"]), side)]
            following = min(ahead, key=lambda other: abs(other["price"] - price)) if ahead else None
            plans[side] = (side_plan(side, following, candles, atr, rsis, lines, chosen, None, d, line_atr) if following
                           else None)
            passed = {"line": gone["line"], "entry": gone["entry"], "stop": gone["stop"], "target": gone["target"],
                      "ratio": gone["ratio"], "worth": gone["worth"], "rsi": gone["rsi"], "rsi_ok": gone["rsi_ok"],
                      "when": _uk(candles[gone["confirmed"]][0] + timedelta(
                          seconds=GRANULARITY_SECONDS[chosen["granularity"]]))}

            if plans[side]:
                plans[side]["passed"] = passed
            else:
                plans[side + "_passed"] = passed

    verdict, headline = _verdict(plans, price, support, resistance, chosen, market)
    step = timedelta(seconds=GRANULARITY_SECONDS[chosen["granularity"]])
    next_close = candles[-1][0] + 2 * step
    shown = candles[-chosen["shown_candles"]:]
    first_shown = len(candles) - len(shown)

    return {
        "instrument": market["name"], "symbol": market["instrument"], "decimals": d,
        "granularity": chosen["granularity"], "timeframe": GRANULARITY_WORDS[chosen["granularity"]],
        "candle_seconds": GRANULARITY_SECONDS[chosen["granularity"]],
        "price": round(price, d), "live": live, "lines_from": lines_from,
        "line_atr": round(line_atr, d) if line_atr else None,
        "read": _uk(candles[-1][0] + step), "next_close": _uk(next_close, "%H:%M"),
        "read_utc": (candles[-1][0] + step).astimezone(timezone.utc).strftime("%H:%M UTC"),
        "atr": round(atr, d), "rsi": rsis[-1], "rsi_zone": rsi_zone(rsis[-1], chosen),
        "rsi_high": chosen["rsi_high"], "rsi_low": chosen["rsi_low"],
        "candles": [[_ms(start), opened, high, low, closed, _uk(start)]
                    for start, opened, high, low, closed in shown],
        "rsis": [round(value, 1) if value is not None else None for value in rsis[first_shown:]],
        "levels": [dict(line, last=max(-1, line["last"] - first_shown)) for line in lines],
        "support": support["price"] if support else None, "resistance": resistance["price"] if resistance else None,
        "buy": plans["buy"], "sell": plans["sell"],
        "buy_passed": plans.get("buy_passed"), "sell_passed": plans.get("sell_passed"),
        "verdict": verdict, "headline": headline,
        "settings": {name: chosen[name] for name in ("swing_strength", "merge_atr", "min_touches", "bounce_atr", "stop_atr",
                                                     "target_buffer_atr", "min_reward", "retest_candles",
                                                     "touch_atr")},
    }


def _waiting_for(side_plan_):
    said = _stage_words(side_plan_)

    if side_plan_["stage"] != "passed" and side_plan_.get("no_room"):
        said += f" -- though the next {side_plan_['kind_ahead']} leaves no room to a target, so not worth taking"
    elif side_plan_["stage"] != "passed" and not side_plan_["worth"]:
        said += f" -- though only {side_plan_['ratio']:.2f} : 1 to the next line, so not worth taking"

    gone = side_plan_.get("passed")

    if gone:
        d = side_plan_["decimals"]
        said += f" (the last {side_plan_['side']} setup, at {gone['line']:,.{d}f}, came and went at {gone['when']} UK)"

    return said


def _stage_words(side_plan_):
    d = side_plan_["decimals"]
    line = f"{side_plan_['line']:,.{d}f}"

    if side_plan_["stage"] == "break":
        return f"a {'close above' if side_plan_['side'] == 'buy' else 'close below'} {line}"

    if side_plan_["stage"] == "retest":
        return f"price to come back to {line}, which it broke {'up' if side_plan_['side'] == 'buy' else 'down'} through"

    if side_plan_["stage"] == "confirm":
        return f"a candle to close back {'above' if side_plan_['side'] == 'buy' else 'below'} {line} after its retest"

    return f"the next break: the last setup at {line} has passed"


def _verdict(plans, price, support, resistance, chosen, market):
    d = market["decimals"]

    for side in ("buy", "sell"):
        found = plans[side]

        if not found or found["stage"] != "ready":
            continue

        if found["no_room"]:
            return "skip", (f"A {side} setup confirmed at {found['line']:,.{d}f}, but {found['kind_ahead']} at"
                            f" {found['next_line']:,.{d}f} sits right {'above' if side == 'buy' else 'under'} the"
                            f" entry: no room to a target, so the rules say skip it.")

        if not found["worth"]:
            return "skip", (f"A {side} setup confirmed at {found['line']:,.{d}f}, but only {found['ratio']:.2f} : 1 to"
                            f" the next line, under {chosen['min_reward']:g} : 1: the rules say skip it.")

        if not found["rsi_ok"]:
            return "skip", (f"A {side} setup confirmed at {found['line']:,.{d}f}, but RSI is {found['rsi']:.0f}, outside"
                            f" the range the rules want: skip it.")

        return side, (f"{side.title()} setup confirmed at {found['line']:,.{d}f}: in near {found['entry']:,.{d}f}, stop"
                      f" {found['stop']:,.{d}f}, target {found['target']:,.{d}f}, {found['ratio']:.2f} : 1.")

    if support and resistance:
        where = f"between support {support['price']:,.{d}f} and resistance {resistance['price']:,.{d}f}"
    elif support:
        where = f"above support {support['price']:,.{d}f}, with no line above it in the candles read"
    elif resistance:
        where = f"below resistance {resistance['price']:,.{d}f}, with no line under it in the candles read"
    else:
        where = f"at {price:,.{d}f}, with no line either side in the candles read"

    waits = [f"{side.title()} plan: wait for {_waiting_for(plans[side])}." for side in ("buy", "sell") if plans[side]]
    return "wait", " ".join([f"Nothing to do yet: {market['name']} is {where}."] + waits)


# ---- reading and showing --------------------------------------------------------------------------

_listener = None
_hide_listener = None


def set_listeners(on_plan=None, on_hide=None):
    """Who is shown the plan (a dict), and who puts it away."""
    global _listener, _hide_listener

    if on_plan is not None:
        _listener = on_plan

    if on_hide is not None:
        _hide_listener = on_hide


def _said(command):
    return " ".join(re.findall(r"[a-z0-9]+", (command or "").casefold()))


def _names_pattern(chosen):
    """Every market's name, longest first, as one pattern."""
    names = sorted(chosen["markets"], key=len, reverse=True)
    return "(?:" + "|".join(r"\s+".join(map(re.escape, name.split())) for name in names) + ")"


def which(command, chosen=None):
    """The market named in [command]: (what was said, OANDA's name), or None. The longest name wins."""
    chosen = chosen or settings()
    found = re.search(rf"\b{_names_pattern(chosen)}\b", _said(command))

    if not found:
        return None

    spoken = " ".join(found.group(0).split())
    return spoken, chosen["markets"][spoken]


def known(chosen=None):
    """One name for each market that can be read, in the settings' order."""
    chosen = chosen or settings()
    names, seen = [], set()

    for spoken, instrument in chosen["markets"].items():
        if instrument not in seen:
            seen.add(instrument)
            names.append(spoken)

    return names


def read(command, client=None):
    """The plan now for the market named in [command], read from OANDA."""
    chosen = settings()
    named = which(command, chosen)

    if not named:
        raise PlanError("no market I know was named")

    spoken, instrument = named

    if client is None:
        if not oanda.configured():
            raise PlanError("OANDA isn't set up, so no chart can be read")

        client = oanda.Client()

    try:
        details = client.instrument(instrument)
        candles, _asks = client.candles(instrument, chosen["granularity"], chosen["candles"],
                                        align_utc=chosen["utc_candles"], mid=chosen["mid_prices"])
    except oanda.OandaError as error:
        raise PlanError(str(error)) from None

    if not candles:
        raise PlanError("OANDA sent no candles")

    try:
        bid, ask, _when = client.price(instrument)
        price, live = (bid + ask) / 2, True
    except oanda.OandaError:
        price, live = candles[-1][4], False

    market = {"name": details.get("display_name") or spoken.title(), "instrument": instrument,
              "decimals": int(details.get("price_decimals", 2)), "spoken": spoken}
    found = plan(candles, price, chosen, market, live)
    found["forming"] = _forming(client, instrument, chosen["granularity"], chosen["utc_candles"],
                                chosen["mid_prices"])
    found["entry"], found["entry_error"] = None, None
    found["entry_timeframe"] = GRANULARITY_WORDS.get(chosen["entry_granularity"])

    if chosen["entry_granularity"]:
        try:
            found["entry"] = entry_plan(client, found, chosen, market, price, live)
        except (oanda.OandaError, PlanError) as error:
            found["entry_error"] = str(error)

    return found


def entry_chosen(chosen):
    """The settings for the second page: the entry candles in place of the chart's own."""
    return dict(chosen, granularity=chosen["entry_granularity"], candles=chosen["entry_candles"],
                shown_candles=chosen["entry_shown_candles"], retest_candles=chosen["entry_retest_candles"])


def entry_plan(client, found, chosen, market, price, live):
    """The same plan on the entry candles (fifteen-minute, unless set otherwise), against [found]'s lines.

    The longer chart draws the lines; the shorter one says when price breaks, retests and confirms --
    sooner, with a stop sized by its own, smaller ATR, at the cost of more false starts.
    """
    shorter = entry_chosen(chosen)
    candles, _asks = client.candles(market["instrument"], shorter["granularity"], shorter["candles"],
                                    align_utc=chosen["utc_candles"], mid=chosen["mid_prices"])

    if not candles:
        raise PlanError(f"OANDA sent no {GRANULARITY_WORDS[shorter['granularity']]} candles")

    shown = plan(candles, price, shorter, market, live, lines=found["levels"], line_atr=found["atr"],
                 lines_from=found["timeframe"])
    shown["forming"] = _forming(client, market["instrument"], shorter["granularity"], chosen["utc_candles"],
                                chosen["mid_prices"])
    return shown


def _forming(client, instrument, granularity, align_utc, mid):
    """The candle still forming, drawn after the finished ones so the chart reaches now; never judged on.

    None when OANDA has none open (the market shut) or cannot say.
    """
    try:
        candle = client.forming(instrument, granularity, align_utc=align_utc, mid=mid)
    except oanda.OandaError:
        return None

    if not candle:
        return None

    start, opened, high, low, closed = candle
    return [_ms(start), opened, high, low, closed, _uk(start)]


def show(command, client=None):
    """Open the plan for the market asked about and say it briefly. Returns the sentence."""
    chosen = settings()
    named = which(command, chosen)

    if not named:
        return (f"Which market, sir? I can read {', '.join(known(chosen))}; more can be added in"
                f" {SETTINGS_NAME}.")

    try:
        found = read(command, client)
    except PlanError as error:
        return f"I couldn't read the {named[0]} chart, sir: {error}."

    global _last
    _last = (command, client)

    if _listener:
        _listener(found)

    return describe(found)


_last = None


def refresh():
    """Read the plan last shown again and show it, saying nothing: the panel keeps itself current while open.

    Returns True when a fresh plan was shown.
    """
    if not _last or not _listener:
        return False

    try:
        found = read(*_last)
    except PlanError as error:
        print(f"[JARVIS] trade plan: refresh failed ({error})")
        return False

    found["refreshed"] = True
    _listener(found)
    return True


def hide():
    if _hide_listener:
        _hide_listener()
        return True

    return False


def describe(found):
    """A few sentences for the voice; the panel carries the chart and the figures."""
    places = 0 if found["price"] >= 1000 else found["decimals"]
    price = f"{found['price']:,.{places}f}" + ("" if found["live"] else ", the market closed")
    rsi = f" RSI is {found['rsi']:.0f}, {found['rsi_zone'].split(':')[0]}." if found["rsi"] is not None else ""
    shorter = found.get("entry")
    timed = ""

    if shorter:
        if shorter["verdict"] in ("buy", "sell", "skip"):
            timed = f" On the {shorter['timeframe']} chart: {shorter['headline']}"
        else:
            timed = f" On the {shorter['timeframe']} chart, nothing to do yet either."

    return (f"{found['instrument']} is {price} on the {found['timeframe']} chart. {found['headline']}{rsi}{timed}"
            f" The plan is on the panel, sir.")


# ---- what is asked --------------------------------------------------------------------------------

# A question about trading a market, not any sentence with "buy" in it: "remind me to buy gold
# earrings" is not one.
_DECIDING = re.compile(
    r"\b(?:should|shall|do|would|could|can|must)\s+(?:i|we)\b.*\b(?:buy|sell|long|short|enter|trade)\b"
    r"|\bis\s+(?:it|now)\s+(?:a\s+good\s+time|time)\s+to\s+(?:buy|sell|go\s+long|go\s+short|trade)\b"
    r"|\bbuy\s+or\s+sell\b|\bsell\s+or\s+buy\b|\blong\s+or\s+short\b"
    r"|\b(?:trade\s+)?(?:plan|setup|set\s+up)\s+(?:for|on)\b")
_NOT_THIS = re.compile(r"\b(?:trader|trades|traded|record|journal|bot|alert|alerts)\b")


def wanted(command):
    """ "Shall I buy or sell gold", "silver plan", "should I go long on oil" -- not a trader or its record."""
    text = _said(command)
    chosen = settings()

    if not which(text, chosen) or _NOT_THIS.search(text) or dismissed(text):
        return False

    named_plan = re.search(rf"\b{_names_pattern(chosen)}\s+(?:trade\s+)?(?:plan|setup|set\s+up)\b", text)
    return bool(_DECIDING.search(text) or named_plan)


def dismissed(command):
    text = _said(command)
    names = _names_pattern(settings())
    return bool(re.search(rf"\b(?:close|hide|dismiss|shut|put away)\b.*\b(?:{names}|trade)\s+"
                          rf"(?:plan|chart|panel|setup)\b", text))
