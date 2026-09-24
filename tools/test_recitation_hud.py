"""What the HUD reads while the Quran is recited, paused and finished.

Two bugs this guards:

- A single short verse (2:1) showed standby for the whole time it was
  sounding. The recitation claimed the HUD when it began, and a moment
  later the end of the command turn that started it put the HUD back to
  standby. A long surah hid this, because every new verse claimed the HUD
  again; one verse never got a second chance.
- Pausing left the HUD on reciting although nothing was sounding.

The rule under test is recitation.owns_hud(), which main.py's _state uses
to turn a request for standby or listening into reciting. Real sessions
run on real threads here; only the audio and the verse fetch are stand-ins,
so no sound is made and no network is used.

    python tools/test_recitation_hud.py
"""

import sys
import threading
import time
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---- stand-ins for sound and the verse service ----------------------------

class _Speech(types.ModuleType):
    """Plays a 'verse' by waiting, and can be paused, resumed and stopped."""

    def __init__(self):
        super().__init__("speech")
        self.play_seconds = 0.3
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()

    def play_file(self, path):
        self._stop.clear()
        end = time.monotonic() + self.play_seconds
        while time.monotonic() < end:
            if self._stop.is_set():
                return False
            if not self._resume.is_set():
                paused_at = time.monotonic()
                self._resume.wait()
                end += time.monotonic() - paused_at
            time.sleep(0.01)
        return True

    def pause_speaking(self):
        self._resume.clear()

    def resume_speaking(self):
        self._resume.set()

    def stop_speaking(self):
        self._stop.set()
        self._resume.set()


quran = types.ModuleType("actions.quran")
quran.DEFAULT_RECITER = "ar.alafasy"
quran.surah = lambda number: {"englishName": "Al-Baqarah"}
quran.verse_count = lambda number: 286
quran.bounds_message = lambda surah, ayah: None
quran.cache_surah_text = lambda *a, **k: None
quran.fetch_verse = lambda surah, ayah, reciter=None: {"audio": f"{surah}-{ayah}.mp3", "text": ""}

speech = _Speech()
sys.modules["speech"] = speech

import actions  # noqa: E402

sys.modules["actions.quran"] = quran
actions.quran = quran

from actions import recitation  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def wait_for(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


# What main.py's _state does with a request, given owns_hud().
def shown(requested):
    if requested in ("idle", "listening") and recitation.owns_hud():
        return "reciting"
    return requested


# ---- one short verse -------------------------------------------------------

seen_at_end = {}


def on_end(reason, state):
    # Asked from inside the ending session, exactly as main.py does.
    seen_at_end["owns"] = recitation.owns_hud()
    seen_at_end["reason"] = reason


recitation.set_listeners(on_end=on_end)

speech.play_seconds = 0.4
session = recitation.begin(2, ayah=1, auto=False)

check(recitation.owns_hud(), "a recitation owns the HUD from the moment it begins")
# The command turn that started it now finishes and asks for standby.
check(shown("idle") == "reciting", "the end of the command turn no longer puts a short verse on standby")
check(shown("listening") == "reciting", "the microphone re-arming no longer shows listening over the verse")
check(shown("speaking") == "speaking" and shown("thinking") == "thinking", "speaking and thinking still show during a recitation")

wait_for(lambda: not session.running)
check(seen_at_end.get("reason") == "verse", "the single verse finishes normally")
check(seen_at_end.get("owns") is False, "when it ends, standby is allowed through straight away")
check(shown("idle") == "idle" and shown("listening") == "listening", "after the verse, the HUD returns to standby or listening")

# ---- a whole surah, paused and resumed ------------------------------------

speech.play_seconds = 0.3
session = recitation.begin(2, ayah=1, auto=True)
wait_for(lambda: session._playing)

session.pause()
check(not recitation.owns_hud(), "paused: the recitation lets go of the HUD")
check(shown("idle") == "idle", "paused shows standby")
check(shown("listening") == "listening", "paused still shows listening when the microphone is waiting")

ayah = session.ayah
time.sleep(0.5)
check(session.ayah == ayah, "nothing moves on while paused")

session.resume()
check(recitation.owns_hud() and shown("idle") == "reciting", "resumed: back to reciting")

recitation.stop()
wait_for(lambda: not session.running)
check(not recitation.owns_hud(), "stopped: standby again")

sys.exit(1 if failures else 0)
