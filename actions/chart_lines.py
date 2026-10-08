"""Support and resistance read from a chart: where price has turned, and how much room a trade has before one.

Shared by the trade plan (actions/trade_plan.py), the gold backtest and the gold trader
(actions/gold_strategy.py, actions/gold_trader.py), so a line means the same thing everywhere:

    a peak      a candle whose high is the highest of the [strength] candles either side of it, and
                that stands out: price rose at least [prominence] to reach it within [window] candles
                before, and fell at least that far from it within [window] after (a trough: the lowest,
                fallen to and risen from). What a trader calls "a high" -- not every wobble.
    a line      peaks of one kind (highs, or lows) all within [merge] of each other, at their average;
                it needs [touches] of them: two highs close together make a resistance line, two lows
                a support line. Each line keeps its kind and the time of its latest peak, so the chart
                can take, as traders do, the most recent pair of highs above the price as resistance
                and the most recent pair of lows below it as support.
                Your own lines are lines whatever the chart says, and keep their own price.
    room        how far a trade can go before the first line in its way, less a [buffer] -- price
                often turns just short of a line, so a target is not asked to touch it

Arithmetic only: candles in, prices out.
"""


def peaks(candles, strength, prominence=0.0, window=None):
    """[(index, price, kind)] of every high ("high") and low ("low") in [candles] ((start, open, high, low,
    close)) that stands out by [prominence] on both sides within [window] candles (2 x [strength] unless
    given)."""
    window = window or 2 * strength
    found = []

    for index in range(strength, len(candles) - strength):
        around = candles[index - strength:index + strength + 1]
        before = candles[max(0, index - window):index]
        after = candles[index + 1:index + 1 + window]
        high, low = candles[index][2], candles[index][3]

        if high >= max(candle[2] for candle in around):
            rise = high - min(candle[3] for candle in before)
            fall = high - min(candle[3] for candle in after)

            if min(rise, fall) >= prominence:
                found.append((index, high, "high"))

        if low <= min(candle[3] for candle in around):
            fall = max(candle[2] for candle in before) - low
            rise = max(candle[2] for candle in after) - low

            if min(rise, fall) >= prominence:
                found.append((index, low, "low"))

    return found


def swings(candles, strength, prominence=0.0, window=None):
    """[(index, price)] of every peak, high or low."""
    return [(index, price) for index, price, _kind in peaks(candles, strength, prominence, window)]


def levels(candles, merge, strength, touches, yours=(), prominence=0.0, window=None):
    """The lines, lowest first: [{"price", "kind", "touches", "yours", "last", "swings"}] -- "kind" "high"
    (resistance's) or "low" (support's), None for your own; "last" the index of the latest peak; "swings"
    [(index, price)] of every one, so a chart can mark where price turned."""
    groups = [{"prices": [], "swings": [], "yours": True, "kind": None, "price": float(level)} for level in yours]

    def fits(group, price):
        # Every swing in a line lies within [merge] of every other: a line cannot creep, swing by swing,
        # further than that from where it started.
        spread = group["prices"] + [price] + ([group["price"]] if group["yours"] else [])
        return max(spread) - min(spread) <= merge

    for index, price, kind in sorted(peaks(candles, strength, prominence, window), key=lambda peak: peak[1]):
        near = min((group for group in groups if group["kind"] in (None, kind) and fits(group, price)),
                   key=lambda group: abs(group["price"] - price), default=None)

        if near is not None:
            near["prices"].append(price)
            near["swings"].append((index, price))

            if not near["yours"]:
                near["price"] = sum(near["prices"]) / len(near["prices"])
        else:
            groups.append({"prices": [price], "swings": [(index, price)], "yours": False, "kind": kind,
                           "price": price})

    return sorted(({"price": round(group["price"], 6), "kind": group["kind"], "touches": len(group["swings"]),
                    "yours": group["yours"],
                    "last": max((index for index, _price in group["swings"]), default=-1),
                    "swings": sorted(group["swings"])} for group in groups
                   if group["yours"] or len(group["swings"]) >= touches), key=lambda line: line["price"])


def latest(lines, price, side):
    """The resistance ("above") or support ("below") as traders draw it: of the lines that side of [price]
    made of highs (resistance) or lows (support), the one whose latest peak is the most recent -- the last
    two highs close together, not merely the nearest line. Your own lines count either way. None if none."""
    kind = "high" if side == "above" else "low"
    beyond = [line for line in lines if (line["price"] > price if side == "above" else line["price"] < price)
              and line.get("kind") in (kind, None)]
    return max(beyond, key=lambda line: (line["last"], -abs(line["price"] - price)), default=None)


def room(side, entry, lines, buffer):
    """(room, line): how far a [side] trade entered at [entry] can go before the first line in its way, less
    [buffer], and that line -- or (None, None) with no line that way. The room is negative when the trade
    starts within [buffer] of the line: selling right on top of support."""
    if side == "buy":
        ahead = [line for line in lines if line["price"] > entry]
        nearest = min(ahead, key=lambda line: line["price"], default=None)
        return (nearest["price"] - buffer - entry, nearest) if nearest else (None, None)

    ahead = [line for line in lines if line["price"] < entry]
    nearest = max(ahead, key=lambda line: line["price"], default=None)
    return (entry - (nearest["price"] + buffer), nearest) if nearest else (None, None)
