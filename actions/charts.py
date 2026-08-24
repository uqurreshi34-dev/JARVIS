"""Read a CSV in the JARVIS folder and plot two of its columns.

Everything happens locally: the file is read with Python's own csv module and
drawn with matplotlib, so no data leaves the machine and nothing is charged.
"""

import csv
import io
import os
import re
from datetime import datetime

import matplotlib

# A non-interactive backend, since the chart is rendered to bytes and shown
# in a Qt window rather than matplotlib's own viewer.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow the backend call)

from actions import files  # noqa: E402


# Reading a huge file to plot it would freeze the assistant.
MAX_ROWS = 5000

# Beyond this many categories a bar chart is unreadable, so it is trimmed.
MAX_CATEGORIES = 30

_NUMBER = re.compile(r"^-?[\d,]*\.?\d+$")

# The chart is drawn to match the HUD rather than matplotlib's defaults.
_BACKDROP = "#0a1017"
_PANEL = "#0d1620"
_INK = "#e4f0fa"
_ACCENT = "#5fc8f5"
_GRID = "#1d2c3a"


def _as_number(value):
    """A float from a spreadsheet cell, or None."""
    text = (value or "").strip().replace("£", "").replace("$", "")
    text = text.replace("%", "").strip()

    if not _NUMBER.match(text):
        return None

    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def read_columns(name):
    """Return (headers, rows) for a CSV in the folder, or (None, None)."""
    path = files.find_existing(name)

    if not path:
        return None, None

    if os.path.splitext(path)[1].casefold() not in (".csv", ".txt"):
        print(f"[JARVIS] {path} is not a spreadsheet")
        return None, None

    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            sample = handle.read(4096)
            handle.seek(0)

            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel

            reader = csv.reader(handle, dialect)
            headers = next(reader, None)

            if not headers:
                return None, None

            headers = [h.strip() for h in headers]
            rows = []

            for index, row in enumerate(reader):
                if index >= MAX_ROWS:
                    print(f"[JARVIS] only the first {MAX_ROWS} rows are used")
                    break

                if any(cell.strip() for cell in row):
                    rows.append(row)

    except OSError as error:
        print(f"[JARVIS] could not read {path}: {error}")
        return None, None

    return headers, rows


def match_column(headers, spoken):
    """Find the column a spoken word refers to, or None."""
    if not headers or not spoken:
        return None

    wanted = spoken.strip().casefold()

    for index, header in enumerate(headers):
        if header.casefold() == wanted:
            return index

    for index, header in enumerate(headers):
        if wanted in header.casefold() or header.casefold() in wanted:
            return index

    # Fall back to the closest name, which covers speech slips.
    from difflib import SequenceMatcher

    best_index = None
    best_score = 0.0

    for index, header in enumerate(headers):
        score = SequenceMatcher(None, wanted, header.casefold()).ratio()

        if score > best_score:
            best_score = score
            best_index = index

    return best_index if best_score >= 0.6 else None


def describe_columns(headers):
    """Spoken list of the columns available."""
    if not headers:
        return "that file has no columns"

    if len(headers) == 1:
        return f"one column, {headers[0]}"

    listed = ", ".join(headers[:-1]) + f", and {headers[-1]}"

    return f"{len(headers)} columns: {listed}"


def _series(rows, index):
    """Values from one column, as text."""
    return [row[index].strip() if index < len(row) else "" for row in rows]


# The chart most recently drawn, so it can still be saved after the offer
# to save it has lapsed.
_last_chart = {"data": None, "name": None}


def last_chart():
    """The most recent chart as (png bytes, suggested name)."""
    return _last_chart.get("data"), _last_chart.get("name")


def save_last():
    """Save the chart currently on screen. Returns the path, or None."""
    data, name = last_chart()

    if not data:
        return None

    return save(data, name or "chart")


def plot(name, x_index, y_index, title=None):
    """Draw a chart and return (png bytes, description), or (None, reason)."""
    headers, rows = read_columns(name)

    if not headers:
        return None, f"I couldn't read {name}, sir."

    if not rows:
        return None, f"{name} has no data, sir."

    if not (0 <= x_index < len(headers) and 0 <= y_index < len(headers)):
        return None, "Those columns aren't in that file, sir."

    x_label = headers[x_index]
    y_label = headers[y_index]

    x_raw = _series(rows, x_index)
    y_raw = _series(rows, y_index)

    pairs = []

    for x_value, y_value in zip(x_raw, y_raw):
        number = _as_number(y_value)

        if number is None or not x_value:
            continue

        pairs.append((x_value, number))

    if not pairs:
        return None, f"{y_label} doesn't hold numbers I can plot, sir."

    trimmed = False

    if len(pairs) > MAX_CATEGORIES:
        pairs = pairs[:MAX_CATEGORIES]
        trimmed = True

    labels = [pair[0] for pair in pairs]
    values = [pair[1] for pair in pairs]

    # If the x column is numeric too, a line chart reads better than bars.
    numeric_x = all(_as_number(label) is not None for label in labels)

    if numeric_x:
        # Points have to be in order along the axis, or the line zigzags
        # back on itself and tells you nothing.
        pairs = sorted(pairs, key=lambda pair: _as_number(pair[0]))
        labels = [pair[0] for pair in pairs]
        values = [pair[1] for pair in pairs]

    figure, axes = plt.subplots(figsize=(7.2, 4.4), dpi=110)

    figure.patch.set_facecolor(_BACKDROP)
    axes.set_facecolor(_PANEL)

    if numeric_x:
        axes.plot(
            [_as_number(label) for label in labels], values,
            color=_ACCENT, linewidth=2.0, marker="o", markersize=4,
        )
    else:
        axes.bar(labels, values, color=_ACCENT)

        if max(len(label) for label in labels) > 6 or len(labels) > 8:
            plt.setp(axes.get_xticklabels(), rotation=40, ha="right")

    axes.set_xlabel(x_label, color=_INK, fontsize=10)
    axes.set_ylabel(y_label, color=_INK, fontsize=10)
    axes.set_title(
        title or f"{y_label} by {x_label}",
        color=_INK, fontsize=12, pad=14,
    )

    axes.tick_params(colors=_INK, labelsize=8)
    axes.grid(True, color=_GRID, linewidth=0.7, axis="y")
    axes.set_axisbelow(True)

    for spine in axes.spines.values():
        spine.set_color(_GRID)

    figure.tight_layout()

    buffer = io.BytesIO()

    try:
        figure.savefig(buffer, format="png", facecolor=_BACKDROP)
    except Exception as error:
        plt.close(figure)
        return None, f"I couldn't draw that chart, sir. {error}"

    plt.close(figure)

    spoken = f"Here's {y_label} by {x_label}, sir."

    if trimmed:
        spoken += f" Showing the first {MAX_CATEGORIES} rows."

    data = buffer.getvalue()

    # Remembered so "save the chart" works later, even if the offer to save
    # it was interrupted by another command.
    _last_chart["data"] = data
    _last_chart["name"] = f"{name} {y_label} by {x_label}"

    return data, spoken


def save(data, name):
    """Write chart bytes into the JARVIS folder. Returns the path or None."""
    if not data:
        return None

    stem = re.sub(r"[^\w \-]", "", name or "chart").strip() or "chart"
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")

    path = files.resolve(f"{stem} {stamp}.png", ".png")

    if not path:
        return None

    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError as error:
        print(f"[JARVIS] could not save the chart: {error}")
        return None

    print(f"[JARVIS] chart saved to {path}")

    return path
