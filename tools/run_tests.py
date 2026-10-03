"""Run every test suite in tools/ and say which passed: the same check on your PC as on GitHub.

Each tools/test_*.py is its own script that exits non-zero when anything in
it failed, so each runs on its own, in a fresh Python, with the Qt panels
drawn offscreen and a time limit, and a summary at the end. GitHub Actions
runs exactly this on every push to the working branch
(.github/workflows/tests.yml).

    python tools/run_tests.py                     every suite
    python tools/run_tests.py house sensor        only suites whose names contain these
    python tools/run_tests.py --skip stop_button  all but these
    python tools/run_tests.py --report out.txt    every suite's full output kept in a file

Exits 1 if any suite failed or ran out of time, 0 otherwise.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Long enough for the slowest suite on a cold machine (the semantic memory
# suites load a model); short enough that a hung one does not hold up the rest.
TIMEOUT_SECONDS = 600

# How many of a failed suite's last lines are shown in the summary.
TAIL_LINES = 15

# Named like suites but not suites, and never run here: test_console.py is
# the typed console for trying commands without a microphone, and waits for
# you at a prompt; test_type.py is a manual check of typing, and really
# types into whatever window has the focus.
NOT_SUITES = ("test_console", "test_type")


def suites(directory, wanted=(), skipped=()):
    """The test scripts in [directory], by name, filtered by [wanted] and [skipped] (parts of names)."""
    found = [path for path in sorted(Path(directory).resolve().glob("test_*.py")) if path.stem not in NOT_SUITES]

    if wanted:
        found = [path for path in found if any(part in path.stem for part in wanted)]

    return [path for path in found if not any(part and part in path.stem for part in skipped)]


def why(output):
    """What a failed suite's output says went wrong: every FAIL line and traceback, then its last lines.

    The last lines alone hid a suite's one FAIL among the dozens of PASS
    lines printed after it.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    tail = lines[-TAIL_LINES:]
    failed = [line for line in lines[:-TAIL_LINES]
              if line.lstrip().startswith(("FAIL", "- ", "Traceback", "AssertionError", "Error"))
              or "Error:" in line]

    return failed[:40] + (["..."] if failed else []) + tail


def run(path, timeout):
    """Run one suite. (passed, seconds, output)."""
    environment = dict(os.environ)
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.monotonic()

    try:
        # No keyboard: a suite that ever asked for input gets an empty line, not a wait.
        finished = subprocess.run([sys.executable, str(path)], cwd=str(ROOT), env=environment,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=timeout)
        output = finished.stdout.decode("utf-8", "replace")
        passed = finished.returncode == 0
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or b"").decode("utf-8", "replace") + f"\n[run_tests] timed out after {timeout}s\n"
        passed = False

    return passed, time.monotonic() - started, output


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Run JARVIS's test suites.")
    parser.add_argument("names", nargs="*", help="only suites whose names contain one of these")
    parser.add_argument("--skip", default=os.getenv("JARVIS_TEST_SKIP", ""),
                        help="comma-separated parts of names to leave out (or JARVIS_TEST_SKIP)")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS, help="seconds per suite")
    parser.add_argument("--report", help="write every suite's full output to this file")
    parser.add_argument("--tests-dir", default=str(ROOT / "tools"), help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)

    chosen = suites(options.tests_dir, options.names, [part.strip() for part in options.skip.split(",")])

    if not chosen:
        print("[run_tests] no suites matched")
        return 1

    failed, report = [], []

    for path in chosen:
        passed, seconds, output = run(path, options.timeout)
        print(f"{'PASS' if passed else 'FAIL'}  {path.stem}  ({seconds:.1f}s)", flush=True)
        report.append(f"===== {path.stem}: {'passed' if passed else 'FAILED'} in {seconds:.1f}s =====\n{output}\n")

        if not passed:
            failed.append(path.stem)
            print("\n".join("      " + line for line in why(output)), flush=True)

    if options.report:
        Path(options.report).write_text("\n".join(report), encoding="utf-8")

    print(f"\n{len(chosen) - len(failed)} of {len(chosen)} suites passed"
          + (f"; failed: {', '.join(failed)}" if failed else "."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
