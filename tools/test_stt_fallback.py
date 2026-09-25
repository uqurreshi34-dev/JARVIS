"""Cloud transcription falls back to local Whisper on the spot.

With STT_ENGINE=groq, a request that fails -- the network drops, the daily
allowance runs out, Groq has a bad minute -- used to lose the utterance.
Now the same utterance is transcribed by local Whisper, loaded in the
background at start-up, and Groq is rested before being tried again.
Checked with Groq and local Whisper both faked: no network, no model.

- a working cloud answers, and local Whisper is loaded in the background;
- the cloud client gives up quickly and never retries on its own;
- a failed request is answered locally, for the same utterance;
- while rested, Groq is not asked at all; afterwards it is tried again,
  and a success clears the rest;
- rests double while Groq keeps failing, up to a ceiling, and follow the
  wait a rate-limited response asks for;
- without a local model, a cloud failure loses only that utterance;
- STT_ENGINE=groq without a key starts local Whisper, not Vosk;
- STT_ENGINE=whisper never uses the cloud.

    python tools/test_stt_fallback.py
"""

import os
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import transcriber  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- fakes -----------------------------------------------------------------

class Cloud:
    """Stands in for the OpenAI client pointed at Groq."""

    made = []

    def __init__(self, **options):
        Cloud.made.append(options)
        self.calls = 0
        self.failure = None
        self.audio = types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.calls += 1

        if self.failure is not None:
            raise self.failure

        return types.SimpleNamespace(text="Open Chrome.")


class RateLimited(Exception):
    def __init__(self, seconds):
        super().__init__("rate limited")
        self.response = types.SimpleNamespace(headers={"retry-after": str(seconds)})


class Local:
    name = "fake-local"
    loaded = 0

    def __init__(self, block_seconds):
        Local.loaded += 1

    def _transcribe(self, audio):
        return "open chrome (local)"


class Unavailable:
    name = "fake-missing"

    def __init__(self, block_seconds):
        raise RuntimeError("not installed")


sys.modules["openai"] = types.SimpleNamespace(OpenAI=Cloud)
os.environ["GROQ_API_KEY"] = "gsk-test"

clock = {"now": 1000.0}
transcriber.time.monotonic = lambda: clock["now"]

audio = np.zeros(transcriber.SAMPLE_RATE, dtype=np.float32)


def engine(factories=(Local,)):
    made = transcriber.GroqWhisperEngine(0.03, local_factories=factories)
    made._local_ready.wait(timeout=5)
    return made


# ---- a working cloud ----------------------------------------------------------

Cloud.made.clear()
groq = engine()
check(groq._transcribe(audio) == "Open Chrome.", "a working cloud answers")
check(Local.loaded == 1 and groq._local is not None, "local Whisper is loaded in the background, ready to stand in")
options = Cloud.made[-1]
check(options.get("timeout") == groq.CLOUD_TIMEOUT and options.get("max_retries") == 0,
      f"the cloud client gives up after {groq.CLOUD_TIMEOUT}s and never retries on its own")

# ---- a failure mid-session ----------------------------------------------------

client = groq._client
client.failure = TimeoutError("timed out")
check(groq._transcribe(audio) == "open chrome (local)", "a failed request is answered locally, for the same utterance")
check(groq._rest_until == clock["now"] + groq.REST_SECONDS, f"Groq is rested for {groq.REST_SECONDS:.0f}s")

before = client.calls
check(groq._transcribe(audio) == "open chrome (local)" and client.calls == before,
      "while rested, Groq is not asked at all")

clock["now"] += groq.REST_SECONDS + 1
check(groq._transcribe(audio) == "open chrome (local)" and client.calls == before + 1,
      "after the rest, Groq is tried again")
check(groq._rest == groq.REST_SECONDS * 2, "a second failure in a row doubles the rest")

for _ in range(10):
    clock["now"] = groq._rest_until + 1
    groq._transcribe(audio)

check(groq._rest == groq.MAX_REST_SECONDS, f"rests stop growing at {groq.MAX_REST_SECONDS:.0f}s")

client.failure = None
clock["now"] = groq._rest_until + 1
check(groq._transcribe(audio) == "Open Chrome." and groq._rest == 0.0, "a success clears the rest")

client.failure = RateLimited(90)
start = clock["now"]
groq._transcribe(audio)
check(groq._rest_until == start + 90, "a rate-limited response's own wait is honoured")

# ---- nothing to fall back on ---------------------------------------------------------

bare = engine(factories=(Unavailable,))
bare._client.failure = ConnectionError("offline")

try:
    bare._transcribe(audio)
except RuntimeError as error:
    check("no local Whisper" in str(error), "without a local model, the failure is reported, not hidden")
else:
    check(False, "without a local model, the failure is reported, not hidden")

# The segmenting engine turns that into a lost utterance, not a crash.
bare._buffer = [np.full(transcriber.SAMPLE_RATE, 0.1, dtype=np.float32)]
check(bare._finish() is None, "and only that utterance is lost")

# ---- which engine starts ---------------------------------------------------------------

real = (transcriber.GroqWhisperEngine, transcriber.LocalWhisperEngine,
        transcriber.OpenAIWhisperEngine, transcriber.VoskEngine)


class NoKey:
    name = "groq-whisper"

    def __init__(self, block_seconds):
        raise RuntimeError("GROQ_API_KEY is needed")


class Vosk:
    name = "vosk"

    def __init__(self, model, block_seconds):
        pass


class NoCloud:
    name = "groq-whisper"

    def __init__(self, block_seconds):
        raise AssertionError("the cloud must not be used")


try:
    transcriber.GroqWhisperEngine = NoKey
    transcriber.LocalWhisperEngine = Local
    transcriber.VoskEngine = Vosk

    transcriber.ENGINE = "groq"
    check(isinstance(transcriber.build_engine(None, 480), Local),
          "STT_ENGINE=groq without a key starts local Whisper, not Vosk")

    transcriber.GroqWhisperEngine = NoCloud
    transcriber.ENGINE = "whisper"
    check(isinstance(transcriber.build_engine(None, 480), Local), "STT_ENGINE=whisper never uses the cloud")
finally:
    (transcriber.GroqWhisperEngine, transcriber.LocalWhisperEngine,
     transcriber.OpenAIWhisperEngine, transcriber.VoskEngine) = real

sys.exit(1 if failures else 0)
