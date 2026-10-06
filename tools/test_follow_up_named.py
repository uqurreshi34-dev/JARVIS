"""Whether a command came with JARVIS's name, and what a follow-up without it may do.

A conversation in the room -- friends, a phone call, a video -- used to
chain on: each overheard sentence was answered and opened the next follow-up
window, and "I had one croissant and two eggs" became a memory. Checked
against voice.py's own listener, with the microphone and speech models stood
in for, and main.py's settings read from its source:

- "Jarvis, ..." is named; a bare command in a follow-up window is not;
- his name alone, then a command, counts as named;
- what woke him is printed, so a false wake shows what he heard;
- main.py limits follow-ups without his name, keeps memory and diary writes
  for named commands, and ends the turn when a command is not understood.

    python tools/test_follow_up_named.py
"""

import contextlib
import io
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# voice.py reads the voiceprint from the JARVIS folder: a temporary one, never yours.
from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

sys.modules["sounddevice"] = MagicMock(name="sounddevice")
sys.modules["vosk"] = types.SimpleNamespace(KaldiRecognizer=MagicMock(), Model=MagicMock())
sys.modules["speech"] = types.SimpleNamespace(is_speaking=lambda: False, speech_epoch=lambda: 0)

import transcriber  # noqa: E402


class Engine:
    """Hands back one transcription per audio block, in order."""

    def __init__(self):
        self.said, self.active, self.name = [], False, "test"

    def reset(self):
        pass

    def feed(self, data):
        if not self.said:
            return None
        return types.SimpleNamespace(text=self.said.pop(0), confidence=None)


engine = Engine()
transcriber.build_engine = lambda model, block: engine

import voice  # noqa: E402

voice.REQUIRE_WAKE_WORD = True
voice._make_wake_recognizer = lambda: None
voice._wake_listener = None
voice.ACKNOWLEDGE_WAKE = False

failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def hear(*utterances, follow_up=False):
    """listen() over [utterances], one per audio block; (command, named, printed)."""
    engine.said = list(utterances)
    voice._drain_queue()
    voice._disarm()

    if follow_up:
        voice.arm_follow_up()

    real_drain = voice._drain_queue

    def refill():
        real_drain()
        for _ in engine.said:
            voice.audio_queue.put(b"\x00\x00" * 8)

    voice._drain_queue = refill
    printed = io.StringIO()

    try:
        with contextlib.redirect_stdout(printed):
            command = voice.listen()
    finally:
        voice._drain_queue = real_drain

    return command, voice.last_was_named(), printed.getvalue()


command, named, printed = hear("jarvis what time is it")
check(command == "what time is it" and named and '[wake] woken by "jarvis what time is it"' in printed,
      "addressed by name: named, and what woke him is printed")

command, named, _printed = hear("i had one croissant and two eggs", follow_up=True)
check(command == "i had one croissant and two eggs" and not named,
      "a bare command in a follow-up window: taken, but not named")

command, named, _printed = hear("jarvis", "remember i like pasta")
check(command == "remember i like pasta" and named, "his name alone, then the command: named")

command, named, printed = hear("what did you eat today", "jarvis open chrome")
check(command == "open chrome" and named and '[ignored] "what did you eat today"' in printed,
      "talk not meant for him is ignored, outside a follow-up")

# The wake grammar hears "jarvis" in "oh yeah"; the transcript has nothing like it, so he is not woken.
class Waker:
    def AcceptWaveform(self, data):
        return True

    def Result(self):
        return '{"text": "jarvis"}'

    def Reset(self):
        pass


voice._make_wake_recognizer = lambda: Waker()
command, named, printed = hear("oh yeah", "jarvis open chrome")
check(command == "open chrome" and '[ignored] "oh yeah"' in printed and "Woken" not in printed,
      "'oh yeah' heard as the name by the wake grammar does not wake him")
voice._make_wake_recognizer = lambda: None

# ---- main.py's rules, read from its source (it needs the desktop to import) ------------------------

source = (ROOT / "main.py").read_text(encoding="utf-8")
turns = re.search(r"^FOLLOW_UP_TURNS = (\d+)", source, re.MULTILINE)
named_only = re.search(r"^NAMED_ONLY_INTENTS = frozenset\(\{(.*?)\}\)", source, re.MULTILINE | re.DOTALL)
check(turns and int(turns.group(1)) >= 1, "follow-ups without his name are limited (FOLLOW_UP_TURNS)")
check(named_only and all(f'"{intent}"' in named_only.group(1) for intent in ("remember", "make_note", "add_event")),
      "remembering, notes and diary entries need his name in a follow-up")
check("self._unnamed_turns < FOLLOW_UP_TURNS" in source and "named = last_was_named()" in source,
      "the follow-up window opens only while the run without his name is short")
unknown = source.split('self._say(phrases.pick("unknown"))', 1)[1].split("continue", 1)[0]
check("self._close_turn()" in unknown,
      "a command not understood ends the turn: no window left open, queued announcements said")

phrases = (ROOT / "phrases.py").read_text(encoding="utf-8")
check('"name_first"' in phrases, "and there is something to say when his name is needed")

sys.exit(1 if failures else 0)
