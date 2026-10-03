"""The test runner (tools/run_tests.py), on a folder of pretend suites.

Checked:

- a suite that passes passes, one that fails fails, and the run fails;
- one that runs out of time is stopped and counted as failed;
- one that asks for input gets none, and fails at once rather than waiting;
- names choose suites, --skip and JARVIS_TEST_SKIP leave them out;
- the console and the typing check, which are not suites, are never run;
- --report keeps every suite's output, in full, non-ASCII included;
- and the real tools/ folder: every suite but those two is chosen.

    python tools/test_run_tests.py
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import run_tests  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


folder = Path(tempfile.mkdtemp(prefix="jarvis-runner-"))
(folder / "test_good.py").write_text('print("all fine")\n', encoding="utf-8")
(folder / "test_bad.py").write_text('import sys\nprint("FAIL something")\nsys.exit(1)\n', encoding="utf-8")
(folder / "test_slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
(folder / "test_asks.py").write_text('input("You: ")\n', encoding="utf-8")
(folder / "test_words.py").write_text('print("26.5\\u00b0C, caf\\u00e9")\n', encoding="utf-8")
(folder / "test_console.py").write_text('raise SystemExit("the console was run")\n', encoding="utf-8")
(folder / "test_type.py").write_text('raise SystemExit("the typing check was run")\n', encoding="utf-8")


def runner(*arguments):
    said = io.StringIO()

    with redirect_stdout(said):
        code = run_tests.main(["--tests-dir", str(folder), "--timeout", "3", *arguments])

    return code, said.getvalue()


report = folder / "report.txt"
code, said = runner("--report", str(report))
check(code == 1, "a failing suite fails the run")
check("PASS  test_good" in said and "FAIL  test_bad" in said and "FAIL something" in said,
      "each suite passes or fails on its own, a failure shown with its last lines")
check("FAIL  test_slow" in said and "timed out after 3s" in said, "a suite out of time is stopped and failed")
check("FAIL  test_asks" in said and "EOFError" in said, "a suite asking for input fails at once, not waiting")
check("test_console" not in said and "test_type" not in said, "the console and the typing check are never run")
check("26.5\u00b0C" in report.read_text(encoding="utf-8") and report.read_text(encoding="utf-8").count("=====") == 10,
      "the report keeps every suite's output in full")

code, said = runner("good", "words")
check(code == 0 and "2 of 2 suites passed." in said, "names choose suites, and all passing passes")

code, said = runner("--skip", "bad,slow,asks")
check(code == 0 and "test_bad" not in said, "--skip leaves them out")

os.environ["JARVIS_TEST_SKIP"] = "bad,slow,asks"

try:
    code, said = runner()
finally:
    os.environ.pop("JARVIS_TEST_SKIP")

check(code == 0, "and so does JARVIS_TEST_SKIP")

chosen = [path.stem for path in run_tests.suites(ROOT / "tools")]
check(len(chosen) > 30 and "test_console" not in chosen and "test_type" not in chosen and "test_run_tests" in chosen,
      f"in tools/: every suite but the console and the typing check ({len(chosen)})")

sys.exit(1 if failures else 0)
