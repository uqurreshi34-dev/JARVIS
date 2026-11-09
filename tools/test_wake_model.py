""""Hey Jarvis" heard on this PC before anything is written out (wake_model.py, voice.py, transcriber.py).

Nothing is downloaded and no model is run: the download and the detector are stood in for.

- the three files are fetched once, checked against their SHA-256, and kept;
- a file that is not what openWakeWord released is refused, and nothing is kept;
- JARVIS_WAKE_MODEL=off turns the model off, and the threshold stays between 0.05 and 0.99;
- an utterance with "Hey Jarvis" in it, or just before it, or said in a follow-up window, is written out;
  one without it is not -- the transcriber is never asked -- and is counted as kept on the PC.

    python tools/test_wake_model.py
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

sys.modules["sounddevice"] = MagicMock(name="sounddevice")
sys.modules["vosk"] = types.SimpleNamespace(KaldiRecognizer=MagicMock(), Model=MagicMock())
sys.modules["speech"] = types.SimpleNamespace(is_speaking=lambda: False, speech_epoch=lambda: 0)

import numpy as np  # noqa: E402

import request_log  # noqa: E402
import transcriber  # noqa: E402
import wake_model  # noqa: E402

request_log.enable()

transcriber.build_engine = lambda model, block: MagicMock(name="engine")

import voice  # noqa: E402

failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- fetching ----------------------------------------------------------------
contents = {name: f"pretend {name}".encode() for name, _ in wake_model.FILES.values()}
real_files = wake_model.FILES
wake_model.FILES = {key: (name, hashlib.sha256(contents[name]).hexdigest()) for key, (name, _) in real_files.items()}
asked = []


def download(url):
    asked.append(url)
    return contents[url.rsplit("/", 1)[1]]


where = tempfile.mkdtemp(prefix="jarvis-wake-")
paths = wake_model.fetch(where, download=download)
check(len(asked) == 3 and all(url.startswith(wake_model.RELEASE) for url in asked),
      "the three files are fetched from openWakeWord's own release")
check(set(paths) == {"melspectrogram", "embedding", "hey_jarvis"} and all(os.path.exists(p) for p in paths.values()),
      "and kept in the folder")
check(not any(name.endswith(".part") for name in os.listdir(where)), "with no half-written file left behind")

asked.clear()
wake_model.fetch(where, download=download)
check(not asked, "the second time nothing is fetched: they work offline")

with open(paths["embedding"], "wb") as handle:
    handle.write(b"damaged")

wake_model.fetch(where, download=download)
check(len(asked) == 1 and asked[0].endswith("embedding_model.onnx"), "a damaged file is fetched again")

other = tempfile.mkdtemp(prefix="jarvis-wake-")

try:
    wake_model.fetch(other, download=lambda url: b"something else")
    check(False, "a file that is not openWakeWord's is refused")
except RuntimeError as error:
    check("SHA-256" in str(error), "a file that is not openWakeWord's is refused")

check(not os.listdir(other), "and nothing of it is kept")
wake_model.FILES = real_files

check(all(len(digest) == 64 for _, digest in wake_model.FILES.values()), "each real file has its SHA-256 written down")

# ---- settings ----------------------------------------------------------------
for value, expected in ((None, True), ("hey_jarvis", True), ("off", False), ("OFF", False), ("no", False)):
    if value is None:
        os.environ.pop("JARVIS_WAKE_MODEL", None)
    else:
        os.environ["JARVIS_WAKE_MODEL"] = value

    check(wake_model.wanted() is expected, f"JARVIS_WAKE_MODEL={value!r}: {'on' if expected else 'off'}")

os.environ.pop("JARVIS_WAKE_MODEL", None)

for value, expected in ((None, 0.5), ("0.7", 0.7), ("5", 0.99), ("0", 0.05), ("loud", 0.5)):
    if value is None:
        os.environ.pop("JARVIS_WAKE_THRESHOLD", None)
    else:
        os.environ["JARVIS_WAKE_THRESHOLD"] = value

    check(wake_model.threshold() == expected, f"JARVIS_WAKE_THRESHOLD={value!r} -> {expected}")

os.environ.pop("JARVIS_WAKE_THRESHOLD", None)
check(not hasattr(voice.engine, "gate") or voice.engine.gate is None or isinstance(voice.engine, MagicMock),
      "a stand-in engine is not given the wake-word model")
check(wake_model.status() == "not started", "nor is the model started for it")
check(not voice._wake["started"], "importing voice.py starts nothing: the model loads when listening begins")

# ---- the gate ----------------------------------------------------------------


class Detector:
    def __init__(self):
        self.next_score = 0.0
        self.resets = 0

    def feed(self, block):
        return self.next_score

    def reset(self):
        self.resets += 1


detector = Detector()
voice.REQUIRE_WAKE_WORD = True
voice._armed_until = 0.0
voice._wake_ready(detector)
check(voice.engine.gate is voice._gate, "once loaded, the engine asks the wake-word model first")

began = time.monotonic()
check(not voice._gate(began), "no \"Hey Jarvis\": not for JARVIS")

detector.next_score = 0.95
voice._hear_wake(b"\0" * 2560)
check(voice._gate(began), "\"Hey Jarvis\" heard during it: written out")
check(voice._gate(voice._wake["heard_at"] + 1.5), "or heard just before it began")
check(not voice._gate(voice._wake["heard_at"] + 5), "but not one that began long after")

voice._reset_wake()
check(detector.resets == 1 and not voice._gate(began), "after JARVIS speaks the model forgets what it heard")

voice._wake["armed_at_start"] = True
check(voice._gate(began), "said in a follow-up window: no \"Hey Jarvis\" needed")
voice._wake["armed_at_start"] = False

detector.next_score = 0.2
voice._hear_wake(b"\0" * 2560)
check(not voice._gate(began), "a low score is not the wake word")


class Broken(Detector):
    def feed(self, block):
        raise RuntimeError("broken")


voice._wake_ready(Broken())
voice._hear_wake(b"\0" * 2560)
check(voice._wake["detector"] is None and voice.engine.gate is None,
      "a broken model is put aside and the name is found in the words, as before")

# ---- the engine: an utterance turned away is never written out --------------


class Engine(transcriber.SegmentingEngine):
    name = "test-whisper"

    def __init__(self):
        super().__init__(0.25)
        self.asked = 0

    def _transcribe(self, audio):
        self.asked += 1
        self._used_cloud = True
        return "What time is it."


test_engine = Engine()
test_engine._buffer = [np.zeros(transcriber.SAMPLE_RATE * 2, dtype=np.float32)]
test_engine._started = time.monotonic()
test_engine.gate = lambda began: False
check(test_engine._finish() is None and test_engine.asked == 0, "turned away: the transcriber is never asked")

with open(request_log._ledger(datetime.now()), encoding="utf-8") as handle:
    lines = [json.loads(line) for line in handle]

check(lines and lines[-1]["kind"] == "ignored" and lines[-1]["online"] is False and lines[-1]["audio_s"] == 2.0,
      "and counted as kept on the PC")

test_engine._buffer = [np.zeros(transcriber.SAMPLE_RATE * 2, dtype=np.float32)]
test_engine._started = time.monotonic()
test_engine.gate = lambda began: True
result = test_engine._finish()
check(result is not None and result.text == "what time is it" and test_engine.asked == 1, "let through: written out")
check(test_engine.last_heard["online"] is True and test_engine.last_heard["audio_seconds"] == 2.0,
      "and how it was written out is kept for the request")

check(sandbox.assert_clean(), "the real JARVIS folder was not touched")
sys.exit(1 if failures else 0)
