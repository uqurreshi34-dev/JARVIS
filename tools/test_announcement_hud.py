"""An unprompted announcement shows on the HUD for as long as it is spoken (main.py).

A Bitcoin move announced just after start-up was spoken with the HUD on
standby and its words not shown: the follow-up window expired on the voice
thread mid-sentence and set the HUD to standby. Checked, with speech and
listening simulated:

- the HUD shows the announcement's words and speaking while it is said;
- standby asked for meanwhile (the follow-up window expiring) waits until
  it has been said, then takes effect;
- an announcement that comes while something else is being said waits for
  the voice before taking the HUD, so its words are not shown over another's;
- with nothing asked for meanwhile, the HUD goes back to how it was.

    python tools/test_announcement_hud.py
"""

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def _load_main():
    """Import main.py with its heavy neighbours replaced, as tools/test_stop_button.py does.

    No sound is made here: speaking is replaced in every check.
    """
    for name in (
        "sounddevice", "edge_tts", "soundfile", "pyttsx3",
        "commands", "news_panel", "phone", "camera_panel", "chart_panel",
        "image_panel", "image_choices_panel", "brain_panel", "beam",
        "transcriber", "agent", "outlook",
    ):
        sys.modules.setdefault(name, MagicMock())

    voice = types.ModuleType("voice")

    for name in (
        "arm_follow_up", "consume_follow_up_answer", "disarm", "last_was_named", "listen",
        "set_follow_up_expired_listener", "set_level_listener",
        "set_status_listener", "set_wake_listener",
    ):
        setattr(voice, name, MagicMock())

    sys.modules["voice"] = voice
    sys.modules.setdefault("actions.phone_server", MagicMock())

    import main

    return main


class Hud:
    """What the HUD was told, in order."""

    def __init__(self):
        self.told = []

        for name in ("heard_changed", "shutdown", "boot_requested", "boot_updated"):
            setattr(self, name, MagicMock())

        self.state_changed = types.SimpleNamespace(emit=lambda state: self.told.append(("state", state)))
        self.reply_changed = types.SimpleNamespace(emit=lambda text: self.told.append(("reply", text)))

    def last(self, kind):
        found = [value for what, value in self.told if what == kind]
        return found[-1] if found else None


main = _load_main()
hud = Hud()
assistant = main.Assistant(hud)
during = {}


def speaking(text):
    """The voice: while the Bitcoin line is said, the follow-up window expires on the voice thread."""
    if text.startswith("Bitcoin"):
        assistant._on_status("idle")
        during.update(state=hud.last("state"), reply=hud.last("reply"))


with patch.object(main, "speak", side_effect=speaking), patch.object(main, "phone_server", MagicMock()):
    assistant._state(main.LISTENING)
    assistant._on_alert("Bitcoin is up 0.3 percent since 20:51, sir.")

check(during == {"state": main.SPEAKING, "reply": "Bitcoin is up 0.3 percent since 20:51, sir."},
      f"while it is said: speaking, and its words shown, though standby was asked for ({during})")
check(hud.last("state") == main.IDLE, "and once said, the standby that was asked for meanwhile")

hud.told.clear()

with patch.object(main, "speak"), patch.object(main, "phone_server", MagicMock()):
    assistant._state(main.LISTENING)
    assistant._on_alert("XRP is down 0.9 percent, sir.")

check(hud.last("state") == main.LISTENING, "with nothing asked for meanwhile, back to how it was")

# Something else is being said when the announcement comes.
order = []
greeting_started = threading.Event()
release = threading.Event()


def voice(text):
    order.append(("speak", text))

    if text.startswith("Good evening"):
        greeting_started.set()
        release.wait(5)


hud.told.clear()

with patch.object(main, "speak", side_effect=voice), patch.object(main, "phone_server", MagicMock()):
    greeting = threading.Thread(target=assistant._say, args=("Good evening. JARVIS is online.",))
    greeting.start()
    greeting_started.wait(5)
    alert = threading.Thread(target=assistant._on_alert, args=("Bitcoin is down 0.1 percent, sir.",))
    alert.start()
    time.sleep(0.2)
    shown_while_greeting = hud.last("reply")
    release.set()
    greeting.join(5)
    alert.join(5)

check(shown_while_greeting == "Good evening. JARVIS is online.",
      "an announcement waits for the voice: the greeting's words stay while the greeting is said")
check([text for _what, text in order] == ["Good evening. JARVIS is online.", "Bitcoin is down 0.1 percent, sir."]
      and hud.last("reply") == "Bitcoin is down 0.1 percent, sir.", "then it takes the HUD and is said")

sys.exit(1 if failures else 0)
