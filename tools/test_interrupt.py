"""Ctrl+C stops JARVIS: the first interrupt asks for a clean shutdown, a second forces it (main.py).

    python tools/test_interrupt.py

In the Qt event loop a KeyboardInterrupt is printed and the loop runs on, so Ctrl+C left JARVIS running
(14 October 2026). Checked, without a window or real signals:

- the handler asks for a clean shutdown on the first interrupt, and only forces on a second;
- a KeyboardInterrupt reaching the excepthook asks for the same clean shutdown, rather than being printed
  and swallowed; an ordinary fault is still reported and not fatal.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede the imports it stubs for)

sandbox.activate()

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def load_main():
    for name in (
        "sounddevice", "edge_tts", "soundfile", "pyttsx3",
        "commands", "news_panel", "phone", "camera_panel", "chart_panel",
        "image_panel", "image_choices_panel", "brain_panel", "beam",
        "transcriber", "agent", "outlook",
    ):
        sys.modules.setdefault(name, MagicMock())

    voice = types.ModuleType("voice")
    for name in ("arm_follow_up", "consume_follow_up_answer", "disarm", "last_was_named", "listen",
                 "set_follow_up_expired_listener", "set_level_listener", "set_status_listener",
                 "set_wake_listener", "user_talking", "is_muted", "set_muted"):
        setattr(voice, name, MagicMock())
    sys.modules["voice"] = voice
    sys.modules.setdefault("actions.phone_server", MagicMock())

    import main
    return main


main = load_main()


# ---- the handler: first asks, second forces -----------------------------------------------------
events = []
handle = main._interrupt_handler(lambda: events.append("ask"), lambda: events.append("force"))

handle()
check(events == ["ask"], "the first interrupt asks for a clean shutdown, and does not force")
handle()
check(events == ["ask", "force"], "a second interrupt forces it")
handle()
check(events == ["ask", "force", "force"], "and every one after forces too")


# ---- the excepthook: a KeyboardInterrupt asks for shutdown, a fault is reported ------------------
asked = []
with patch.object(main, "_request_stop", lambda: asked.append(True)):
    main._report_fault(KeyboardInterrupt, KeyboardInterrupt(), None)
check(asked == [True], "a KeyboardInterrupt at the excepthook asks for the clean shutdown, not a bare print")

# An ordinary fault is still reported once and never stops JARVIS.
main._faults_seen.clear()
with patch.object(main, "_request_stop", lambda: asked.append("fault-should-not-call")):
    try:
        raise ValueError("a panel drew badly")
    except ValueError as error:
        main._report_fault(ValueError, error, error.__traceback__)
check(asked == [True], "an ordinary fault is reported, not routed to shutdown")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
