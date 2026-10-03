"""Which requests write a research report, and into which folder.

No network: only the decision is checked, and the route commands takes,
never the research itself. Checked:

- a research word and a way of keeping the result is a report, however
  the keeping is said: "save it", "saved the findings" (as speech
  recognition hears it), "save to the football folder", "store the
  results", "write it up", "a report on";
- the folder named is the one used, after "the", "my" or "our";
- research with nothing kept is learning facts, not a report; keeping
  with nothing researched is not a report;
- through commands: such a request is the research report, not the
  multi-step planner, whose first step, a web search, it cannot run.

    python tools/test_research_routing.py
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import research  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


REPORTS = {
    "research manchester city football club and saved the findings in the football folder": "football",
    "research manchester city football club, compare with liverpool football club and save to the football folder": "football",
    "research the best ESP32 boards and save it in the AI folder": "AI",
    "research electric cars and save the report": None,
    "compare bitcoin and ethereum and write a report": None,
    "research deep sleep and store the results in my health folder": "health",
    "look into solar panels and write it up": None,
    "investigate the history of birmingham and put it in our history folder": "history",
    "research manchester city and keep the findings": None,
    "give me a research report on quantum computing": None,
}

for spoken, folder in REPORTS.items():
    check(research.is_report_request(spoken), f"a report: {spoken!r}")
    found = research._requested_folder(spoken)
    check((found is None and folder is None) or (found is not None and Path(found).name == folder),
          f"  into {folder or 'the JARVIS folder'} ({found})")

for spoken in ("research manchester city football club", "learn about the roman empire",
               "compare liverpool and everton", "save it in the cars folder", "save the chart",
               "research liverpool",
               "open the football folder"):
    check(not research.is_report_request(spoken), f"not a report: {spoken!r}")

try:
    import commands
except Exception as error:
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent called"))

    try:
        for spoken in list(REPORTS)[:2]:
            result = commands.handle_command(spoken)
            check(result is not None and result["intent"] == "research_report",
                  f"commands: the research report, not the planner ({result and result['intent']})")

        result = commands._fast_path("research manchester city football club")
        check(result is not None and result["intent"] == "learn_subject", "and plain research still learns facts")
    finally:
        commands.run_agent = real_agent

sys.exit(1 if failures else 0)
