"""Where JARVIS's voice is made: Google, Azure, Kokoro or Edge, chosen in .env.

    google  Google Cloud Text-to-Speech's Chirp 3 HD voices: natural and
            friendly, with a key of your own (GOOGLE_TTS_API_KEY, from a
            Google Cloud project with the Text-to-Speech API on). The first
            1,000,000 Chirp 3 HD characters a month are free; JARVIS counts
            what it sends and stops at GOOGLE_TTS_MONTHLY_CHARACTERS
            (950,000) so the free amount is never passed -- Kokoro speaks
            for the rest of that month. GOOGLE_TTS_VOICE names the voice
            (default en-GB-Chirp3-HD-Achird).
    azure   Microsoft's official Speech service. The same Ryan voice as
            Edge, with a key of your own: AZURE_SPEECH_KEY and
            AZURE_SPEECH_REGION from an Azure Speech resource. The free
            tier (F0) covers 500,000 characters a month.
    kokoro  Kokoro (82M), run on this PC with onnxruntime: natural British
            voices with no key, no account and no network once its files
            are downloaded -- once, from kokoro-onnx's GitHub releases,
            each checked against a fixed SHA-256. JARVIS_KOKORO_VOICE names
            the voice (default bm_fable, chosen by ear from the four
            British men).
    edge    Edge's Read Aloud service: Ryan with no key, but unofficial.
            Since 10 October 2026 it refuses every client with a 403
            (rany2/edge-tts issue 490); it stays here for when it works
            again.

Said aloud, the voice switches at once and stays switched ("Jarvis, use
the Google voice", "switch to Fable"): the choice is kept in voice.json in
the JARVIS folder and wins over .env. Otherwise JARVIS_VOICE_ENGINE picks
one; unset, it is google when its key is set, azure when its key and
region are, and kokoro otherwise -- so a fresh install speaks in a natural
man's voice with nothing to sign up for.
Whichever is chosen, Kokoro stands in when it fails (speech.py), and
Windows' own voice only when Kokoro cannot.

Piper was tried first (12 October 2026) and sounded robotic; Kokoro
replaced it the same day.

Every engine hands back an MP3 and when each sentence starts in it, so
speech.py caches, plays and shows them alike, and the phone gets the
same file.
"""

import base64
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import numpy as np
import soundfile as sf
from dotenv import load_dotenv

load_dotenv()


ENGINE_ENV = "JARVIS_VOICE_ENGINE"
ENGINES = ("google", "azure", "kokoro", "edge")

# The engines that run on this PC.
LOCAL = ("kokoro",)

# What start-up and the boot check call each one.
LABELS = {"google": "GOOGLE", "azure": "AZURE", "kokoro": "KOKORO", "edge": "EDGE"}


def engine():
    """The engine chosen aloud (voice.json), else the one .env asks for, else google or azure with a key, kokoro."""
    said = chosen()

    if said:
        return said

    chosen_in_env = (os.getenv(ENGINE_ENV) or "").strip().casefold()
    return _from_env(chosen_in_env)


def _from_env(asked):
    if asked in ENGINES:
        return asked

    if asked:
        print(f"[JARVIS] {ENGINE_ENV}={asked!r} is not one of {', '.join(ENGINES)}; "
              f"using the default.", flush=True)

    if google_configured():
        return "google"

    return "azure" if azure_configured() else "kokoro"


# ---- switching voice aloud -------------------------------------------------------------------

CHOICE_FILE = "voice.json"

_SWITCH = ("switch", "change", "use", "swap", "go back", "put", "talk", "speak", "try")


def _choice_path():
    from actions import files

    root = files.root()
    return os.path.join(root, CHOICE_FILE) if root else None


def chosen():
    """The engine chosen aloud and kept in voice.json, or None."""
    try:
        with open(_choice_path(), encoding="utf-8") as handle:
            name = str(json.load(handle).get("engine", "")).casefold()
    except (OSError, TypeError, ValueError, AttributeError):
        return None

    return name if name in ENGINES else None


def choose(name):
    """Keep engine [name] as the voice, so it is used again after a restart."""
    path = _choice_path()

    if not path or name not in ENGINES:
        return

    partial = f"{path}.partial"

    try:
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump({"engine": name}, handle)

        os.replace(partial, path)
    except OSError as error:
        print(f"[JARVIS] could not keep the voice choice ({error})")


def names(name):
    """What engine [name] can be called aloud: itself, and its voice's own name ("google", "achird")."""
    called = {name}

    if name == "google":
        called.add(google_voice().rsplit("-", 1)[-1].casefold())
    elif name in LOCAL:
        called.add(kokoro.voice.split("_", 1)[-1].casefold())
    elif name in ("azure", "edge"):
        called.add("ryan")

    return called


def spoken(name):
    """How JARVIS names engine [name]'s voice when switching to it."""
    if name == "google":
        return f"Google's {google_voice().rsplit('-', 1)[-1]} voice"

    if name in LOCAL:
        return kokoro.voice.split("_", 1)[-1].title()

    return "Ryan"


def ready(name):
    """None when engine [name] can speak, or why not."""
    if name == "google" and not google_configured():
        return f"Google's voice needs {GOOGLE_KEY_ENV} in .env"

    if name == "azure" and not azure_configured():
        return f"Azure's voice needs {AZURE_KEY_ENV} and {AZURE_REGION_ENV} in .env"

    return None


def switch_asked(text):
    """The engine [text] asks to switch the voice to -- "use the Google voice", "switch to Fable" -- or None.

    A switching word and one engine's name, as a whole word; "voice" alone
    is not enough, so "what's the weather" or "Google something" never
    switches him.
    """
    words = re.sub(r"[^a-z0-9\s']", " ", str(text or "").casefold()).split()
    said = " ".join(words)

    if not any(said == verb or said.startswith(verb + " ") or f" {verb} " in f" {said} " for verb in _SWITCH):
        return None

    named = [name for name in ENGINES if names(name) & set(words)]

    if len(named) != 1:
        return None

    if {"voice", "voices"} & set(words):
        return named[0]

    # Without the word "voice", only the name alone after the switch: "switch to Fable", "use Google".
    # "switch to google chrome" is a window, and "use google to find..." a search.
    rest = said

    for lead in ("jarvis ", "please ", "can you ", "could you "):
        rest = rest[len(lead):] if rest.startswith(lead) else rest

    for tail in (" please", " jarvis", " sir", " now"):
        rest = rest[:-len(tail)] if rest.endswith(tail) else rest

    for verb in ("switch back to", "switch to", "change back to", "change to", "go back to", "use"):
        if rest.startswith(verb + " ") and rest[len(verb) + 1:].strip() in names(named[0]):
            return named[0]

    return None


def voice_id(name, voice):
    """What the cache keys engine [name]'s phrases by: Azure and Edge share Ryan's, the others their own."""
    if name in LOCAL:
        return local().voice_id()

    if name == "google":
        return f"google:{google_voice()}:{google_rate() or ''}"

    return voice


def local():
    """The voice made on this PC: it speaks when chosen, and stands in when Azure or Edge fails."""
    return kokoro


def describe(name, voice):
    """One line for start-up: which voice, and where it comes from."""
    if name == "google":
        return f"{google_voice()} via Google Cloud Text-to-Speech; Kokoro {kokoro.voice} stands in"

    if name == "azure":
        return f"{voice} via Azure Speech ({_azure_region()}); Kokoro {kokoro.voice} stands in"

    if name == "kokoro":
        return f"Kokoro {kokoro.voice} (on this PC)"

    return f"{voice} via Edge Read Aloud (unofficial); Kokoro {kokoro.voice} stands in"


# ---- the sentences in a reply ----------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def sentences(text):
    """[text] split into its sentences, at a full stop, question or exclamation mark, or a new line."""
    return [part.strip() for line in str(text or "").splitlines()
            for part in _SENTENCE_END.split(line) if part.strip()]


def estimated_boundaries(text, seconds):
    """When each sentence of [text] starts in [seconds] of speech, for an engine that does not say.

    Shared out by length: a sentence twice as long takes about twice as
    long to say. Only the HUD's sentence highlight uses it.
    """
    parts = sentences(text)
    total = sum(len(part) for part in parts)

    if not parts or total <= 0 or seconds <= 0:
        return []

    boundaries = []
    start = 0.0

    for sentence in parts:
        duration = seconds * len(sentence) / total
        boundaries.append({"offset": start, "duration": duration, "text": sentence})
        start += duration

    return boundaries


def _seconds(mp3):
    return float(sf.info(io.BytesIO(mp3)).duration)


# ---- Google Cloud Text-to-Speech -------------------------------------------------------------

GOOGLE_KEY_ENV = "GOOGLE_TTS_API_KEY"
GOOGLE_VOICE_ENV = "GOOGLE_TTS_VOICE"
GOOGLE_RATE_ENV = "GOOGLE_TTS_RATE"
GOOGLE_LIMIT_ENV = "GOOGLE_TTS_MONTHLY_CHARACTERS"

GOOGLE_URL = "https://texttospeech.googleapis.com/v1"

# A British man, the voice Google calls friendly. Any Chirp 3 HD voice
# works: tools/voice_samples.py says each British man's line so you can
# choose by ear.
DEFAULT_GOOGLE_VOICE = "en-GB-Chirp3-HD-Achird"

# Chirp 3 HD's free amount is 1,000,000 characters a month; this stops
# short of it, so what JARVIS sends is never charged for.
DEFAULT_GOOGLE_LIMIT = 950_000

GOOGLE_TIMEOUT = (5, 20)

# Characters sent to Google this month, kept in the JARVIS folder.
USAGE_FILE = "voice-usage.json"

# language-REGION-family-name, as Google names them: "en-GB-Chirp3-HD-Achird".
_GOOGLE_VOICE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}(-[A-Za-z0-9]+){2,}$")
_usage_lock = threading.Lock()
_limit_said = set()


def _google_key():
    return (os.getenv(GOOGLE_KEY_ENV) or "").strip()


def google_configured():
    return bool(_google_key())


def google_voice():
    chosen = (os.getenv(GOOGLE_VOICE_ENV) or "").strip()
    return chosen if _GOOGLE_VOICE.match(chosen) else DEFAULT_GOOGLE_VOICE


def google_rate():
    return _float(os.getenv(GOOGLE_RATE_ENV), 0.25, 2.0)


def google_limit():
    try:
        return max(0, int(str(os.getenv(GOOGLE_LIMIT_ENV) or DEFAULT_GOOGLE_LIMIT).replace(",", "").replace("_", "")))
    except ValueError:
        return DEFAULT_GOOGLE_LIMIT


def _usage_path():
    from actions import files

    root = files.root()
    return os.path.join(root, USAGE_FILE) if root else None


def _month():
    return time.strftime("%Y-%m")


def google_used():
    """Characters sent to Google this month."""
    path = _usage_path()

    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, TypeError, ValueError):
        return 0

    if not isinstance(saved, dict) or saved.get("month") != _month():
        return 0

    try:
        return max(0, int(saved.get("google", 0)))
    except (TypeError, ValueError):
        return 0


def _count_google(characters):
    path = _usage_path()

    if not path:
        return

    total = google_used() + characters
    partial = f"{path}.partial"

    try:
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump({"month": _month(), "google": total}, handle)

        os.replace(partial, path)
    except OSError as error:
        print(f"[JARVIS] could not record Google's characters ({error})")


def _google_error(response):
    """Google's own words for what went wrong, never the key."""
    try:
        return str(response.json()["error"]["message"])[:200]
    except Exception:
        return ""


def google_mp3(text, voice=None, rate=None):
    """(MP3 bytes, sentence boundaries) for [text] from Google. Raises on any failure, or at the monthly limit."""
    if not google_configured():
        raise RuntimeError(f"Google Text-to-Speech needs {GOOGLE_KEY_ENV} in .env")

    text = str(text)
    voice = voice or google_voice()
    rate = rate if rate is not None else google_rate()

    with _usage_lock:
        if google_used() + len(text) > google_limit():
            month = _month()

            if month not in _limit_said:
                _limit_said.add(month)
                print(f"[JARVIS] Google's characters for this month are used up ({google_used():,} of "
                      f"{google_limit():,}): Kokoro speaks until next month, so nothing is charged.", flush=True)

            raise RuntimeError("this month's Google characters are used up")

    import requests

    config = {"audioEncoding": "MP3", "sampleRateHertz": 24000}

    if rate:
        config["speakingRate"] = rate

    response = requests.post(
        f"{GOOGLE_URL}/text:synthesize",
        json={"input": {"text": text},
              "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
              "audioConfig": config},
        headers={"X-Goog-Api-Key": _google_key(), "User-Agent": "JARVIS"},
        timeout=GOOGLE_TIMEOUT,
    )

    if response.status_code == 403:
        raise RuntimeError("Google refused the key (403): is the Text-to-Speech API enabled for its project, "
                           f"with billing on? {_google_error(response)}".strip())

    if response.status_code == 429:
        raise RuntimeError("Google is rate limiting (429)")

    if response.status_code != 200:
        raise RuntimeError(f"Google answered {response.status_code}: {_google_error(response)}".strip())

    try:
        mp3 = base64.b64decode(response.json()["audioContent"])
    except Exception as error:
        raise RuntimeError(f"Google's answer had no audio ({type(error).__name__})")

    with _usage_lock:
        _count_google(len(text))

    return mp3, estimated_boundaries(text, _seconds(mp3))


def google_voices(language="en-GB", family="Chirp3-HD"):
    """[(name, gender)] of Google's voices for [language] in [family], as Google lists them."""
    import requests

    response = requests.get(f"{GOOGLE_URL}/voices", params={"languageCode": language},
                            headers={"X-Goog-Api-Key": _google_key(), "User-Agent": "JARVIS"},
                            timeout=GOOGLE_TIMEOUT)

    if response.status_code != 200:
        raise RuntimeError(f"Google answered {response.status_code}: {_google_error(response)}".strip())

    return sorted((item["name"], item.get("ssmlGender", "")) for item in response.json().get("voices", [])
                  if f"-{family}-" in item.get("name", ""))


# ---- Azure Speech ----------------------------------------------------------------------------

AZURE_KEY_ENV = "AZURE_SPEECH_KEY"
AZURE_REGION_ENV = "AZURE_SPEECH_REGION"

# The format Edge sends, so a phrase sounds and caches the same either way.
AZURE_FORMAT = "audio-24khz-48kbitrate-mono-mp3"

# Seconds to connect, and to wait for the audio once connected.
AZURE_TIMEOUT = (5, 20)

# A region is a short lowercase name ("uksouth"); anything else is not
# put into a URL.
_REGION = re.compile(r"^[a-z0-9]{2,40}$")


def _azure_key():
    return (os.getenv(AZURE_KEY_ENV) or "").strip()


def _azure_region():
    return (os.getenv(AZURE_REGION_ENV) or "").strip().casefold()


def azure_configured():
    return bool(_azure_key()) and bool(_REGION.match(_azure_region()))


def azure_ssml(text, voice, rate=None, pitch=None):
    """The SSML Azure is sent: [text] escaped, in [voice], at [rate] and [pitch] when given."""
    language = "-".join(str(voice).split("-")[:2]) or "en-GB"
    prosody = "".join(f" {name}={quoteattr(value)}" for name, value in (("rate", rate), ("pitch", pitch)) if value)
    spoken = escape(str(text))

    if prosody:
        spoken = f"<prosody{prosody}>{spoken}</prosody>"

    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang={quoteattr(language)}>"
            f"<voice name={quoteattr(str(voice))}>{spoken}</voice></speak>")


def azure_mp3(text, voice, rate=None, pitch=None):
    """(MP3 bytes, sentence boundaries) for [text] from Azure Speech. Raises on any failure."""
    if not azure_configured():
        raise RuntimeError(f"Azure Speech needs {AZURE_KEY_ENV} and {AZURE_REGION_ENV} in .env")

    import requests

    response = requests.post(
        f"https://{_azure_region()}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=azure_ssml(text, voice, rate, pitch).encode("utf-8"),
        headers={
            "Ocp-Apim-Subscription-Key": _azure_key(),
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": AZURE_FORMAT,
            "User-Agent": "JARVIS",
        },
        timeout=AZURE_TIMEOUT,
    )

    if response.status_code == 401:
        raise RuntimeError(f"Azure Speech refused the key (401): check {AZURE_KEY_ENV} and {AZURE_REGION_ENV}")

    if response.status_code == 429:
        raise RuntimeError("Azure Speech is rate limiting, or this month's free characters are used up (429)")

    if response.status_code != 200 or not response.content:
        raise RuntimeError(f"Azure Speech answered {response.status_code}")

    mp3 = response.content
    return mp3, estimated_boundaries(text, _seconds(mp3))


# ---- voices made on this PC ------------------------------------------------------------------

# Each sentence is made on its own; a breath between them, as a person leaves.
SENTENCE_GAP_SECONDS = 0.2


class LocalVoice:
    """A voice made on this PC: loaded once (load(), or load_in_background()), used from any thread.

    A subclass opens the model (_open) and makes each sentence (_sentence);
    this keeps the loading, the waiting and the MP3 the same for all.
    """

    label = "local"

    def __init__(self):
        self._model = None
        self._error = None
        self._loading = threading.Lock()
        self._using = threading.Lock()
        self._thread = None
        self._downloading = False

    def voice_id(self):
        """What the cache keys this voice's phrases by."""
        raise NotImplementedError

    def ready(self):
        return self._model is not None

    def error(self):
        return self._error

    def load_in_background(self):
        """Start load() on its own thread, once."""
        if self._model is not None or self._thread is not None:
            return

        self._thread = threading.Thread(target=self.load, name=f"{self.label}-voice", daemon=True)
        self._thread.start()

    def wait(self, timeout):
        """Wait up to [timeout] seconds for a load reading the voice off the disk. True when ready.

        A download is not waited for: a first run's files can take
        minutes, and a reply should not.
        """
        thread = self._thread
        deadline = time.monotonic() + timeout

        while (thread is not None and thread.is_alive() and not self._downloading
               and time.monotonic() < deadline):
            thread.join(0.05)

        return self.ready()

    def load(self, **stand_ins):
        """Load the voice, downloading it first if this PC does not have it. True when ready.

        [stand_ins] replace the downloads and the model in tests.
        """
        with self._loading:
            if self._model is not None:
                return True

            try:
                self._model = self._open(**stand_ins)
                self._error = None
                return True

            except Exception as error:
                self._error = f"{type(error).__name__}: {error}"
                print(f"[JARVIS] the {self.label} voice could not load ({self._error}); "
                      f"the Windows voice stands in.", flush=True)
                return False

    def _open(self, **stand_ins):
        raise NotImplementedError

    def _sentence(self, text):
        """(samples, samplerate) for one sentence."""
        raise NotImplementedError

    def audio(self, text):
        """(samples, samplerate, sentence boundaries) for [text]. Raises when the voice is not loaded.

        Each sentence is made on its own, so where each starts is exact,
        not estimated, and no request is ever too long for the model.
        """
        if self._model is None:
            raise RuntimeError(f"the {self.label} voice is not loaded yet"
                               + (f" ({self._error})" if self._error else ""))

        made = []

        with self._using:
            for sentence in sentences(text):
                samples, samplerate = self._sentence(sentence)
                samples = np.asarray(samples, dtype=np.float32).reshape(-1)

                if len(samples):
                    made.append((samples, int(samplerate)))

        if not made:
            raise RuntimeError(f"the {self.label} voice made no sound for that text")

        samplerate = made[0][1]
        gap = np.zeros(int(samplerate * SENTENCE_GAP_SECONDS), dtype=np.float32)
        pieces, boundaries, start = [], [], 0

        for index, (samples, _rate) in enumerate(made):
            if index:
                pieces.append(gap)
                start += len(gap)

            boundaries.append({"offset": start / samplerate, "duration": len(samples) / samplerate, "text": ""})
            pieces.append(samples)
            start += len(samples)

        return np.concatenate(pieces), samplerate, boundaries

    def mp3(self, text):
        """(MP3 bytes, sentence boundaries) for [text]."""
        samples, samplerate, boundaries = self.audio(text)
        buffer = io.BytesIO()
        sf.write(buffer, samples, samplerate, format="MP3")
        return buffer.getvalue(), boundaries


# ---- Kokoro ----------------------------------------------------------------------------------

KOKORO_VOICE_ENV = "JARVIS_KOKORO_VOICE"
KOKORO_SPEED_ENV = "JARVIS_KOKORO_SPEED"
KOKORO_MODEL_ENV = "JARVIS_KOKORO_MODEL"

# A British man, picked by ear. The British voices are bm_fable, bm_george, bm_daniel and
# bm_lewis (men) and bf_emma, bf_isabella, bf_alice and bf_lily (women);
# the American ones start am_ and af_.
DEFAULT_KOKORO_VOICE = "bm_fable"

# Kokoro 1.0 as kokoro-onnx publishes it. fp16 is the default: as clear
# as the full model at half its size, and faster on a CPU than int8 was
# in testing (4.0 s against 6.1 s for 10 s of speech, two cores).
_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_MODELS = {
    "fp16": (f"{_RELEASE}/kokoro-v1.0.fp16.onnx",
             "c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204", "169 MB"),
    "int8": (f"{_RELEASE}/kokoro-v1.0.int8.onnx",
             "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb", "88 MB"),
}
KOKORO_VOICES = (f"{_RELEASE}/voices-v1.0.bin",
                 "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d", "27 MB")

# The model's alphabet: which phoneme is which number. Not inside the model
# file, so it comes from kokoro-onnx's source at a fixed commit.
KOKORO_VOCAB = ("https://raw.githubusercontent.com/thewh1teagle/kokoro-onnx/"
                "3596b26764286a7de9d90c363e988d50578918e5/src/kokoro_onnx/config.json",
                "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f", "3 KB")

# A voice's first letter says its accent, and so how its words are read.
KOKORO_LANGUAGES = {"a": "en-us", "b": "en-gb"}

# The model takes at most this many phonemes at once.
KOKORO_MAX_PHONEMES = 510
KOKORO_RATE = 24000

DOWNLOAD_TIMEOUT = 30


def kokoro_folder():
    """Where Kokoro's files are kept: the app's own folder in LOCALAPPDATA, not the JARVIS folder."""
    base = os.getenv("LOCALAPPDATA") or os.path.join(Path.home(), ".cache")
    return os.path.join(base, "JARVIS", "kokoro")


def _sha256(path):
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


def fetch(url, expected, path, size="", note=None):
    """[path], downloaded from [url] if it is not there, and kept only if its SHA-256 is [expected].

    Written to a .partial first and renamed once checked, so a file that
    is there was checked when it arrived.
    """
    if os.path.exists(path):
        return path

    os.makedirs(os.path.dirname(path), exist_ok=True)
    partial = f"{path}.partial"
    print(f"[JARVIS] downloading the Kokoro voice's {os.path.basename(path)} ({size}), once...", flush=True)

    if note:
        note(True)

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "JARVIS"})
        digest = hashlib.sha256()

        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, open(partial, "wb") as handle:
            for block in iter(lambda: response.read(1 << 20), b""):
                digest.update(block)
                handle.write(block)

        if digest.hexdigest() != expected:
            raise RuntimeError(f"{os.path.basename(path)} did not match its SHA-256; not used")

        os.replace(partial, path)
        print(f"[JARVIS] {os.path.basename(path)} downloaded and checked.", flush=True)
        return path

    finally:
        if note:
            note(False)

        try:
            os.remove(partial)
        except OSError:
            pass


class Kokoro(LocalVoice):
    """Kokoro, run here with onnxruntime; its words read into phonemes by espeak-ng (phonemizer)."""

    label = "Kokoro"

    def __init__(self, voice=None, speed=None, model=None):
        super().__init__()
        self.voice = (voice or os.getenv(KOKORO_VOICE_ENV) or DEFAULT_KOKORO_VOICE).strip()
        self.speed = speed if speed is not None else (_float(os.getenv(KOKORO_SPEED_ENV), 0.5, 2.0) or 1.0)
        chosen = (model or os.getenv(KOKORO_MODEL_ENV) or "fp16").strip().casefold()
        self.model = chosen if chosen in KOKORO_MODELS else "fp16"

    def voice_id(self):
        return f"kokoro:{self.voice}:{self.speed}:{self.model}"

    def _note_downloading(self, active):
        self._downloading = active

    def _open(self, folder=None, get=None, session=None, phonemes=None):
        """The model, the voice's style, the alphabet and the phoneme reader. Raises with why."""
        language = KOKORO_LANGUAGES.get(self.voice[:1])

        if language is None or not re.fullmatch(r"[a-z]{2}_[a-z]+", self.voice):
            raise ValueError(f"{KOKORO_VOICE_ENV}={self.voice!r} is not an English Kokoro voice like "
                             f"{DEFAULT_KOKORO_VOICE}")

        folder = folder or kokoro_folder()
        get = get or fetch
        url, expected, size = KOKORO_MODELS[self.model]
        model_path = get(url, expected, os.path.join(folder, os.path.basename(url)), size, self._note_downloading)
        voices_path = get(*KOKORO_VOICES[:2], os.path.join(folder, "voices-v1.0.bin"), KOKORO_VOICES[2],
                          self._note_downloading)
        vocab_path = get(*KOKORO_VOCAB[:2], os.path.join(folder, "kokoro-vocab.json"), KOKORO_VOCAB[2],
                         self._note_downloading)

        with open(vocab_path, encoding="utf-8") as handle:
            vocab = json.load(handle)["vocab"]

        with np.load(voices_path) as styles:
            if self.voice not in styles.files:
                english = ", ".join(name for name in styles.files if name[:1] in KOKORO_LANGUAGES)
                raise ValueError(f"no Kokoro voice {self.voice!r}; the English ones are {english}")

            style = np.asarray(styles[self.voice], dtype=np.float32)

        if session is None:
            import onnxruntime

            session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])

        return {"session": session, "style": style, "vocab": vocab,
                "phonemes": phonemes or _espeak(language)}

    def tokens(self, text):
        """The model's numbers for [text]'s phonemes, in pieces no longer than it takes."""
        model = self._model
        said = model["phonemes"](text)
        known = [model["vocab"][symbol] for symbol in said if symbol in model["vocab"]]
        pieces = []

        while known:
            if len(known) <= KOKORO_MAX_PHONEMES:
                pieces.append(known)
                break

            # Too long for one go: break at the last word gap that fits.
            space = model["vocab"].get(" ")
            cut = max((index for index in range(KOKORO_MAX_PHONEMES) if known[index] == space), default=0)
            cut = cut or KOKORO_MAX_PHONEMES
            pieces.append(known[:cut])
            known = known[cut:]

        return pieces

    def _sentence(self, text):
        model = self._model
        audio = []

        for piece in self.tokens(text):
            style = model["style"][min(len(piece), len(model["style"])) - 1].reshape(1, -1)
            out = model["session"].run(None, {
                "tokens": np.array([[0, *piece, 0]], dtype=np.int64),
                "style": style.astype(np.float32),
                "speed": np.array([self.speed], dtype=np.float32),
            })[0]
            audio.append(np.asarray(out, dtype=np.float32).reshape(-1))

        return (np.concatenate(audio) if audio else np.zeros(0, dtype=np.float32)), KOKORO_RATE


def _espeak(language):
    """A function reading text into phonemes with espeak-ng, the library espeakng_loader ships."""
    import espeakng_loader
    from phonemizer.backend import EspeakBackend
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    EspeakWrapper.set_library(espeakng_loader.get_library_path())
    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())

    # phonemizer warns about every word count it cannot match up; JARVIS
    # speaks the phonemes either way.
    quiet = logging.getLogger("jarvis.kokoro.phonemizer")
    quiet.setLevel(logging.ERROR)

    backend = EspeakBackend(language, preserve_punctuation=True, with_stress=True, logger=quiet)
    return lambda text: backend.phonemize([text], strip=True)[0]


def _float(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return number if low <= number <= high else None


kokoro = Kokoro()


def mp3(name, text, voice=None, rate=None, pitch=None):
    """(MP3 bytes, sentence boundaries) for [text] from engine [name] -- google, azure or kokoro."""
    if name == "google":
        return google_mp3(text)

    if name == "azure":
        return azure_mp3(text, voice, rate, pitch)

    if name in LOCAL:
        return local().mp3(text)

    raise ValueError(f"{name!r} is not made here")
