"""The wake word addresses JARVIS only at the start of what is said.

"Open a github issue on jarvis called mark2", said while he was listening
for a follow-up, was cut at "jarvis" and heard as "called mark2". His name
inside a sentence is a name being used, not him being called.

Checked against voice.py's own splitter, with the microphone and speech
models stood in for, so nothing is heard or loaded:

- "Jarvis, ..." and "hey Jarvis ..." are him being called, and the rest is
  the command;
- "jarvis" further in is part of the command, kept whole;
- a mention inside the sentence is not taken for the wake grammar's hit.

    python tools/test_wake_position.py
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Stand-ins: no microphone, no Vosk model, no voice, no transcription engine.
sys.modules["sounddevice"] = MagicMock(name="sounddevice")
sys.modules["vosk"] = types.SimpleNamespace(KaldiRecognizer=MagicMock(), Model=MagicMock())
sys.modules["speech"] = types.SimpleNamespace(is_speaking=lambda: False, speech_epoch=lambda: 0)

import transcriber  # noqa: E402

transcriber.build_engine = lambda model, block: MagicMock(name="engine")

import voice  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


ADDRESSED = {
    "jarvis open chrome": "open chrome",
    "jarvis, what time is it": "what time is it",
    "hey jarvis what's the weather": "what's the weather",
    "ok so jarvis read my notes": "read my notes",
    "jarvis open a github issue on jarvis called mark2": "open a github issue on jarvis called mark2",
}

for said, command in ADDRESSED.items():
    addressed, remainder = voice._split_wake(said)
    check(addressed and remainder == command, f"addressed: {said!r} -> {remainder!r}")

NOT_ADDRESSED = [
    "open a github issue on jarvis called mark2",
    "add a comment to github issue five on jarvis saying fixed",
    "close issue five on my jarvis github repository",
    "list the open issues in the jarvis repo please",
]

for said in NOT_ADDRESSED:
    addressed, _ = voice._split_wake(said)
    check(not addressed, f"a mention, not a call: {said!r}")
    check(voice._mentions_wake(said), f"but still seen as a mention: {said!r}")

check(not voice._mentions_wake("open chrome"), "an ordinary command mentions no wake word")

# What the listening loop does with a follow-up: a command with no address
# is used whole.
said = "open a github issue on jarvis called mark2"
addressed, remainder = voice._split_wake(said)
command = remainder if addressed and remainder else said
check(command == said, f"a follow-up naming the repo is heard whole: {command!r}")

sys.exit(1 if failures else 0)
