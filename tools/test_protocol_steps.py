"""Protocol steps that speak and do: {"say"}, {"command"} and {"pause"}, for a demo protocol.

Runs in a sandboxed JARVIS folder with pretend hands and a pretend voice
(nothing is spoken and no OBS is touched). Checked:

- a demo protocol does its steps in order: OBS's recording started, words
  said, commands done as if said, pauses, the recording stopped;
- a command JARVIS doesn't understand is said once, at the end; an
  optional OBS step that fails is not;
- a command that starts a protocol is refused, so no protocol can run
  itself for ever;
- a pause is held to a minute, and one that is not a number says so;
- a protocol with no "clear" is never kept as engaged, so it runs again;
  one with a clear still is;
- with no voice wired in, a say step says why it could not;
- through commands: "initiate demo protocol" is a protocol, no model call.

    python tools/test_protocol_steps.py
"""

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import protocols  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


DEMO = {
    "aliases": ["demonstration", "tiktok"],
    "engage": [
        {"service": "obs", "wait": 10, "optional": True},
        {"service": "obs", "tool": "obs-start-record", "optional": True},
        {"say": "Good evening. Allow me to show you around."},
        {"command": "show me my files"},
        {"pause": 4},
        {"command": "close the files"},
        {"command": "how warm did my room get overnight"},
        {"pause": 600},
        {"command": "make me a sandwich"},
        {"say": "That's the tour."},
        {"service": "obs", "tool": "obs-stop-record", "optional": True},
    ],
    "say": "Demo complete, sir.",
}

with open(os.path.join(folder, "protocols.json"), "w", encoding="utf-8") as handle:
    json.dump({"protocols": {
        "demo": DEMO,
        "loop": {"engage": [{"command": "initiate demo protocol"}, {"command": "clean slate"}], "say": "Looped."},
        "pauses": {"engage": [{"pause": "soon"}], "say": "Paused."},
        "kept": {"engage": [{"say": "Kept."}], "say": "Kept, sir.", "clear": [{"say": "Cleared."}]},
    }}, handle)


class Hands(protocols.Hands):
    """Everything a step does, written down rather than done."""

    def __init__(self):
        self.done = []
        self.obs_up = True

    def wait_service(self, name, seconds):
        self.done.append(("wait", name))
        return self.obs_up

    def act(self, service, tool, arguments, then):
        self.done.append(("act", tool))
        return (True, None) if self.obs_up else (False, "OBS refused it")

    def pause(self, seconds):
        self.done.append(("pause", seconds))

    def still_open(self, record):
        return False


hands = Hands()
protocols.hands = hands
spoken, ran = [], []


def run(words):
    ran.append(words)
    hands.done.append(("command", words))
    return words != "make me a sandwich"


protocols.set_voice(say=lambda words: (spoken.append(words), hands.done.append(("say", words))), run=run)

check(protocols.asked("initiate demo protocol") == ("engage", "demo"), "the demo protocol is asked for by name")
check(protocols.asked("start the tiktok protocol") == ("engage", "demo"), "or an alias")

said = protocols.engage("demo")
check(hands.done == [
    ("wait", "obs"), ("act", "obs-start-record"),
    ("say", "Good evening. Allow me to show you around."),
    ("command", "show me my files"), ("pause", 4.0), ("command", "close the files"),
    ("command", "how warm did my room get overnight"), ("pause", protocols.MAX_PAUSE_SECONDS),
    ("command", "make me a sandwich"), ("say", "That's the tour."), ("act", "obs-stop-record"),
], f"every step in order, the long pause held to a minute ({hands.done})")
check(said == "Demo complete, sir. One step didn't go through: I didn't understand 'make me a sandwich'.",
      f"a command not understood is said once, at the end ({said!r})")

check(protocols.engaged() == [], "a protocol with nothing to clear is never kept as engaged")
hands.done.clear()
hands.obs_up = False
said = protocols.engage("demo")
check(("command", "show me my files") in hands.done and "OBS" not in said,
      f"so it runs again, and OBS not answering goes unsaid when its steps are optional ({said!r})")
hands.obs_up = True

hands.done.clear()
ran.clear()
said = protocols.engage("loop")
check(ran == [] and "can't start another protocol" in said, f"a protocol can't start a protocol ({said!r})")

said = protocols.engage("pauses")
check("isn't a number of seconds" in said, f"a pause that is not a number says so ({said!r})")

said = protocols.engage("kept")
check(protocols.engaged() == ["kept"], "a protocol with a clear is still kept as engaged")

protocols.set_voice()
said = protocols.engage("demo")
check("no voice" in said, f"with no voice wired in, a say step says why ({said!r})")

# ---- through commands -------------------------------------------------------------------------

try:
    import commands
except Exception as error:
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        result = commands.handle_command("initiate demo protocol")
        check(result["intent"] == "protocol" and result["response"] == "Initiating demo protocol, sir.",
              "commands: 'initiate demo protocol' is a protocol, no model call")
    finally:
        commands.run_agent = real_agent

sys.exit(1 if failures else 0)
