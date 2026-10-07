"""JARVIS answers only the enrolled voice (speaker.py, tools/enroll_voice.py).

A trading video said "...Jarvis, I was a consecutively unprofitable
trader...", which woke him and was saved as a memory: the wake word alone
cannot tell you from a video. Checked, with the speaker model stood in for
(the real one is a 26 MB download) and a sandboxed JARVIS folder:

- the features are Kaldi's 80 filterbanks over speech, the room's quiet left
  out, and too little audio is too little to judge;
- an utterance scoring below the threshold is dropped -- before it is sent
  to be transcribed -- and one at or above it goes through;
- with no voiceprint, or the check switched off, everyone is answered, as
  before; with a voiceprint that cannot be used, or a model that will not
  load, nobody is: never everybody;
- the voiceprint is checked when read, and a different model's refused;
- the model is used only if its SHA-256 is the one expected, and a download
  that is not is thrown away;
- you, let through, and near misses go in jarvis-log.txt with their scores;
  clear misses (a video) do not, or they would fill it;
- enrolling saves the averaged voiceprint and says how your own clips score;
  --add records more clips and adds them to the ones you have (an older
  voiceprint counting as its clips); --report reads the scores back;
  --on, --off and --threshold change only what they say;
- the listener takes a dropped utterance as nothing heard, so a wake-word
  hit on someone else's voice does not wake him for the next thing said;
- the voiceprint and the model are never tidied away.

    python tools/test_voice_check.py
"""

import contextlib
import io
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

import speaker  # noqa: E402
from actions import folder_organizer  # noqa: E402
from tools import enroll_voice  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def quietly(call, *args, **kwargs):
    printed = io.StringIO()

    with contextlib.redirect_stdout(printed):
        result = call(*args, **kwargs)

    return result, printed.getvalue()


def unit(*values):
    vector = np.zeros(256)
    vector[:len(values)] = values
    return vector / np.linalg.norm(vector)


rng = np.random.default_rng(7)
SECOND = speaker.SAMPLE_RATE


def voice(seconds, pitch=180.0):
    """A buzzy tone, loud enough to count as speech."""
    times = np.arange(int(seconds * SECOND)) / SECOND
    return (0.3 * np.sign(np.sin(2 * np.pi * pitch * times)) + 0.01 * rng.standard_normal(len(times))).astype(np.float32)


# ---- features --------------------------------------------------------------------------------------

banks = speaker.features(voice(1.0))
check(banks is not None and banks.shape[1] == 80 and abs(float(banks.mean())) < 1e-4 and banks.dtype == np.float32,
      "80 filterbanks per 10 ms, the mean taken off")
padded = np.concatenate([np.zeros(2 * SECOND, dtype=np.float32) + 1e-5 * rng.standard_normal(2 * SECOND).astype(np.float32),
                         voice(1.0)])
check(abs(len(speaker.features(padded)) - len(banks)) <= 3, "two seconds of room before the speech are left out")
check(speaker.features(voice(0.02)) is None and speaker.features(voice(0.2)) is None, "too little audio is too little to judge")
check(np.allclose(speaker._BANK.sum(axis=0)[-1], 0.0) and speaker._BANK.shape == (80, 257),
      "Kaldi's mel bank, the Nyquist bin left out")

# ---- the gate --------------------------------------------------------------------------------------


class Model:
    """Stands in for the speaker model: says how the audio sounds, by its length."""

    def __init__(self, voices=None):
        self.voices = voices or {}

    def embed(self, audio):
        if len(audio) < SECOND // 4:
            return None
        return self.voices.get(len(audio), unit(1.0))


you, someone = unit(1.0, 0.2), unit(0.1, 1.0)
gate = speaker.Gate(unit(1.0, 0.0), 0.5, Model({SECOND: you, 2 * SECOND: someone}))
passed, printed = quietly(gate, np.zeros(SECOND))
check(passed and gate.last_score > 0.9 and not printed, "your voice goes through, unremarked")
passed, printed = quietly(gate, np.zeros(2 * SECOND))
check(not passed and "not your voice" in printed and "0.10" in printed, f"another voice is dropped, and says so ({printed.strip()})")
passed, printed = quietly(gate, np.zeros(100))
check(not passed and "too short" in printed, "too short to tell is dropped, not let through")
broken = speaker.Gate(unit(1.0), 0.5, types.SimpleNamespace(embed=lambda audio: 1 / 0))
passed, printed = quietly(broken, np.zeros(SECOND))
check(not passed and "failed" in printed, "a check that fails drops the utterance")
check(not quietly(speaker.Closed("no model"), np.zeros(SECOND))[0], "a check that cannot run drops everything")

from actions import journal  # noqa: E402

near = speaker.Gate(unit(1.0, 0.0), 0.5, Model({3 * SECOND: unit(0.42, np.sqrt(1 - 0.42 ** 2))}))
passed, _printed = quietly(near, np.zeros(3 * SECOND))
logged = [line for line in journal.recent(50) if f" {speaker.JOURNAL_KIND} " in line]
check(not passed and len(logged) == 2 and "yours: sounds 0.98 like you, 0.50 needed, 1.0s" in logged[0]
      and "near miss: sounds 0.42 like you, 0.50 needed, 3.0s" in logged[1] and logged[1].endswith("-> ignored"),
      "you, and a near miss, are logged with their scores; another voice, far off, is not")

# ---- whether there is a check ------------------------------------------------------------------------

path = speaker.voiceprint_path()
check(path == os.path.join(folder, "voiceprint.json"), "the voiceprint lives in the JARVIS folder")
made, printed = quietly(speaker.gate, model_factory=Model)
check(made is None and "voice check off" in printed and "enroll_voice" in printed,
      "no voiceprint: everyone answered, as before, and start-up says how to change that")


def write(saved):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(saved, handle)


good = {"enabled": True, "threshold": 0.6, "voiceprint": list(unit(1.0, 1.0)), "model_sha256": speaker.MODEL_SHA256}
write(good)
made, printed = quietly(speaker.gate, model_factory=Model)
check(isinstance(made, speaker.Gate) and made.threshold == 0.6 and np.allclose(made.voiceprint, unit(1.0, 1.0))
      and "voice check on" in printed, "a voiceprint: only that voice answered, at its own threshold")
write(dict(good, enabled=False))
check(quietly(speaker.gate, model_factory=Model)[0] is None, "switched off: everyone answered")

for name, bad in (("not JSON", "{"), ("a short voiceprint", dict(good, voiceprint=[1.0, 2.0])),
                  ("a threshold out of range", dict(good, threshold=1.5)), ("a threshold of true", dict(good, threshold=True)),
                  ("another model's voiceprint", dict(good, model_sha256="0" * 64))):
    if isinstance(bad, str):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(bad)
    else:
        write(bad)

    made, printed = quietly(speaker.gate, model_factory=Model)
    check(isinstance(made, speaker.Closed) and "--off" in printed, f"{name}: nobody answered until it is fixed")

write(good)


def no_model():
    raise RuntimeError("voice-id-model.onnx is not in the JARVIS folder")


made, printed = quietly(speaker.gate, model_factory=no_model)
check(isinstance(made, speaker.Closed) and "not in the JARVIS folder" in printed, "a model that will not load: nobody answered")

# ---- the model file ----------------------------------------------------------------------------------

with open(speaker.model_path(), "wb") as handle:
    handle.write(b"not the model")

try:
    speaker.Model()
    refused = False
except RuntimeError as error:
    refused = "SHA-256" in str(error)

check(refused, "a model file that is not exactly the one expected is never loaded")


class Download(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


real_urlopen = speaker.urllib.request.urlopen
speaker.urllib.request.urlopen = lambda url, timeout: Download(b"something else entirely")

try:
    quietly(speaker.download_model)
    refused = False
except RuntimeError as error:
    refused = "SHA-256" in str(error)
finally:
    speaker.urllib.request.urlopen = real_urlopen

leftovers = [name for name in os.listdir(folder) if name.endswith(".part")]
check(refused and not leftovers, "a download that is not the expected file is thrown away, nothing half-written left")
check(speaker.MODEL_URL.startswith("https://") and len(speaker.MODEL_SHA256) == 64, "fetched over HTTPS, pinned by SHA-256")

# ---- enrolling ----------------------------------------------------------------------------------------

os.remove(path)
clips = [np.zeros(SECOND + step) for step in range(5)]
voices = {SECOND + step: unit(1.0, 0.1 * step) for step in range(5)}
code, printed = quietly(enroll_voice.enrol, Model(voices), clips + [np.zeros(10)], 0.5)
saved = speaker.load_voiceprint()
check(code == 0 and saved and saved["clips"] == 5 and saved["enabled"] and saved["threshold"] == 0.5
      and np.allclose(saved["voiceprint"], speaker.centroid(list(voices.values())), atol=1e-5)
      and "too little speech" in printed and "scored" in printed,
      "enrolling saves the averaged voiceprint from the usable clips, and says how they scored")
code, printed = quietly(enroll_voice.enrol, Model({SECOND: unit(1.0, 0.0), SECOND + 1: unit(0.0, 1.0),
                                                   SECOND + 2: unit(-1.0, 0.2)}), clips[:3], 0.5)
check(code == 0 and "may miss you" in printed, "clips unlike each other: told he may miss you")
code, printed = quietly(enroll_voice.enrol, Model(), [np.zeros(10)] * 4, 0.5)
check(code == 1 and "Nothing saved" in printed, "too few usable clips: nothing saved")

# --add: more clips, recorded where he missed you, added to the ones you have.
kept = speaker.load_voiceprint()
recorded = iter([np.zeros(SECOND + 10 + step) for step in range(4)])
more = {SECOND + 10 + step: unit(0.5, 1.0, 0.1 * step) for step in range(4)}
real = (speaker.download_model, speaker.Model, enroll_voice.record)
speaker.download_model, speaker.Model = (lambda report=print: None), (lambda: Model(more))
enroll_voice.record = lambda seconds, prompt: next(recorded)

try:
    code, printed = quietly(enroll_voice.main, ["--add", "--clips", "4"])
finally:
    speaker.download_model, speaker.Model, enroll_voice.record = real

added = speaker.load_voiceprint()
everything = list(kept["clip_vectors"]) + list(more.values())
check(code == 0 and added["clips"] == 7 and len(added["clip_vectors"]) == 7 and added["threshold"] == kept["threshold"]
      and np.allclose(added["voiceprint"], speaker.centroid(everything), atol=1e-5) and "3 of them from before" in printed,
      "--add: the new clips join the ones you have, at the same threshold")

legacy = json.load(open(path, encoding="utf-8"))
legacy.pop("clip_vectors")
write(dict(legacy, clips=2))
old = speaker.load_voiceprint()
recorded = iter([np.zeros(SECOND + 10 + step) for step in range(3)])
speaker.download_model, speaker.Model = (lambda report=print: None), (lambda: Model(more))
enroll_voice.record = lambda seconds, prompt: next(recorded)

try:
    code, printed = quietly(enroll_voice.main, ["--add", "--clips", "3"])
finally:
    speaker.download_model, speaker.Model, enroll_voice.record = real

check(code == 0 and speaker.load_voiceprint()["clips"] == 5
      and np.allclose(speaker.load_voiceprint()["voiceprint"],
                      speaker.centroid([old["voiceprint"]] * 2 + list(more.values())[:3]), atol=1e-5),
      "--add to a voiceprint saved before clips were kept: it counts as its clips")

code, printed = quietly(enroll_voice.report, [
    "2026-10-07 11:01:02  voice            yours: sounds 0.61 like you, 0.50 needed, 2.1s  -> heard",
    "2026-10-07 11:02:03  command          'what time is it' -> get_time (local)",
    "2026-10-07 11:03:04  voice            near miss: sounds 0.44 like you, 0.50 needed, 1.2s  -> ignored",
    "2026-10-07 11:04:05  voice            yours: sounds 0.72 like you, 0.50 needed, 3.0s  -> heard",
])
check(code == 0 and "2 times, scoring 0.61 to 0.72" in printed and "2026-10-07 11:03  0.44" in printed
      and f"{speaker.SAFE_THRESHOLD:.2f}" in printed, "--report: how you scored, and the near misses, from the log")
check("Nothing in jarvis-log.txt yet" in quietly(enroll_voice.report, [])[1], "--report with nothing logged says so")
write(dict(legacy, clips=3))

check(quietly(enroll_voice.main, ["--off"])[0] == 0 and speaker.load_voiceprint()["enabled"] is False
      and speaker.load_voiceprint()["clips"] == 3, "--off switches the check off, keeping the voiceprint")
check(quietly(enroll_voice.main, ["--on"])[0] == 0 and speaker.load_voiceprint()["enabled"] is True, "--on switches it back")
check(quietly(enroll_voice.main, ["--threshold", "0.62"])[0] == 0 and speaker.load_voiceprint()["threshold"] == 0.62,
      "--threshold changes only the threshold")
check(quietly(enroll_voice.main, ["--threshold", "2"])[0] == 1 and speaker.load_voiceprint()["threshold"] == 0.62,
      "a threshold out of range is refused")
os.remove(path)
code, printed = quietly(enroll_voice.main, ["--off"])
check(code == 1 and "No voiceprint yet" in printed, "--off with no voiceprint: told to enrol first")

# ---- the engines and the listener -------------------------------------------------------------------

sys.modules["sounddevice"] = MagicMock(name="sounddevice")
sys.modules["vosk"] = types.SimpleNamespace(KaldiRecognizer=MagicMock(), Model=MagicMock())
sys.modules["speech"] = types.SimpleNamespace(is_speaking=lambda: False, speech_epoch=lambda: 0)

import transcriber  # noqa: E402


class Whisper(transcriber.SegmentingEngine):
    name = "test"

    def __init__(self):
        super().__init__(0.25)
        self.sent = []

    def _transcribe(self, audio):
        self.sent.append(len(audio))
        return "Jarvis, remember this."


def utterance(engine):
    loud = (voice(0.25) * 32767).astype(np.int16).tobytes()
    quiet = np.zeros(4000, dtype=np.int16).tobytes()
    result = None

    for block in [quiet] * 4 + [loud] * 6 + [quiet] * 6:
        result = engine.feed(block) or result

    return result


whisper = Whisper()
check(whisper.gate is None and utterance(whisper).text == "jarvis, remember this", "with no check, what is heard is transcribed")
heard = []
whisper.gate = lambda audio: heard.append(len(audio)) or False
result = utterance(whisper)
check(result is not None and result.text == "" and len(whisper.sent) == 1 and heard and heard[0] >= SECOND,
      "a voice that is not yours is dropped before it is sent to be transcribed")
whisper.gate = lambda audio: True
check(utterance(whisper).text == "jarvis, remember this" and len(whisper.sent) == 2, "yours is transcribed")


class Recogniser:
    def __init__(self, *args):
        self.blocks = 0

    def SetWords(self, on):
        pass

    def Reset(self):
        self.blocks = 0

    def AcceptWaveform(self, block):
        self.blocks += 1
        return self.blocks == 3

    def PartialResult(self):
        return '{"partial": ""}'

    def Result(self):
        return '{"text": "jarvis open chrome"}'


sys.modules["vosk"].KaldiRecognizer = Recogniser
vosk = transcriber.VoskEngine(None, 0.25)
heard = []
vosk.gate = lambda audio: heard.append(len(audio)) or False
results = [vosk.feed(np.zeros(4000, dtype=np.int16).tobytes()) for _ in range(3)]
check(results[-1] is not None and results[-1].text == "" and heard == [12000] and not vosk._heard,
      "Vosk: the check hears the whole utterance, and drops it when it is not you")

engine_said = []


class Engine:
    def __init__(self):
        self.active, self.name, self.gate = False, "test", None

    def reset(self):
        pass

    def feed(self, data):
        return types.SimpleNamespace(text=engine_said.pop(0), confidence=None) if engine_said else None


transcriber.build_engine = lambda model, block: Engine()

import voice  # noqa: E402

check(voice.engine.gate is None, "the listener's engine is given the check (none here: no voiceprint)")
voice.REQUIRE_WAKE_WORD, voice.ACKNOWLEDGE_WAKE, voice._wake_listener = True, False, None


class Waker:
    """The wake grammar hears the name in the first utterance only."""

    calls = 0

    def AcceptWaveform(self, data):
        Waker.calls += 1
        return Waker.calls == 1

    def Result(self):
        return '{"text": "jarvis"}'

    def Reset(self):
        pass


voice._make_wake_recognizer = Waker
# Someone else's "Jarvis" (dropped: nothing heard), then you, talking to the room, then you, to him.
engine_said[:] = ["", "i was an unprofitable trader", "jarvis what time is it"]
real_drain = voice._drain_queue


def refill():
    real_drain()

    for _ in engine_said:
        voice.audio_queue.put(b"\x00\x00" * 8)


voice._drain_queue = refill

try:
    command, printed = quietly(voice.listen)
finally:
    voice._drain_queue = real_drain

check(command == "what time is it" and '[ignored] "i was an unprofitable trader"' in printed,
      "a wake-word hit on a dropped voice does not wake him for the next thing said")

# ---- never tidied away -------------------------------------------------------------------------------

check(folder_organizer.is_protected("voiceprint.json") and folder_organizer.is_protected("voice-id-model.onnx"),
      "the voiceprint and the model are never tidied away")

sys.exit(1 if failures else 0)
