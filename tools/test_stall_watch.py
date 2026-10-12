"""stall_watch says when the HUD's thread froze, for how long, and where.

    python tools/test_stall_watch.py

Off-screen Qt; the "HUD" is a timer on this thread, frozen on purpose by a
sleep inside a function with a name to look for.
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import stall_watch  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


app = QApplication.instance() or QApplication([])
reports = []
stall_watch.start(report=lambda seconds, hud, others: reports.append((seconds, hud, others)))


def run_for(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def freeze_the_hud_on_purpose():
    time.sleep(1.2)


run_for(0.6)
check(not reports, "a HUD that keeps up is not reported")

freeze_the_hud_on_purpose()
run_for(0.6)
check(len(reports) == 1, f"one freeze, one report ({len(reports)})")

if reports:
    seconds, hud, _others = reports[0]
    check(1.0 <= seconds <= 1.6, f"for about how long it was frozen ({seconds:.2f}s)")
    check(any("freeze_the_hud_on_purpose" in place for place in hud), f"and in what: {hud[-2:]}")

stall_watch._print_report(1.23, ["main.py:731 run", "speech.py:1340 start_voice"], [("kokoro-voice", ["voices.py:500 _open"])])

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
