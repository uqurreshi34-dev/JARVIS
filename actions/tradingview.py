"""TradingView's readout for a coin, said aloud and shown on the HUD.

The connected TradingView service (mcp.json) does the reading; this asks
it, takes only the numbers and the few words it expects, says them, and
draws a card for the chart panel: the price, the change, where RSI sits,
the Bollinger signal, and the support and resistance levels around the
price. Read-only throughout: mcp_services.read() refuses any tool that
could change something.

Said aloud, and never a model call:

    bitcoin on tradingview / tradingview ethereum
    show me xrp on the four hour chart / bitcoin daily chart
    what's the rsi on bitcoin / bitcoin technical analysis

The coins are the ones JARVIS watches (markets.COINS), against the dollar
stablecoin on Binance; the chart is one hour unless another is named.
"Close the chart" puts the card away.
"""

import io
import json
import re

from actions import markets, mcp_services


TOOL = "coin_analysis"
EXCHANGE = "BINANCE"
QUOTE = "USDT"
DEFAULT_TIMEFRAME = "1h"

# Spoken timeframes, longest first when matched, and how each is said back.
TIMEFRAMES = {
    "five minute": "5m", "5 minute": "5m", "five min": "5m", "5 min": "5m", "5m": "5m",
    "fifteen minute": "15m", "15 minute": "15m", "15 min": "15m", "quarter hour": "15m", "15m": "15m",
    "four hour": "4h", "4 hour": "4h", "four hours": "4h", "4 hours": "4h", "4h": "4h",
    "one hour": "1h", "1 hour": "1h", "hourly": "1h", "hour": "1h", "1h": "1h",
    "daily": "1D", "one day": "1D", "1 day": "1D", "day": "1D", "1d": "1D",
    "weekly": "1W", "one week": "1W", "week": "1W", "1w": "1W",
    "monthly": "1M", "one month": "1M", "month": "1M",
}
SPOKEN_TIMEFRAMES = {"5m": "five minute", "15m": "fifteen minute", "1h": "one hour", "4h": "four hour",
                     "1D": "daily", "1W": "weekly", "1M": "monthly"}

# What asks for the analysis rather than the price: TradingView by name, or
# a word only an analysis answers.
_NAMES = ("tradingview", "trading view", "trading views")
_CUES = ("analysis", "analyse", "analyze", "technicals", "technical", "rsi", "indicators", "indicator",
         "signal", "signals", "chart", "charts", "levels", "support", "resistance", "readout")
_NOT_ASKING = ("save", "close", "hide", "remove", "delete", "dismiss", "clear")

# The only words taken from TradingView's answer: anything else is not said.
_RSI_SIGNALS = ("overbought", "bullish", "neutral", "bearish", "oversold")
_SIGNALS = ("buy", "sell", "neutral")

_WORD = re.compile(r"[a-z0-9]+")


def _words(text):
    return _WORD.findall(str(text or "").casefold().replace("'", ""))


def _has(words, phrase):
    wanted = phrase.split()
    return any(words[i:i + len(wanted)] == wanted for i in range(len(words) - len(wanted) + 1))


def _coin(words):
    """The coin named among [words] (by markets.COIN_WORDS), longest name first; or None."""
    for size in (3, 2, 1):
        for index in range(len(words) - size + 1):
            coin = markets.coin_for(" ".join(words[index:index + size]))

            if coin:
                return coin

    return None


def timeframe(words):
    for phrase in sorted(TIMEFRAMES, key=lambda phrase: len(phrase.split()), reverse=True):
        if _has(words, phrase):
            return TIMEFRAMES[phrase]

    return DEFAULT_TIMEFRAME


def asked(text):
    """(coin, timeframe) when [text] asks for TradingView's readout of a coin JARVIS watches, else None."""
    words = _words(text)

    if not words or words[0] in _NOT_ASKING:
        return None

    named = any(_has(words, name) for name in _NAMES)

    if not (named or any(word in _CUES for word in words)):
        return None

    coin = _coin(words)
    return (coin, timeframe(words)) if coin else None


# ---- asking TradingView ---------------------------------------------------------------------

def _read(symbol, exchange, frame):
    """Ask the connected service that offers coin_analysis. (text, None) or (None, why not)."""
    servers = mcp_services.offering(TOOL)

    if not servers:
        return None, "not connected"

    return mcp_services.read(servers[0], TOOL, {"symbol": symbol, "exchange": exchange, "timeframe": frame})


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _word(value, allowed):
    word = str(value or "").strip().casefold()
    return word if word in allowed else None


def readout(text):
    """TradingView's answer as the few values used, checked: a dict, or None if it is not one."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None

    if not isinstance(data, dict) or "error" in data:
        return None

    prices = data.get("price_data") if isinstance(data.get("price_data"), dict) else {}
    rsi = data.get("rsi") if isinstance(data.get("rsi"), dict) else {}
    mood = data.get("market_sentiment") if isinstance(data.get("market_sentiment"), dict) else {}
    levels = data.get("support_resistance") if isinstance(data.get("support_resistance"), dict) else {}
    price = _number(prices.get("current_price"))

    if price is None:
        return None

    return {
        "price": price,
        "change": _number(prices.get("change_percent")),
        "open": _number(prices.get("open")), "high": _number(prices.get("high")), "low": _number(prices.get("low")),
        "rsi": _number(rsi.get("value")),
        "rsi_signal": _word(rsi.get("signal"), _RSI_SIGNALS),
        "signal": _word(mood.get("buy_sell_signal"), _SIGNALS),
        "levels": {key: _number(levels.get(key)) for key in
                   ("support_3", "support_2", "support_1", "pivot", "resistance_1", "resistance_2", "resistance_3")
                   if _number(levels.get(key)) is not None},
    }


def _decimals(value):
    return 2 if abs(value) >= 1 else 4


def spoken(coin, frame, values):
    """"Bitcoin on the one hour chart is at ... dollars, up ... percent. RSI ..., bullish. ...", as said."""
    name = markets.COINS[coin]["spoken"]
    price = values["price"]
    said = f"{name} on the {SPOKEN_TIMEFRAMES.get(frame, frame)} chart is at " \
           f"{markets.spoken_number(price, _decimals(price))} dollars"

    change = values["change"]

    if change is not None:
        if abs(change) < 0.005:
            said += ", flat"
        else:
            said += f", {'up' if change > 0 else 'down'} {markets.spoken_number(abs(change), 2)} percent"

    sentences = [said + "."]

    if values["rsi"] is not None:
        sentences.append(f"RSI {markets.spoken_number(values['rsi'], 1)}"
                         + (f", {values['rsi_signal']}." if values["rsi_signal"] else "."))

    if values["signal"]:
        sentences.append(f"The Bollinger signal is {values['signal']}.")

    sentences[-1] = sentences[-1][:-1] + ", sir."
    return " ".join(sentences)


def answer(text, reader=None):
    """(what to say, card png or None, card title) for [text]."""
    request = asked(text)

    if request is None:
        return "I couldn't tell which coin you meant, sir.", None, ""

    coin, frame = request
    label = markets.COINS[coin]["label"]
    name = markets.COINS[coin]["spoken"]
    reply, problem = (reader or _read)(f"{label}{QUOTE}", EXCHANGE, frame)

    if problem == "not connected":
        return "TradingView isn't connected, sir.", None, ""

    values = readout(reply) if problem is None else None

    if values is None:
        print(f"[JARVIS] TradingView could not read {label}{QUOTE}: {problem or (reply or '')[:200]}")
        return f"TradingView couldn't give me {name} just now, sir.", None, ""

    title = f"{label} {SPOKEN_TIMEFRAMES.get(frame, frame).upper()} TRADINGVIEW"
    return spoken(coin, frame, values), card(label, frame, values), title


# ---- the card -----------------------------------------------------------------------------------

_BACKDROP = "#0a1017"
_PANEL = "#0d1620"
_INK = "#e4f0fa"
_FAINT = "#7d93a8"
_GRID = "#1d2c3a"
_ACCENT = "#5fc8f5"
_UP, _DOWN, _LEVEL = "#4fe39a", "#ff6b6b", "#f5b85f"


def _level(value):
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.{_decimals(value)}f}"


def _money(value):
    return f"${value:,.{_decimals(value)}f}"


def card(label, frame, values):
    """A PNG card of [values], drawn to match the HUD; None if it cannot be drawn."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch, Rectangle
    except Exception as error:
        print(f"[JARVIS] could not draw the TradingView card: {error}")
        return None

    figure = plt.figure(figsize=(7.2, 4.4), dpi=110)
    figure.patch.set_facecolor(_BACKDROP)
    change = values["change"]
    trend = _UP if (change or 0) > 0 else _DOWN if (change or 0) < 0 else _FAINT

    figure.text(0.05, 0.9, f"{label} / {QUOTE}   \u00b7   {EXCHANGE}   \u00b7   {frame.upper()}",
                color=_FAINT, fontsize=9)
    figure.text(0.05, 0.74, _money(values["price"]), color=_INK, fontsize=26, weight="bold")

    if change is not None:
        figure.text(0.05, 0.665, f"{'+' if change > 0 else ''}{change:.2f}%", color=trend, fontsize=12)

    span = "   ".join(f"{name} {_money(values[key])}" for name, key in (("O", "open"), ("H", "high"), ("L", "low"))
                      if values[key] is not None)
    figure.text(0.3, 0.665, span, color=_FAINT, fontsize=8.5)

    # The Bollinger signal, as a chip on the right.
    signal = values["signal"] or "no signal"
    chip = {"buy": _UP, "sell": _DOWN}.get(signal, _FAINT)
    badge = figure.add_axes([0.72, 0.73, 0.23, 0.13])
    badge.axis("off")
    badge.add_patch(FancyBboxPatch((0.08, 0.14), 0.84, 0.72, boxstyle="round,pad=0.02,rounding_size=0.18",
                                   facecolor=_PANEL, edgecolor=chip, linewidth=1.6, transform=badge.transAxes))
    badge.text(0.5, 0.5, signal.upper(), color=chip, fontsize=13, weight="bold", ha="center", va="center")
    figure.text(0.835, 0.69, "BOLLINGER SIGNAL", color=_FAINT, fontsize=7, ha="center")

    # RSI, 0 to 100, with its zones.
    gauge = figure.add_axes([0.05, 0.43, 0.9, 0.09])
    gauge.set_xlim(0, 100)
    gauge.set_ylim(0, 1)
    gauge.axis("off")

    for start, width, colour in ((0, 30, "#6fb8ff"), (30, 40, _GRID), (70, 30, "#ff8a5b")):
        gauge.add_patch(Rectangle((start, 0.3), width, 0.4, facecolor=colour, alpha=0.55, edgecolor="none"))

    if values["rsi"] is not None:
        rsi = min(100.0, max(0.0, values["rsi"]))
        gauge.plot([rsi, rsi], [0.05, 0.95], color=_INK, linewidth=2.4)
        gauge.text(rsi, 1.05, f"RSI {values['rsi']:.1f}" + (f"  {values['rsi_signal']}" if values["rsi_signal"] else ""),
                   color=_INK, fontsize=9, ha="center", va="bottom")

    for mark in (30, 70):
        gauge.text(mark, -0.2, str(mark), color=_FAINT, fontsize=7, ha="center", va="top")

    # The levels around the price, on one line.
    levels = values["levels"]
    ladder = figure.add_axes([0.05, 0.06, 0.9, 0.2])
    ladder.axis("off")
    points = list(levels.values()) + [values["price"]]
    low, high = min(points), max(points)
    pad = (high - low) * 0.06 or abs(high) * 0.01 or 1
    ladder.set_xlim(low - pad, high + pad)
    ladder.set_ylim(0, 1)
    ladder.plot([low - pad, high + pad], [0.5, 0.5], color=_GRID, linewidth=1.4)

    for index, (key, level) in enumerate(sorted(levels.items(), key=lambda item: item[1])):
        short = "P" if key == "pivot" else ("S" if key.startswith("support") else "R") + key[-1]
        colour = _ACCENT if key == "pivot" else _UP if key.startswith("support") else _DOWN
        ladder.plot([level, level], [0.35, 0.65], color=colour, linewidth=2)
        ladder.text(level, 0.75 if index % 2 else 0.25,
                    f"{short}\n{_level(level)}" if index % 2 else f"{_level(level)}\n{short}",
                    color=colour, fontsize=7.5, ha="center", va="bottom" if index % 2 else "top", linespacing=1.3)

    ladder.scatter([values["price"]], [0.5], color=_LEVEL, s=70, zorder=5)
    ladder.text(values["price"], 0.95, "NOW", color=_LEVEL, fontsize=7.5, ha="center", va="bottom")
    figure.text(0.05, 0.3, "SUPPORT AND RESISTANCE", color=_FAINT, fontsize=7)

    buffer = io.BytesIO()

    try:
        figure.savefig(buffer, format="png", facecolor=_BACKDROP)
    except Exception as error:
        print(f"[JARVIS] could not draw the TradingView card: {error}")
        return None
    finally:
        plt.close(figure)

    return buffer.getvalue()
