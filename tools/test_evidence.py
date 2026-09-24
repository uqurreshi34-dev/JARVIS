"""Offline tests for cutting research pages down to their evidence.

No network, no model calls. A made-up page with a long menu and cookie
notice at the top, and the facts that matter further down, checks that:

- the facts are kept, where the old first-7000-characters cut missed them;
- the budget is respected, and pages with nothing relevant send nothing;
- kept text is word for word from the page, in the page's order;
- a passage repeated on a second page is sent once;
- the research report prompt is built from the reduced evidence.

It runs with the local embedding model when this PC has it, and with the
word-overlap fallback otherwise; both are checked where possible.

    python tools/test_evidence.py
"""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import evidence  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


MENU = "\n".join(
    f"Home | News | Sport | Weather | Sign in | Menu item {i} | Subscribe | Cookie settings"
    for i in range(120)
)
FILLER = " ".join(
    f"Readers also enjoyed story number {i} about gardening tips and holiday recipes."
    for i in range(40)
)
FACTS = (
    "The Model Y Long Range is rated at 331 miles on the EPA cycle for 2025. "
    "Tesla reported 1.79 million deliveries in 2024, a fall of about one per cent. "
    "The Ioniq 5 offers up to 318 miles of range with its larger 84 kWh battery. "
    "Hyundai said charging from 10 to 80 per cent takes about 18 minutes."
)
PAGE = f"{MENU}\n\n{FILLER}\n\n{FACTS}\n\n{FILLER}"
FOCUS = "compare Tesla Model Y and Hyundai Ioniq 5 range deliveries charging"


def run_suite(label):
    reduced = evidence.reduce(PAGE, FOCUS, 1200)

    check("331 miles" not in PAGE[:7000], f"[{label}] the old 7,000-character cut would have missed the facts")
    check("331 miles" in reduced and "18 minutes" in reduced, f"[{label}] the facts deep in the page are kept")
    check(len(reduced) <= 1200 + 20, f"[{label}] the budget is respected ({len(reduced)} chars)")
    check("Cookie settings" not in reduced and "gardening" not in reduced, f"[{label}] menus and unrelated stories are dropped")

    flat = " ".join(PAGE.split())
    pieces = [piece.strip() for piece in reduced.split("[...]") if piece.strip()]
    check(all(piece in flat for piece in pieces), f"[{label}] every kept passage is word for word from the page")
    positions = [flat.index(piece) for piece in pieces]
    check(positions == sorted(positions), f"[{label}] kept passages stay in the page's order")

    seen = set()
    first = evidence.reduce(PAGE, FOCUS, 1200, seen)
    second = evidence.reduce(f"{MENU}\n\n{FACTS}", FOCUS, 1200, seen)
    check(first and "331 miles" not in second, f"[{label}] a passage already sent from one page is not sent again")

    check(evidence.reduce(MENU + "\n" + FILLER, "quantum chromodynamics lattice", 1200) == "",
          f"[{label}] a page with nothing relevant sends nothing")


semantic_available = evidence._semantic("test", ["a passage"]) is not None

if semantic_available:
    run_suite("embedding model")

original = evidence._semantic
evidence._semantic = lambda focus, chunks: None
run_suite("word overlap")
evidence._semantic = original

check(evidence.reduce("", FOCUS, 500) == "", "empty page gives empty evidence")

# The report prompt is built from reduced evidence, not raw pages. Importing
# research builds the provider pool, which needs a key to exist; a dummy one
# is enough because the model call below is replaced.
if not any(os.getenv(k) for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_FOUNDRY_API_KEY")):
    os.environ["GROQ_API_KEY"] = "test-key"

try:
    from actions import research
except Exception as error:  # needs JARVIS's full environment
    print(f"SKIP research wiring (could not import research: {error})")
else:
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[-1]["content"]
        return "report"

    research._chat = fake_chat
    sources = [
        {"title": "Range test", "url": "https://example.com/a", "query": "Model Y range", "text": PAGE},
        {"title": "Charging", "url": "https://example.com/b", "query": "Ioniq 5 charging", "text": PAGE},
    ]
    research._report("Compare the Tesla Model Y and Hyundai Ioniq 5", ["Tesla Model Y", "Hyundai Ioniq 5"], sources)
    prompt = captured.get("prompt", "")
    check("331 miles" in prompt, "the report prompt carries the facts")
    check(len(prompt) < len(PAGE), f"the report prompt is far smaller than the raw pages ({len(prompt)} vs {2 * len(PAGE)} chars)")
    check(prompt.count("331 miles") == 1, "the same fact from two pages is sent once")

print(f"(embedding model {'used' if semantic_available else 'not available here; word overlap tested'})")
sys.exit(1 if failures else 0)
