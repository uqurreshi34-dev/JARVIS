"""A written report on a coin, with tables and graphs.

Built entirely from data JARVIS already fetches free of charge, drawn with
matplotlib and written into a Word document that lands in the JARVIS
folder. Nothing here advises: it reports what happened and stops.
"""

import io
import os
from datetime import datetime

import matplotlib

# A non-interactive backend, since the charts go into a document rather
# than a window.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow the backend call)

from actions import files, markets  # noqa: E402


# The periods a report covers, in order.
PERIODS = ("day", "week", "month")

# Styling, matched to the HUD rather than matplotlib's defaults.
_BACKDROP = "#0a1017"
_PANEL = "#0d1620"
_INK = "#e4f0fa"
_LINE = "#5fc8f5"
_RISE = "#5feba0"
_FALL = "#ff6e6e"
_GRID = "#1d2c3a"


def _chart(name, period, readings):
    """A price chart for one period, as PNG bytes, or None."""
    if len(readings) < 2:
        return None

    when = [point[0] for point in readings]
    price = [point[1] for point in readings]

    rising = price[-1] >= price[0]

    figure, axes = plt.subplots(figsize=(7.0, 3.1), dpi=110)

    figure.patch.set_facecolor(_BACKDROP)
    axes.set_facecolor(_PANEL)

    colour = _RISE if rising else _FALL

    axes.plot(when, price, color=colour, linewidth=1.8)
    axes.fill_between(when, price, min(price), color=colour, alpha=0.12)

    spoken = markets.PERIODS.get(period, {}).get("spoken", period)
    label = markets.COINS.get(name, {}).get("spoken", name)

    axes.set_title(
        f"{label} over {spoken}", color=_INK, fontsize=11, pad=12
    )
    axes.set_ylabel("GBP", color=_INK, fontsize=9)

    axes.tick_params(colors=_INK, labelsize=8)
    axes.grid(True, color=_GRID, linewidth=0.7, axis="y")
    axes.set_axisbelow(True)

    for spine in axes.spines.values():
        spine.set_color(_GRID)

    figure.autofmt_xdate(rotation=30)
    figure.tight_layout()

    buffer = io.BytesIO()

    try:
        figure.savefig(buffer, format="png", facecolor=_BACKDROP)
    except Exception as error:
        print(f"[JARVIS] could not draw the chart: {error}")
        return None
    finally:
        plt.close(figure)

    return buffer.getvalue()


def _money(value):
    """A price written the way it would be in a document."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"

    if number >= 100:
        return f"£{number:,.0f}"

    if number >= 1:
        return f"£{number:,.2f}"

    return f"£{number:,.4f}"


def _percent(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"

    return f"{number:+.2f}%"


def gather(name):
    """Figures and charts for every period. Returns a list of dictionaries."""
    collected = []

    for period in PERIODS:
        readings = markets.history(name, period)
        figures = markets.summarise(readings)

        if not figures:
            continue

        collected.append({
            "period": period,
            "spoken": markets.PERIODS[period]["spoken"],
            "figures": figures,
            "chart": _chart(name, period, readings),
        })

    return collected


def write(name):
    """Write the report. Returns (path, sections) or (None, [])."""
    try:
        from docx import Document
        from docx.shared import Inches, Pt, RGBColor

    except ImportError:
        print("[JARVIS] python-docx is not installed")
        return None, []

    sections = gather(name)

    if not sections:
        return None, []

    label = markets.COINS.get(name, {}).get("spoken", name)

    document = Document()

    heading = document.add_heading(f"{label} market report", level=1)

    for run in heading.runs:
        run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

    written = datetime.now().strftime("%d %B %Y at %H:%M")

    note = document.add_paragraph()
    run = note.add_run(f"Prepared by JARVIS on {written}. ")
    run.italic = True

    run = note.add_run(
        "Figures are from CoinGecko in pounds sterling. This is a record "
        "of what happened, not advice."
    )
    run.italic = True
    run.font.size = Pt(9)

    # One summary table covering every period, so the whole picture is
    # visible before the detail.
    document.add_heading("At a glance", level=2)

    table = document.add_table(rows=1, cols=6)
    table.style = "Light Grid Accent 1"

    for index, title in enumerate(
        ("Period", "Open", "Close", "High", "Low", "Change")
    ):
        cell = table.rows[0].cells[index]
        cell.text = title

        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True

    for section in sections:
        figures = section["figures"]
        row = table.add_row().cells

        row[0].text = section["spoken"].replace("the ", "").capitalize()
        row[1].text = _money(figures["open"])
        row[2].text = _money(figures["close"])
        row[3].text = _money(figures["high"])
        row[4].text = _money(figures["low"])
        row[5].text = _percent(figures["change"])

    # Then a section per period, each with its own chart.
    for section in sections:
        figures = section["figures"]

        document.add_heading(
            section["spoken"].replace("the ", "").capitalize(), level=2
        )

        direction = "rose" if figures["change"] >= 0 else "fell"

        document.add_paragraph(
            f"{label} {direction} {abs(figures['change']):.2f}% over "
            f"{section['spoken']}, from {_money(figures['open'])} to "
            f"{_money(figures['close'])}. It reached a high of "
            f"{_money(figures['high'])} and a low of "
            f"{_money(figures['low'])}, a spread of "
            f"{figures['spread']:.2f}% of the average price."
        )

        if section["chart"]:
            document.add_picture(
                io.BytesIO(section["chart"]), width=Inches(6.2)
            )

        detail = document.add_table(rows=1, cols=2)
        detail.style = "Light List Accent 1"

        detail.rows[0].cells[0].text = "Readings"
        detail.rows[0].cells[1].text = f"{figures['readings']:,}"

        row = detail.add_row().cells
        row[0].text = "From"
        row[1].text = figures["from"].strftime("%d %b %Y %H:%M")

        row = detail.add_row().cells
        row[0].text = "To"
        row[1].text = figures["to"].strftime("%d %b %Y %H:%M")

        document.add_paragraph()

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = files.resolve(f"{label} report {stamp}.docx", ".docx")

    if not path:
        return None, sections

    try:
        document.save(path)
    except Exception as error:
        print(f"[JARVIS] could not save the report: {error}")
        return None, sections

    print(f"[JARVIS] report written to {path}")

    return path, sections


def describe(name, path, sections):
    """What to say once the report is written."""
    label = markets.COINS.get(name, {}).get("spoken", name)

    if not sections:
        return f"I couldn't get any figures for {label}, sir."

    if not path:
        return f"I gathered the figures for {label}, but couldn't save them, sir."

    day = next(
        (s for s in sections if s["period"] == "day"), sections[0]
    )

    figures = day["figures"]
    direction = "up" if figures["change"] >= 0 else "down"

    return (
        f"Written, sir. {label} is {direction} "
        f"{markets.spoken_number(abs(figures['change']), 1)} percent "
        f"over the last 24 hours, at "
        f"{markets.spoken_price(figures['close'])}. "
        f"The report is in your JARVIS folder."
    )
