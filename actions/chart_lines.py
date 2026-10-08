"""Support and resistance read from a chart: where price has turned, and how much room a trade has before one.

Shared by the trade plan (actions/trade_plan.py), the gold backtest and the gold trader
(actions/gold_strategy.py, actions/gold_trader.py), so a line means the same thing everywhere:

    a swing     a candle whose high is the highest of the [strength] candles either side of it
                (a swing low: whose low is the lowest)
    a line      swings within [merge] of each other, at their average; it needs [touches] of them,
                as price turning at a price once is a swing, and turning there again makes it a line.
                Your own lines are lines whatever the chart says, and keep their own price.
    room        how far a trade can go before the first line in its way, less a [buffer] -- price
                often turns just short of a line, so a target is not asked to touch it

Arithmetic only: candles in, prices out.
"""


def swings(candles, strength):
    """[(index, price)] of every swing high and swing low in [candles] ((start, open, high, low, close))."""
    found = []

    for index in range(strength, len(candles) - strength):
        around = candles[index - strength:index + strength + 1]
        high, low = candles[index][2], candles[index][3]

        if high >= max(candle[2] for candle in around):
            found.append((index, high))

        if low <= min(candle[3] for candle in around):
            found.append((index, low))

    return found


def levels(candles, merge, strength, touches, yours=()):
    """The lines, lowest first: [{"price", "touches", "yours", "last"}], "last" the index of the latest swing."""
    groups = [{"prices": [], "touches": 0, "yours": True, "last": -1, "price": float(level)} for level in yours]

    for index, price in sorted(swings(candles, strength), key=lambda swing: swing[1]):
        near = min(groups, key=lambda group: abs(group["price"] - price), default=None)

        if near is not None and abs(near["price"] - price) <= merge:
            near["prices"].append(price)
            near["touches"] += 1
            near["last"] = max(near["last"], index)

            if not near["yours"]:
                near["price"] = sum(near["prices"]) / len(near["prices"])
        else:
            groups.append({"prices": [price], "touches": 1, "yours": False, "last": index, "price": price})

    return sorted(({"price": round(group["price"], 6), "touches": group["touches"], "yours": group["yours"],
                    "last": group["last"]} for group in groups
                   if group["yours"] or group["touches"] >= touches), key=lambda line: line["price"])


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
