"""Write one JARVIS Signal Desk briefing across every market we can read.

    python tools/signal_desk.py

Nothing is traded. Nothing is sent anywhere. It writes a plain-text
briefing and, for each market, a 4-hour chart and a 15-minute chart into
~/JARVIS/signal-desk/, and prints the briefing so you can read it here.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# The OANDA token lives in .env, as it does for JARVIS itself.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


from actions import files, trade_plan  # noqa: E402


# Which markets go into the briefing. Any name from trade-plan.json's
# "markets" section will do -- add or remove as you like.
MARKETS = (
    "gold",
    "silver",
    "platinum",
    "copper",
    "crude oil",
    "brent crude",
    "natural gas",
)


HOW_TO_READ = """\
How to read this
----------------
Every signal follows one rule: a line breaks, price comes back to test
it, and a candle closes back through it. The stop sits beyond the
retest's wick; the target sits just short of the next line (three times
the risk when no line is in the way). Under 2 : 1 reward to risk, the
plan says skip. The edge is in the ratio and the discipline, not in any
single call.

A setup is live from the close that confirms it until the next close on
that timeframe -- up to 4 hours on the 4-hour chart, 15 minutes on the
15-minute one. After that it is shown as passed, with the entry it had,
and is not chased.

Nothing on earth can promise a market will move a certain way. These
are signals, read from OANDA's own candles, and any trade taken on
them can lose money. Size small. Expect losses. That is what trading
is.

Two charts per market:
  4-hour     the map. Support and resistance, drawn from the last two
             highs or lows that held on this timeframe. The plan and
             the verdict live here.
  15-minute  the trigger. The SAME lines, on finer candles, so the
             break, retest and confirmation show as they happen --
             often hours before the 4-hour chart registers them. The
             entry, stop and target come from this page.

The 15-minute chart can never disagree with the 4-hour about a level,
because it draws the same lines. It can only be ahead on timing.

Rules, not a forecast.
"""


def _age(when):
    """How long ago a confirmation closed, as '3h 53m ago'. Empty when unknown.

    Negative gaps (clock skew, a candle from the future) are clamped to
    zero rather than shown as '-4m ago'.
    """
    if when is None:
        return ""

    minutes = max(0, int(
        (datetime.now(timezone.utc) - when).total_seconds() // 60))

    if minutes < 60:
        return f"{minutes}m ago"

    return f"{minutes // 60}h {minutes % 60}m ago"


def glance(results):
    """One line for the top of the file: what is live, and on which timeframe."""
    live = []

    for name, found in results:
        if found is None or found["verdict"] not in ("buy", "sell"):
            continue

        side = found["verdict"]
        plan = found.get(side)

        if plan is None:
            continue

        # Is the trigger timeframe also confirmed, or only the map?
        entry = found.get("entry")
        entry_plan = entry.get(side) if entry else None

        triggered = bool(
            entry_plan
            and entry_plan["stage"] == "ready"
        )

        live.append((name, found, side, plan, triggered))

    if not live:
        return "Nothing live: no setup has confirmed on its last 4-hour close."

    parts = []
    any_triggered = False

    for _name, found, side, plan, triggered in live:
        d = found["decimals"]
        tag = "triggered" if triggered else "4h only"

        # A triggered setup is live because the 15-minute candle
        # confirmed it, so that is the confirmation to age. A 4h-only
        # setup has no 15-minute confirmation yet, so age the 4-hour
        # one. Either way the timestamp is the candle's close, set in
        # trade_plan.side_plan.
        entry = found.get("entry")
        entry_plan = entry.get(side) if entry else None
        source = entry_plan if (triggered and entry_plan) else plan

        age = _age(source.get("confirmed_at"))
        freshness = f", confirmed {age}" if age else ""

        parts.append(
            f"{found['instrument']} {side.upper()} "
            f"({tag}: entry {plan['entry']:,.{d}f}, "
            f"stop {plan['stop']:,.{d}f}, "
            f"target {plan['target']:,.{d}f}{freshness})"
        )

        if triggered:
            any_triggered = True

    next_close = live[0][1].get("next_close") or "soon"

    if len(parts) == 1:
        opening = "One setup live" if any_triggered else "One setup confirmed on the 4-hour"
        return f"{opening}: {parts[0]}. Next 4-hour close {next_close} UK."

    opening = (
        f"{len(parts)} setups live"
        if any_triggered
        else f"{len(parts)} setups confirmed on the 4-hour"
    )
    return (
        f"{opening}: " + "; ".join(parts) +
        f". Next 4-hour close {next_close} UK."
    )


def block_for(found, chart_4h, chart_15m):
    """One market, one paragraph, with both chart names below."""
    places = found["decimals"]
    verdict = found["verdict"].upper()

    lines = [
        f"{found['instrument']}  ({found['symbol']})  --  {verdict}",
        f"  Price:       {found['price']:,.{places}f}",
    ]

    if found.get("support") is not None:
        lines.append(f"  Support:     {found['support']:,.{places}f}")

    if found.get("resistance") is not None:
        lines.append(f"  Resistance:  {found['resistance']:,.{places}f}")

    if found["rsi"] is not None:
        lines.append(
            f"  RSI:         {found['rsi']:.0f}  ({found['rsi_zone']})")

    lines.append(f"  4-hour:      {found['headline']}")

    # The 4-hour plan's own freshness, when it is the one live: the
    # close of the candle that confirmed it, to the minute. Without
    # this the block cannot tell a setup that confirmed on the last
    # close from one confirmed overnight -- the glance line at the
    # top of the file says, but a reader of the block alone does not.
    if found["verdict"] in ("buy", "sell"):
        live_plan = found.get(found["verdict"])

        if live_plan and live_plan["stage"] == "ready":
            when = live_plan.get("confirmed_at")
            age = _age(when)

            if age:
                stamp = trade_plan._uk(when, "%H:%M")
                lines[-1] += f"  confirmed {stamp} UK ({age})"

    entry = found.get("entry")

    if entry is not None:
        lines.append(f"  15-minute:   {entry['headline']}")

        for side in ("buy", "sell"):
            plan = entry.get(side)

            if not plan or plan["stage"] != "ready":
                continue

            when = plan.get("confirmed_at")
            age = _age(when)
            freshness = ""

            if age:
                stamp = trade_plan._uk(when, "%H:%M")
                freshness = f"  confirmed {stamp} UK ({age})"

            lines.append(
                f"  {side.upper()}:  entry {plan['entry']:,.{places}f},  "
                f"stop {plan['stop']:,.{places}f},  "
                f"target {plan['target']:,.{places}f},  "
                f"R:R {plan['ratio']:.2f}{freshness}"
            )

    elif found.get("entry_error"):
        lines.append(
            f"  15-minute:   could not be read ({found['entry_error']})")

    if chart_4h:
        lines.append(f"  Chart 4h:    {chart_4h}")

    if chart_15m:
        lines.append(f"  Chart 15m:   {chart_15m}")

    return "\n".join(lines)


def label_for(found, kind, value):
    """A line's label, saying whether the price has moved past it.

    A resistance line the price is now above has been broken; a support
    line the price is now below has been broken. Saying so is the difference
    between a chart that reads right and one that reads wrong, even when
    both are technically correct.
    """
    price = found["price"]
    text = f"{value:,.{found['decimals']}f}"

    if kind == "resistance" and price > value:
        return f"broken resistance {text}"

    if kind == "support" and price < value:
        return f"broken support {text}"

    return f"{kind} {text}"


def chart_png(found, path):
    """The candles, the two lines, and where the price is now -- as a PNG.

    The last finished candle is followed by the one still forming, as
    OANDA's own chart draws it, so the right edge reaches now. Labels
    are placed in priority order (entry, stop, target first; then now;
    then support and resistance) and pushed apart when they would
    collide, with a short connector drawn back to the true level. A
    level outside the visible range gets its label at the edge with an
    arrow, so a distant target does not squash the candles.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception as error:
        print(f"[signal desk] could not draw charts: {error}")
        return False

    candles = list(found["candles"])

    if found.get("forming"):
        candles.append(found["forming"])

    if not candles:
        return False

    d = found["decimals"]
    price_lows = [c[3] for c in candles]
    price_highs = [c[2] for c in candles]
    candle_range = max(price_highs) - min(price_lows)

    extras = [found.get("support"), found.get("resistance")]

    for side in ("buy", "sell"):
        plan = found.get(side)

        if not plan or plan["stage"] != "ready":
            continue

        extras.append(plan.get("entry"))
        extras.append(plan.get("stop"))

        if (plan.get("target") is not None
                and abs(plan["target"] - found["price"]) <= candle_range * 3):
            extras.append(plan["target"])

    for value in extras:
        if value is not None:
            price_lows.append(value)
            price_highs.append(value)

    low, high = min(price_lows), max(price_highs)
    pad = (high - low) * 0.06
    low, high = low - pad, high + pad

    fig, ax = plt.subplots(figsize=(11, 4.8), dpi=110)
    fig.patch.set_facecolor("#0a1017")
    ax.set_facecolor("#0d1620")

    for index, candle in enumerate(candles):
        _, open_, high_, low_, close = candle[:5]
        up = close >= open_
        colour = "#4fe39a" if up else "#ff6b6b"

        ax.plot([index, index], [low_, high_], color=colour,
                linewidth=0.9, solid_capstyle="butt", zorder=2)

        body_bottom = min(open_, close)
        body_height = max(abs(close - open_), (high - low) * 0.0006)
        ax.add_patch(Rectangle((index - 0.34, body_bottom), 0.68, body_height,
                               facecolor=colour, edgecolor=colour,
                               linewidth=0, zorder=3))

    items = []

    if found.get("resistance") is not None:
        items.append((2, label_for(found, "resistance", found["resistance"]),
                      found["resistance"], "#ff6b6b", "--"))

    if found.get("support") is not None:
        items.append((2, label_for(found, "support", found["support"]),
                      found["support"], "#4fe39a", "--"))

    items.append((1, f"now {found['price']:,.{d}f}",
                  found["price"], "#5fc8f5", ":"))

    for side in ("buy", "sell"):
        plan = found.get(side)

        if not plan or plan["stage"] != "ready":
            continue

        colour = "#4fe39a" if side == "buy" else "#ff6b6b"

        # How long since the confirmation candle closed. Shown on the
        # entry label only: the entry is the price the confirmation set,
        # so its age is the setup's age. Stop and target share the same
        # moment; saying it three times is noise on a chart.
        age = _age(plan.get("confirmed_at"))
        suffix = f" ({age})" if age else ""

        for name in ("entry", "stop", "target"):
            label = f"{side.upper()} {name} {plan[name]:,.{d}f}"

            if name == "entry":
                label += suffix

            items.append((0, label, plan[name], colour, "-."))

    min_gap = (high - low) * 0.06
    placed = []

    def fits(y):
        return all(abs(y - taken) >= min_gap for taken in placed)

    def place(anchor):
        y = anchor

        for _ in range(40):
            if fits(y) and low <= y <= high:
                placed.append(y)
                return y
            y += min_gap * 0.4

        y = anchor - min_gap * 0.4

        for _ in range(40):
            if fits(y) and low <= y <= high:
                placed.append(y)
                return y
            y -= min_gap * 0.4

        placed.append(anchor)
        return anchor

    label_x = len(candles) + 1.0

    for priority, text, value, colour, style in sorted(items, key=lambda t: t[0]):
        on_chart = low <= value <= high

        if on_chart:
            ax.axhline(value, color=colour, linewidth=1.3,
                       linestyle=style, alpha=0.85, zorder=4)

        if value > high:
            shown_y = place(high * 0.985)
            shown_text = f" {text} \u2191 "
        elif value < low:
            shown_y = place(low * 1.015)
            shown_text = f" {text} \u2193 "
        else:
            shown_y = place(value)
            shown_text = f" {text} "

        if on_chart and abs(shown_y - value) > min_gap * 0.4:
            ax.plot([label_x - 0.9, label_x], [value, shown_y],
                    color=colour, linewidth=0.8, alpha=0.55, zorder=5)

        ax.text(label_x, shown_y, shown_text,
                color=colour, fontsize=9, va="center", ha="left", zorder=6,
                bbox=dict(facecolor="#0a1017", edgecolor=colour,
                          linewidth=0.8, boxstyle="round,pad=0.3"))

    longest = max(len(t[1]) for t in items) + 4
    right = max(6, int(longest * 0.7))

    ax.set_xlim(-0.6, len(candles) + right)
    ax.set_ylim(low, high)

    ax.set_title(
        f"{found['instrument']}  ·  {found['symbol']}  ·  {found['timeframe']}"
        f"  ·  {found['verdict'].upper()}",
        color="#e4f0fa", fontsize=13, pad=12, loc="left",
    )

    ax.tick_params(colors="#7d93a8", labelsize=8)
    ax.set_xticks([])

    for spine in ax.spines.values():
        spine.set_color("#1d2c3a")

    ax.grid(True, color="#1d2c3a", linewidth=0.5, alpha=0.4)

    fig.savefig(path, facecolor="#0a1017", bbox_inches="tight")
    plt.close(fig)

    return True


def main():
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    base = now.strftime("%Y-%m-%d-%H%M")

    folder = Path(files.root()) / "signal-desk"
    folder.mkdir(exist_ok=True)

    results = []

    for name in MARKETS:
        try:
            found = trade_plan.read(f"the {name} plan")
        except trade_plan.PlanError as error:
            print(f"{name.upper()}:  could not be read ({error})")
            results.append((name, None))
            continue

        stem = name.replace(" ", "-")

        chart_4h = f"{base}-{stem}.png"
        if not chart_png(found, folder / chart_4h):
            chart_4h = None

        chart_15m = None
        entry = found.get("entry")

        if entry is not None:
            candidate = f"{base}-{stem}-15m.png"

            if chart_png(entry, folder / candidate):
                chart_15m = candidate

        found["_chart_4h"] = chart_4h
        found["_chart_15m"] = chart_15m
        results.append((name, found))

        marker = "BUY" if found["verdict"] == "buy" else (
            "SELL" if found["verdict"] == "sell" else found["verdict"].upper())
        print(f"{name}:  {marker}")

    pieces = [
        f"JARVIS Signal Desk  --  {stamp}",
        "=" * 64,
        "",
        HOW_TO_READ,
        "=" * 64,
        "",
        glance(results),
        "",
        "=" * 64,
        "",
    ]

    for _name, found in results:
        if found is None:
            continue

        pieces.append(block_for(found, found.get("_chart_4h"),
                                found.get("_chart_15m")))
        pieces.append("")

    text = "\n".join(pieces)
    path = folder / f"{base}.txt"
    path.write_text(text, encoding="utf-8")

    print()
    print(text)
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
