"""The local voice is loaded at start-up only when it speaks; as a stand-in, only the first time it is needed.

    python tools/test_voice_startup.py

Its model import holds Python's lock for a second or two, which froze the HUD during the boot animation
(14 October 2026). Checked, with the voice replaced by a counter:

- with a network voice chosen, neither start_voice nor prewarm loads the local one: while the network voice
  works, it is never imported;
- the first time the network voice fails, the fallback starts the local voice loading and waits for it, so
  the reply is in the good voice, not the Windows one;
- with the local voice chosen, start_voice and prewarm load it at once, as before -- the greeting waits for it.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede the imports it stubs for)

sandbox.activate()

# The audio libraries are not present off Windows (no PortAudio); speech.py imports them at module load.
# setdefault leaves the real ones in place on Windows, and stands in for them here.
for _name in ("sounddevice", "soundfile", "edge_tts"):
    sys.modules.setdefault(_name, MagicMock())

import speech  # noqa: E402
import voices  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


class Counter:
    """Stands in for the local voice: counts how it was asked to load, and what it was asked to say."""

    def __init__(self):
        self.background = 0
        self.blocking = 0
        self.waited = 0
        self.loaded = False
        self.said = []

    def load_in_background(self):
        self.background += 1
        self.loaded = True

    def load(self, **_):
        self.blocking += 1
        self.loaded = True
        return True

    def ready(self):
        return self.loaded

    def wait(self, timeout):
        self.waited += 1
        return self.loaded

    def audio(self, text):
        self.said.append(text)
        return [0.0], 24000, []


def start_up(engine):
    """What start-up asks of the local voice with [engine] chosen: start_voice, then prewarm."""
    fake = Counter()

    with patch.object(speech.speech, "engine", engine), \
         patch.object(voices, "local", lambda: fake), \
         patch.object(voices, "describe", lambda *_: f"{engine} voice"), \
         patch.object(speech.speech, "prewarm", lambda *_: None):
        speech.start_voice()
        speech.prewarm(lines=[])

    return fake


fake = start_up("google")
check(fake.background == 0 and fake.blocking == 0,
      "a network voice does not load the local one at start-up -- not in start_voice, not in prewarm")

fake = start_up("kokoro")
check(fake.background == 1, "the local voice, when chosen, is loaded by start_voice -- the greeting waits for it")
check(fake.blocking == 1, "and prewarm makes its phrases in it")


# The first failure of the network voice: the fallback loads the stand-in and says the line in it.
fake = Counter()

with patch.object(speech.speech, "engine", "google"), \
     patch.object(voices, "local", lambda: fake), \
     patch.object(speech.speech, "_play_reactive", lambda *a, **k: None) as played, \
     patch.object(speech.speech, "_speak_windows", lambda *a, **k: fake.said.append("WINDOWS")):
    speech._stop.clear()
    speech.speech._speak_fallback("the network just failed")

check(fake.background == 1 and fake.waited == 1,
      "a failure starts the stand-in loading and waits for it, there and then")
check(fake.said == ["the network just failed"],
      f"and the reply is said in the stand-in, not the Windows voice ({fake.said})")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
