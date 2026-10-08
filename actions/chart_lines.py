"""Support and resistance read from a chart: where price has turned, and how much room a trade has before one.

Shared by the trade plan (actions/trade_plan.py), the gold backtest and the gold trader
(actions/gold_strategy.py, actions/gold_trader.py), so a line means the same thing everywhere:

    a peak      a candle whose high is the highest of the [strength] candles either side of it, and
                that stands out: price rose at least [prominence] to reach it within [window] candles
                before, and fell at least that far from it within [window] after (a trough: the lowest,
                fallen to and risen from). What a trader calls "a high" -- not every wobble.
    a line      two highs within [merge] of each other, with no close above them in between -- price
                came up, was turned back, and was turned back again at the same price: resistance.
                Two lows the same way: support. The most recent such pair is the line in use, as a
                trader takes the two latest highs that line up.
                Your own lines are lines whatever the chart says, and keep their own price.
    a zone      for the gold trader's room rule: every level price turned at twice or more, old or new,
                since any of them can stop a trade short (zones()).
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


def levels(candles, merge, strength, touches=2, yours=(), prominence=0.0, window=None, lookback=None):
    """The lines, lowest first: [{"price", "kind", "touches", "yours", "last", "swings"}].

    As a trader draws them. Going back from now, each high (of the last [lookback] candles, all if None)
    is paired with the next earlier high within [merge] of it that price has respected -- no candle closed
    above the higher of the two between them. The pair is a resistance line, at their average; a pair of
    lows the same way, with no close below between them, is a support line. Each high or low makes one
    line at most, so the most recent pair comes first. "kind" is "high" (resistance) or "low" (support),
    None for your own lines; "last" the index of the later peak; "swings" [(index, price)] of both, so a
    chart can ring them. [touches] is kept for callers: a line is always at least two peaks.
    """
    found = [{"price": float(level), "kind": None, "touches": 0, "yours": True, "last": -1, "swings": []}
             for level in yours]
    first = max(0, len(candles) - lookback) if lookback else 0

    for kind in ("high", "low"):
        recent = sorted(((index, price) for index, price, which in peaks(candles, strength, prominence, window)
                         if which == kind and index >= first), reverse=True)
        used = set()

        for later, (index, price) in enumerate(recent):
            if later in used:
                continue

            for earlier in range(later + 1, len(recent)):
                then, before = recent[earlier]

                if earlier in used or abs(price - before) > merge:
                    continue

                between = candles[then + 1:index]
                held = (all(candle[4] < max(price, before) for candle in between) if kind == "high"
                        else all(candle[4] > min(price, before) for candle in between))

                if held:
                    used.update((later, earlier))
                    pair = [(then, before), (index, price)]
                    same = next((line for line in found if line["kind"] == kind
                                 and all(abs(other - value) <= merge for _i, other in line["swings"]
                                         for _j, value in pair)), None)

                    # The same level met again: one line with more touches, not two lines on top of each other.
                    if same:
                        same["swings"] = sorted(same["swings"] + pair)
                        same["touches"] = len(same["swings"])
                        same["last"] = max(same["last"], index)
                        same["price"] = round(sum(value for _i, value in same["swings"]) / same["touches"], 6)
                    else:
                        found.append({"price": round((price + before) / 2, 6), "kind": kind, "touches": 2,
                                      "yours": False, "last": index, "swings": pair})
                    break

    return sorted(found, key=lambda line: line["price"])


def zones(candles, merge, strength, touches, yours=(), prominence=0.0, window=None):
    """Every level price has turned at [touches] times or more, lowest first, in the same form as levels().

    For the gold trader's room rule, which asks whether ANY level stands in a trade's way -- not only the
    latest pair a trader would draw. Peaks of one kind within [merge] of each other are one level, at
    their average; every peak in a level lies within [merge] of every other, so a level cannot creep.
    Tested over three years of OANDA gold with the room rule: keep it as it is unless a backtest says so.
    """
    groups = [{"prices": [], "swings": [], "yours": True, "kind": None, "price": float(level)} for level in yours]

    def fits(group, price):
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


def latest(lines, kind):
    """The resistance ("high") or support ("low") as a trader draws it: the line through the most recent
    pair of that kind, wherever the price is now -- a support price has since broken through is still the
    support it was, and is drawn. None if there is none."""
    return max((line for line in lines if line.get("kind") == kind), key=lambda line: line["last"], default=None)


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
