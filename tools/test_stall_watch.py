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
pauses = []
time.sleep(0.8)   # before start(): not the HUD's time, and not reported
stall_watch.start(report=lambda seconds, hud, others: reports.append((seconds, hud, others)),
                  report_pause=lambda seconds, threads: pauses.append((seconds, threads)))


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

# A thread holding Python's lock stops the watcher too: its own sleep overruns, and that is reported.
real_sleep = stall_watch._sleep
overran = []


def overrunning_sleep(seconds):
    if not overran:
        overran.append(1)
        real_sleep(seconds + 0.8)
    else:
        real_sleep(seconds)


stall_watch._sleep = overrunning_sleep
run_for(1.5)
stall_watch._sleep = real_sleep
check(len(pauses) == 1 and pauses[0][0] >= 0.7, f"a pause of the whole program is reported ({pauses[:1]})")

stall_watch._print_pause(1.1, [("kokoro-voice", ["voices.py:500 _open"])])
stall_watch._print_report(1.23, ["main.py:731 run", "speech.py:1340 start_voice"], [("kokoro-voice", ["voices.py:500 _open"])])

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
