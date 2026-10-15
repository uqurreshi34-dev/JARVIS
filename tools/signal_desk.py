"""Write one JARVIS Signal Desk briefing across every market we can read.

    python tools/signal_desk.py

Nothing is traded. Nothing is sent anywhere. It writes a plain-text
briefing and one chart image per market into ~/JARVIS/signal-desk/, and
prints the briefing so you can read it right here.
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


def glance(results):
    """One line for the top of the file: what to look at, or that nothing is on."""
    live = [
        (name, found)
        for name, found in results
        if found is not None and found["verdict"] in ("buy", "sell")
    ]

    if not live:
        return "Nothing on today. Every market is between its lines."

    parts = [
        f"{found['instrument']} {found['verdict'].upper()} "
        f"(entry {found[found['verdict']]['entry']:,.{found['decimals']}f}, "
        f"stop {found[found['verdict']]['stop']:,.{found['decimals']}f}, "
        f"target {found[found['verdict']]['target']:,.{found['decimals']}f})"
        for _name, found in live
    ]

    next_close = live[0][1].get("next_close") or "soon"

    if len(parts) == 1:
        return f"One setup live: {parts[0]}. Next 4-hour close {next_close} UK."

    return (
        f"{len(parts)} setups live: " + "; ".join(parts) +
        f". Next 4-hour close {next_close} UK."
    )


def block_for(found, chart_name):
    """One market, one paragraph, with its chart named below."""
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

    lines.append(f"  Verdict:     {found['headline']}")

    for side in ("buy", "sell"):
        plan = found.get(side)

        if not plan or plan["stage"] != "ready":
            continue

        lines.append(
            f"  {side.upper()}:  entry {plan['entry']:,.{places}f},  "
            f"stop {plan['stop']:,.{places}f},  "
            f"target {plan['target']:,.{places}f},  "
            f"R:R {plan['ratio']:.2f}"
        )

    if chart_name:
        lines.append(f"  Chart:       {chart_name}")

    return "\n".join(lines)


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

    # What the y-range must contain. Support and resistance always;
    # entry and stop always when a plan is ready; target only when it
    # is near enough that including it will not squash the candles
    # (a target 15% away would otherwise flatten everything into a
    # strip at the bottom of the chart).
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

    # Priority: 0 = the plan's actionable levels, 1 = where the price is
    # now, 2 = the support and resistance context. Lower number wins a
    # clash; the loser gets pushed aside and a connector drawn.
    items = []

    if found.get("resistance") is not None:
        items.append((2, f"resistance {found['resistance']:,.{d}f}",
                      found["resistance"], "#ff6b6b", "--"))

    if found.get("support") is not None:
        items.append((2, f"support {found['support']:,.{d}f}",
                      found["support"], "#4fe39a", "--"))

    items.append((1, f"now {found['price']:,.{d}f}",
                  found["price"], "#5fc8f5", ":"))

    for side in ("buy", "sell"):
        plan = found.get(side)

        if not plan or plan["stage"] != "ready":
            continue

        colour = "#4fe39a" if side == "buy" else "#ff6b6b"

        for name in ("entry", "stop", "target"):
            items.append((0, f"{side.upper()} {name} {plan[name]:,.{d}f}",
                          plan[name], colour, "-."))

    # Vertical layout. min_gap is roughly one label's height plus a
    # touch of breathing room, as a share of the visible price range.
    min_gap = (high - low) * 0.06
    placed = []

    def fits(y):
        return all(abs(y - taken) >= min_gap for taken in placed)

    def place(anchor):
        """A y near anchor that clears the labels already placed.

        Tries upward first -- a label usually belongs just above its
        line -- then downward if the top of the chart is reached. If
        neither fits, uses the anchor and takes the collision: better
        than a label missing entirely.
        """
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

    # Actionable levels first (priority 0), then "now", then the rest.
    # The line is always drawn at its true price; only the text moves.
    for priority, text, value, colour, style in sorted(items, key=lambda t: t[0]):
        on_chart = low <= value <= high

        if on_chart:
            ax.axhline(value, color=colour, linewidth=1.3,
                       linestyle=style, alpha=0.85, zorder=4)

        # An off-chart level gets its label at the edge with an arrow,
        # so the reader knows the price is real but not yet reached.
        if value > high:
            shown_y = place(high * 0.985)
            shown_text = f" {text} \u2191 "
        elif value < low:
            shown_y = place(low * 1.015)
            shown_text = f" {text} \u2193 "
        else:
            shown_y = place(value)
            shown_text = f" {text} "

        # A short diagonal connector, only when the label had to move.
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

        chart_name = f"{base}-{name.replace(' ', '-')}.png"
        drew = chart_png(found, folder / chart_name)
        results.append((name, found))

        print(f"{name}:  {found['verdict'].upper()}")

        if not drew:
            chart_name = None

        # Store the chart's name alongside the plan so the text can point at it.
        found["_chart"] = chart_name

    pieces = [
        f"JARVIS Signal Desk  --  {stamp}",
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

        pieces.append(block_for(found, found.get("_chart")))
        pieces.append("")

    pieces.append(
        "Rules, not a forecast. Figures come from OANDA's own candles. "
        "Nothing here is advice."
    )

    text = "\n".join(pieces)
    path = folder / f"{base}.txt"
    path.write_text(text, encoding="utf-8")

    print()
    print(text)
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
