"""JARVIS's voice: Azure, Kokoro or Edge, and what stands in when one fails.

    python tools/test_voices.py

No network, no audio device and no download: Azure's answers, GitHub and
Kokoro's model are stood in for, and the speaker is a fake that only
counts samples. Set JARVIS_TEST_KOKORO_FOLDER to a folder holding Kokoro's
real files (kokoro-v1.0.fp16.onnx, voices-v1.0.bin, kokoro-vocab.json) to
also make speech with it.

Covers what went wrong on 12 October 2026: Edge's free service started
refusing every request (403), the stand-in was Windows' woman's voice, and
JARVIS stayed on SPEAKING after it; then Piper replaced it and sounded
robotic, and Kokoro replaced Piper.
"""

import hashlib
import io
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# No audio device: playback is a stream that takes samples at once.
fake_sd = types.ModuleType("sounddevice")


class _Stream:
    def __init__(self, *args, callback=None, **kwargs):
        self._callback = callback
        self.samplerate = kwargs.get("samplerate", 24000)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


fake_sd.OutputStream = _Stream
fake_sd.CallbackStop = type("CallbackStop", (Exception,), {})
fake_sd.sleep = lambda ms: time.sleep(ms / 1000.0)
sys.modules.setdefault("sounddevice", fake_sd)

for key in ("JARVIS_VOICE_ENGINE", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "JARVIS_KOKORO_VOICE",
            "JARVIS_KOKORO_SPEED", "JARVIS_KOKORO_MODEL"):
    os.environ.pop(key, None)

import voices  # noqa: E402


RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def _mp3(seconds=1.0, rate=24000):
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(int(seconds * rate), dtype=np.float32), rate, format="MP3")
    return buffer.getvalue()


def _env(**values):
    clean = {key: "" for key in ("JARVIS_VOICE_ENGINE", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")}
    clean.update(values)
    return patch.dict(os.environ, clean)


# ---- which engine ----------------------------------------------------------------------------

with _env():
    check(voices.engine() == "kokoro", "nothing set: Kokoro, which needs no key")

with _env(AZURE_SPEECH_KEY="k", AZURE_SPEECH_REGION="uksouth"):
    check(voices.engine() == "azure", "an Azure key and region: Azure")

with _env(AZURE_SPEECH_KEY="k"):
    check(voices.engine() == "kokoro", "a key with no region is not Azure set up")

with _env(AZURE_SPEECH_KEY="k", AZURE_SPEECH_REGION="uk south/evil"):
    check(not voices.azure_configured(), "a region that is not a region name never goes into a URL")

with _env(JARVIS_VOICE_ENGINE="edge", AZURE_SPEECH_KEY="k", AZURE_SPEECH_REGION="uksouth"):
    check(voices.engine() == "edge", "JARVIS_VOICE_ENGINE picks the engine over the default")

with _env(JARVIS_VOICE_ENGINE="google"):
    check(voices.engine() == "kokoro", "Google is gone: asking for it gets the default")

check(voices.DEFAULT_KOKORO_VOICE == "bm_fable", "Kokoro speaks as Fable, chosen by ear")

with _env(JARVIS_VOICE_ENGINE="piper"):
    check(voices.engine() == "kokoro", "Piper is gone: asking for it gets the default")

check(voices.local() is voices.kokoro, "Kokoro is the voice made on this PC")
check(voices.sentences("One. Two?\nThree!  Four") == ["One.", "Two?", "Three!", "Four"],
      "a reply splits into sentences at stops and new lines")


# ---- Azure -----------------------------------------------------------------------------------

ssml = voices.azure_ssml("Fish & chips <now>, 'sir'", "en-GB-RyanNeural", "-7%", "-4Hz")
check("Fish &amp; chips &lt;now&gt;" in ssml, "the text is escaped, so it cannot break the SSML")
check("xml:lang=\"en-GB\"" in ssml and "name=\"en-GB-RyanNeural\"" in ssml, "the language comes from the voice")
check("rate=\"-7%\"" in ssml and "pitch=\"-4Hz\"" in ssml, "the same rate and pitch as Edge")
check("<prosody" not in voices.azure_ssml("Hi.", "en-GB-RyanNeural"), "no rate or pitch: no prosody")

sent = {}


def _answer(status, content=b""):
    def post(url, data=None, headers=None, timeout=None):
        sent.update(url=url, data=data, headers=headers, timeout=timeout)
        return types.SimpleNamespace(status_code=status, content=content)

    return post


fake_requests = types.ModuleType("requests")

with _env(AZURE_SPEECH_KEY="secret-key", AZURE_SPEECH_REGION="uksouth"), \
        patch.dict(sys.modules, {"requests": fake_requests}):
    fake_requests.post = _answer(200, _mp3(2.0))
    audio, boundaries = voices.azure_mp3("First one. Second, longer one here.", "en-GB-RyanNeural", "-7%", "-4Hz")

    check(sent["url"] == "https://uksouth.tts.speech.microsoft.com/cognitiveservices/v1", "Azure's own endpoint")
    check(sent["headers"]["Ocp-Apim-Subscription-Key"] == "secret-key", "the key goes in its header")
    check(sent["headers"]["X-Microsoft-OutputFormat"] == voices.AZURE_FORMAT, "an MP3 like Edge's")
    check(sent["timeout"] == voices.AZURE_TIMEOUT, "with a time limit")
    check(audio.startswith(b"\xff") or audio[:3] == b"ID3", "the MP3 comes back")
    check(len(boundaries) == 2 and boundaries[0]["offset"] == 0
          and boundaries[1]["offset"] > 0, "a sentence boundary for each sentence")

    for status, words in ((401, "refused the key"), (429, "free characters"), (500, "500")):
        fake_requests.post = _answer(status)

        try:
            voices.azure_mp3("Hi.", "en-GB-RyanNeural")
            raised = ""
        except RuntimeError as error:
            raised = str(error)

        check(words in raised, f"Azure answering {status} says why: {raised!r}")

    check("secret-key" not in raised, "and never repeats the key")

with _env():
    try:
        voices.azure_mp3("Hi.", "en-GB-RyanNeural")
        check(False, "Azure with no key raises")
    except RuntimeError as error:
        check("AZURE_SPEECH_KEY" in str(error), "Azure with no key names the setting")

timings = voices.estimated_boundaries("One. Two two. Three three three.", 6.0)
check(len(timings) == 3 and abs(sum(t["duration"] for t in timings) - 6.0) < 1e-9
      and timings[1]["offset"] < timings[2]["offset"], "estimated sentences share out the whole time in order")
check(voices.estimated_boundaries("", 3.0) == [], "no text, no sentences")


# ---- Kokoro ----------------------------------------------------------------------------------

class _Session:
    """Kokoro's model as far as voices.py uses it: half a second of sound per call, inputs kept."""

    def __init__(self):
        self.calls = []

    def run(self, outputs, inputs):
        self.calls.append(inputs)
        return [np.full((1, voices.KOKORO_RATE // 2), 0.1, dtype=np.float32)]


def _folder_with_files():
    """A folder holding stand-in voices and alphabet files, as fetch() would leave them."""
    folder = tempfile.mkdtemp(prefix="jarvis-kokoro-test-")
    styles = {name: np.random.default_rng(1).random((510, 1, 256), dtype=np.float32)
              for name in ("bm_george", "bf_emma", "am_adam", "zf_xiaobei")}
    np.savez(os.path.join(folder, "voices-v1.0.bin"), **styles)
    os.replace(os.path.join(folder, "voices-v1.0.bin.npz"), os.path.join(folder, "voices-v1.0.bin"))

    with open(os.path.join(folder, "kokoro-vocab.json"), "w", encoding="utf-8") as handle:
        handle.write('{"vocab": {" ": 16, "h": 50, "e": 47, "l": 54, "o": 57, ".": 4}}')

    return folder


fetched = []


def _get(url, expected, path, size="", note=None):
    fetched.append((url, expected, os.path.basename(path)))
    return path


folder = _folder_with_files()
session = _Session()
voice = voices.Kokoro(voice="bm_george")
check(not voice.ready(), "Kokoro is not ready before it is loaded")

try:
    voice.audio("Hello.")
    check(False, "speaking before loading raises")
except RuntimeError as error:
    check("not loaded" in str(error), "speaking before loading raises, saying so")

loaded = voice.load(folder=folder, get=_get, session=session, phonemes=lambda text: "helo")
check(loaded and voice.ready(), "Kokoro loads")
check([name for _url, _sha, name in fetched] == ["kokoro-v1.0.fp16.onnx", "voices-v1.0.bin", "kokoro-vocab.json"],
      "its model, voices and alphabet are fetched")
check(all(url.startswith("https://") and len(sha) == 64 for url, sha, _name in fetched),
      "each over HTTPS with a fixed SHA-256")
check("3596b26764286a7de9d90c363e988d50578918e5" in voices.KOKORO_VOCAB[0], "the alphabet from a fixed commit")

samples, rate, boundaries = voice.audio("Hello. Hello.")
gap = int(rate * voices.SENTENCE_GAP_SECONDS)
check(rate == voices.KOKORO_RATE and len(samples) == 2 * (rate // 2) + gap,
      "each sentence made on its own, with a breath between")
check([round(item["offset"], 3) for item in boundaries] == [0.0, round(0.5 + voices.SENTENCE_GAP_SECONDS, 3)],
      "so each sentence's start is exact")
first = session.calls[0]
check(first["tokens"].tolist() == [[0, 50, 47, 54, 57, 0]], "phonemes become the model's numbers, wrapped in 0s")
check(first["style"].shape == (1, 256) and first["speed"].tolist() == [1.0], "with the voice's style and speed")

data, mp3_boundaries = voice.mp3("Hello. Hello.")
check(abs(sf.info(io.BytesIO(data)).duration - len(samples) / rate) < 0.2, "Kokoro's speech is cached as an MP3")
check(mp3_boundaries == boundaries, "with the same sentence timings")

long_voice = voices.Kokoro(voice="bm_george")
long_session = _Session()
long_voice.load(folder=folder, get=_get, session=long_session,
                phonemes=lambda text: " ".join(["helo"] * 200))
long_voice.audio("A very long sentence.")
check(len(long_session.calls) == 2 and all(len(call["tokens"][0]) - 2 <= voices.KOKORO_MAX_PHONEMES
                                           for call in long_session.calls),
      "a sentence too long for the model is made in pieces, broken between words")

for bad, why in (("zf_xiaobei", "not an English"), ("../../x", "not an English"), ("bm_nobody", "no Kokoro voice")):
    wrong = voices.Kokoro(voice=bad)
    check(not wrong.load(folder=folder, get=_get, session=_Session(), phonemes=lambda text: "helo")
          and why in (wrong.error() or ""), f"voice {bad!r} is refused: {wrong.error()}")

check(voices.Kokoro(voice="bm_george", speed=1.1).voice_id() != voice.voice_id(), "a different pace is cached apart")
check(voices.Kokoro(model="int8").voice_id() != voices.Kokoro(model="fp16").voice_id(),
      "and so is the other model")
with patch.dict(os.environ, {"JARVIS_KOKORO_SPEED": "9", "JARVIS_KOKORO_MODEL": "huge"}):
    odd = voices.Kokoro()
check(odd.speed == 1.0 and odd.model == "fp16", "a speed or model out of range falls back to the default")

# fetch(): kept only when its SHA-256 matches, never left half-written.
payload = b"kokoro" * 1000


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


target = os.path.join(tempfile.mkdtemp(prefix="jarvis-fetch-"), "sub", "model.onnx")
notes = []

with patch.object(voices.urllib.request, "urlopen", lambda request, timeout=None: _Response(payload)):
    got = voices.fetch("https://example.invalid/model.onnx", hashlib.sha256(payload).hexdigest(), target, "6 KB",
                       notes.append)
    check(got == target and open(target, "rb").read() == payload, "a download that matches its SHA-256 is kept")
    check(notes == [True, False], "and the voice knows while it is downloading")

    bad_target = target + ".bad"

    try:
        voices.fetch("https://example.invalid/model.onnx", "0" * 64, bad_target)
        check(False, "a download that does not match raises")
    except RuntimeError as error:
        check("SHA-256" in str(error), "a download that does not match its SHA-256 is refused")

    check(not os.path.exists(bad_target) and not os.path.exists(bad_target + ".partial"),
          "and nothing of it is left behind")

calls = []

with patch.object(voices.urllib.request, "urlopen", lambda *a, **k: calls.append(1)):
    voices.fetch("https://example.invalid/model.onnx", "0" * 64, target)

check(not calls, "a file already there is not fetched again")


# ---- speech.py: which voice speaks -----------------------------------------------------------

import speech  # noqa: E402

engine = speech.SpeechEngine(engine="azure")
edge = speech.SpeechEngine(engine="edge")

with patch.object(voices, "local", lambda: voice):
    kokoro_engine = speech.SpeechEngine(engine="kokoro")

check(engine._cache_key("Done, sir.") == edge._cache_key("Done, sir."),
      "Azure and Edge share the cache: Ryan already said it")
check(kokoro_engine._cache_key("Done, sir.") != engine._cache_key("Done, sir."), "Kokoro's phrases are cached apart")
check(kokoro_engine._deadline() > engine._deadline(), "a voice made on this PC is given longer than a network")

directory = tempfile.mkdtemp(prefix="jarvis-voice-test-")
path = os.path.join(directory, "phrase.mp3")

with patch.object(voices, "local", lambda: voice):
    kokoro_engine._synthesize_to_cache("Hello. Hello.", path, os.path.join(directory, "phrase.txt"))

check(os.path.exists(path) and len(kokoro_engine._load_boundaries(os.path.join(directory, "phrase.json")) or []) == 2,
      "a Kokoro phrase is cached with its sentences")

played = []
windows = []


def _record(data, samplerate, boundaries, offset=0):
    played.append((len(data), samplerate))


with patch.object(voices, "local", lambda: voice), \
        patch.object(engine, "_play_reactive", side_effect=_record), \
        patch.object(engine, "_speak_neural", side_effect=RuntimeError("Azure answered 403")), \
        patch.object(engine, "_speak_windows", side_effect=windows.append):
    speech._neural_failed_at = 0.0
    engine.speak("Here's your room cam, sir.")

check(played and not windows, "Azure failing: Kokoro says it, not Windows' woman's voice")
check(not speech.is_speaking(), "and speaking ends")

played.clear()
unloaded = voices.Kokoro(voice="bm_george")

with patch.object(voices, "local", lambda: unloaded), \
        patch.object(engine, "_play_reactive", side_effect=_record), \
        patch.object(engine, "_speak_neural", side_effect=RuntimeError("Azure answered 403")), \
        patch.object(engine, "_speak_windows", side_effect=windows.append):
    speech._neural_failed_at = 0.0
    engine.speak("Still loading.")

check(windows == ["Still loading."] and not played, "Kokoro not loaded yet: Windows' voice, the last resort")


# Windows' voice that never finishes: the reply ends, speaking ends, and it is not used again.
hung = threading.Event()


class _HungVoice:
    """A SAPI voice that never says it has finished."""

    def Speak(self, text, flags=0):
        hung.set()

    def WaitUntilDone(self, milliseconds):
        time.sleep(milliseconds / 1000.0)
        return False


speech._windows_voice_hung.clear()

with patch.object(voices, "local", lambda: unloaded), \
        patch.object(speech, "_sapi_voice", _HungVoice), \
        patch.object(speech, "_WINDOWS_VOICE_GRACE", 0.5), \
        patch.object(speech, "_WINDOWS_VOICE_PER_CHAR", 0.0), \
        patch.object(engine, "_speak_neural", side_effect=RuntimeError("Azure answered 403")):
    speech._neural_failed_at = 0.0
    started = time.monotonic()
    engine.speak("This never finishes.")
    took = time.monotonic() - started

    check(hung.is_set() and took < 3, f"a Windows voice that hangs is left behind ({took:.1f}s), not waited on forever")
    check(not speech.is_speaking(), "and JARVIS is not left on SPEAKING")
    check(speech._windows_voice_hung.is_set(), "and it is not used again this run")

    started = time.monotonic()
    engine.speak("Next reply.")
    check(time.monotonic() - started < 0.5, "the next reply does not wait on it")

speech._windows_voice_hung.clear()
speech._neural_failed_at = 0.0

# Each sentence in the Windows voice gets its own voice object, made on the thread that speaks it: the
# one engine pyttsx3 shared between threads is the likeliest reason the second sentence never finished.
made = []


class _QuickVoice:
    def __init__(self):
        made.append((self, threading.get_ident()))

    def Speak(self, text, flags=0):
        pass

    def WaitUntilDone(self, milliseconds):
        return True


with patch.object(voices, "local", lambda: unloaded), \
        patch.object(speech, "_sapi_voice", _QuickVoice), \
        patch.object(engine, "_speak_neural", side_effect=RuntimeError("Azure answered 403")):
    for sentence in ("Room sensor online, sir.", "My room cam sensor online, sir."):
        speech._neural_failed_at = 0.0
        engine.speak(sentence)

check(len(made) == 2 and made[0][0] is not made[1][0], "two sentences, two voice objects: none is reused")
check(threading.get_ident() not in {ident for _voice, ident in made}, "each made on the thread that speaks it")
check(not speech._windows_voice_hung.is_set(), "and both finish")


# The SAPI voice chosen: a man in JARVIS's language, or the one named in .env.

class _Token:
    def __init__(self, name):
        self.name = name

    def GetDescription(self):
        return self.name


class _Tokens:
    def __init__(self, tokens):
        self.tokens = tokens
        self.Count = len(tokens)

    def Item(self, index):
        return self.tokens[index]


class _Catalogue:
    def __init__(self, by_query):
        self.by_query = by_query
        self.asked = []

    def GetVoices(self, query=""):
        self.asked.append(query)
        return _Tokens(self.by_query.get(query, []))


george, david, hazel = _Token("Microsoft George - English (United Kingdom)"), _Token("Microsoft David"), \
    _Token("Microsoft Hazel")
check(speech._language_code("en-GB-RyanNeural") == "809", "Ryan's language is SAPI's 809, from Windows' own table")
check(speech._language_code("xx-YY-Nobody") is None, "an unknown language has no code")
catalogue = _Catalogue({"Gender=Male;Language=809": [george], "Gender=Male": [david, george],
                        "": [hazel, david, george]})
check(speech._choose_sapi_voice(catalogue, None, "809") is george, "a British man when Windows has one")
check(speech._choose_sapi_voice(_Catalogue({"Gender=Male": [david]}), None, "809") is david,
      "any man when it has no British one")
check(speech._choose_sapi_voice(_Catalogue({}), None, "809") is None, "and Windows' default when it has no man")
check(speech._choose_sapi_voice(catalogue, "hazel", "809") is hazel, "JARVIS_WINDOWS_VOICE picks one by name")
check(speech._choose_sapi_voice(catalogue, "nobody", "809") is None, "a name that is not installed: the default")




# Kokoro reading itself off the disk is waited for; a download is not.

class _SlowSession(_Session):
    pass


def _slow_open_session():
    time.sleep(0.6)
    return _SlowSession()


loading = voices.Kokoro(voice="bm_george")
loading.load = (lambda original: (lambda: original(folder=folder, get=_get, session=_slow_open_session(),
                                                    phonemes=lambda text: "helo")))(loading.load)
played.clear()
windows.clear()

with patch.object(voices, "local", lambda: loading), \
        patch.object(kokoro_engine, "_play_reactive", side_effect=_record), \
        patch.object(kokoro_engine, "_speak_windows", side_effect=windows.append), \
        patch.object(speech, "_CACHE_DIR", tempfile.mkdtemp(prefix="jarvis-voice-cache-")):
    loading.load_in_background()
    speech._neural_failed_at = 0.0
    kokoro_engine.speak("Good afternoon, sir. JARVIS is online.")

check(loading.ready() and played and not windows, "the greeting waits for Kokoro loading from the disk")


def _slow_get(url, expected, path, size="", note=None):
    note(True)
    time.sleep(3)
    note(False)
    return path


downloading = voices.Kokoro(voice="bm_george")
downloading.load = (lambda original: (lambda: original(folder=folder, get=_slow_get, session=_Session(),
                                                        phonemes=lambda text: "helo")))(downloading.load)
downloading.load_in_background()
time.sleep(0.2)
started = time.monotonic()
ready = downloading.wait(10)
check(not ready and time.monotonic() - started < 0.5, "a first download is not waited for")


# ---- Kokoro for real, when its files are on this machine ---------------------------------------

real_folder = os.getenv("JARVIS_TEST_KOKORO_FOLDER")

if real_folder and os.path.isdir(real_folder):
    real = voices.Kokoro(voice="bm_george")
    started = time.monotonic()
    real.load(folder=real_folder)
    check(real.ready(), f"the real Kokoro loads ({time.monotonic() - started:.1f}s): {real.error()}")
    started = time.monotonic()
    data, timings = real.mp3("Here's your room cam, sir. The street is quiet.")
    seconds = sf.info(io.BytesIO(data)).duration
    check(seconds > 1.5 and len(timings) == 2,
          f"and speaks two sentences ({seconds:.1f}s of speech in {time.monotonic() - started:.1f}s)")
else:
    print("SKIP Kokoro for real (set JARVIS_TEST_KOKORO_FOLDER to a folder with its files)")


failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
