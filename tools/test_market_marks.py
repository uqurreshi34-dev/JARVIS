"""Price marks survive a restart, so a move made while JARVIS was closed is seen.

Every "run" below is a fresh Watcher, which is what a restart is: the old
one's memory is gone and only the file remains. Prices, thresholds and the
clock are fixtures, and the JARVIS folder is a sandbox.
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import files, markets, watch  # noqa: E402


def _run(price, heard, quiet=False):
    """One JARVIS run: a fresh watcher checks the market once."""
    watcher = watch.Watcher()
    watcher.set_listener(heard.append)

    with patch.object(
        markets, "crypto", return_value={"bitcoin": (price, 0.0)},
    ), patch.object(
        markets, "threshold_for", return_value=0.5,
    ), patch.object(
        markets, "spoken_price", side_effect=lambda value: f"{value:g} pounds",
    ), patch.object(
        watch, "_quiet_hours", return_value=quiet,
    ):
        watcher._check_markets()

    return watcher


def _age_marks(hours):
    """Pretend the saved marks were taken this many hours ago."""
    marks = watch.load_marks()
    earlier = time.time() - hours * 3600
    watch.save_marks({name: (price, earlier) for name, (price, _) in marks.items()})


def _reset():
    for name in (watch.MARKS_FILE, watch.PENDING_FILE):
        path = os.path.join(files.root(), name)

        if os.path.exists(path):
            os.remove(path)


def main():
    failures = []

    # A move while closed is reported at the next start, with since when.
    _reset()
    heard = []
    _run(100.0, heard)

    if heard:
        failures.append(f"the first sighting was reported: {heard}")

    if not watch.load_marks().get("bitcoin"):
        failures.append("the first sighting was not saved")

    _age_marks(2)
    _run(101.0, heard)

    if len(heard) != 1 or "up 1.0 percent since" not in heard[0]:
        failures.append(f"a move made while closed was not reported: {heard}")

    # The alert moved the mark, so the same move is not reported again.
    heard.clear()
    _run(101.2, heard)

    if heard:
        failures.append(f"a move below the threshold was reported: {heard}")

    if abs(watch.load_marks()["bitcoin"][0] - 101.0) > 1e-9:
        failures.append("the mark was not moved to the alerted price")

    # A move within one run carries no "since": nothing was missed.
    _reset()
    heard = []
    watcher = _run(100.0, heard)

    with patch.object(
        markets, "crypto", return_value={"bitcoin": (99.0, 0.0)},
    ), patch.object(
        markets, "threshold_for", return_value=0.5,
    ), patch.object(
        markets, "spoken_price", side_effect=lambda value: f"{value:g} pounds",
    ), patch.object(watch, "_quiet_hours", return_value=False):
        watcher._check_markets()

    if len(heard) != 1 or "since" in heard[0] or "down 1.0" not in heard[0]:
        failures.append(f"a move within one run was mis-reported: {heard}")

    # A mark older than MARK_HOURS is a fresh start, not a move.
    _reset()
    heard = []
    _run(100.0, heard)
    _age_marks(watch.MARK_HOURS + 1)
    _run(150.0, heard)

    if heard:
        failures.append(f"a stale mark produced an alert: {heard}")

    # During quiet hours the move is held for later, not lost.
    _reset()
    heard = []
    _run(100.0, heard)
    _age_marks(1)
    _run(102.0, heard, quiet=True)

    if heard:
        failures.append(f"a quiet-hours move was spoken: {heard}")

    held = [text for _when, text in watch.held()]

    if len(held) != 1 or "up 2.0 percent since" not in held[0]:
        failures.append(f"a quiet-hours move was not held: {held}")

    # A damaged file is ignored rather than crashing the watcher.
    _reset()
    with open(os.path.join(files.root(), watch.MARKS_FILE), "w") as handle:
        handle.write("bitcoin: not-a-price @ yesterday\n: 5 @\nrubbish\n")

    heard = []

    try:
        _run(100.0, heard)
    except Exception as error:
        failures.append(f"a damaged marks file crashed the watcher: {error}")

    if heard or not watch.load_marks().get("bitcoin"):
        failures.append("a damaged marks file was not replaced cleanly")

    # The organisers must never move the file away.
    from actions import folder_guard, folder_organizer

    organiser_names = {
        value
        for value in vars(folder_organizer).values()
        if isinstance(value, frozenset)
        for value in value
        if isinstance(value, str)
    }

    if watch.MARKS_FILE not in organiser_names:
        failures.append("the folder organiser does not protect the marks file")

    if watch.MARKS_FILE not in folder_guard._PROTECTED_AUTO_NAMES:
        failures.append("the folder guard does not protect the marks file")

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: price marks survive a restart, moves made while closed are "
        "reported with since when, and stale or damaged marks start afresh."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
