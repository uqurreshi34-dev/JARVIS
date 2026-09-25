"""Speech-to-text engines behind one interface.

Vosk streams and decides its own utterance boundaries. Whisper transcribes a
complete utterance at once, so it needs speech segmentation with a pre-roll
buffer, otherwise the first word is clipped.

Both engines expose the same contract:

    result = engine.feed(block)   # None until an utterance completes
    engine.reset()                # discard anything part-heard

`result` is a Result(text, confidence); confidence is None where the engine
does not report one.
"""

import json
import os
import time
from collections import deque
from dataclasses import dataclass
import threading
import numpy as np
from dotenv import load_dotenv


load_dotenv()


SAMPLE_RATE = 16000

# "vosk" is light and instant; "whisper" is far more accurate, especially on
# short words like "add" and "at", at the cost of some latency and memory.
ENGINE = (os.getenv("STT_ENGINE") or "vosk").strip().casefold()

# base.en is a good balance. small.en is more accurate and roughly twice the
# work; tiny.en is faster and noticeably worse.
WHISPER_MODEL = os.getenv("WHISPER_MODEL") or "base.en"

# Set True to report how long each utterance took to capture.
TIMING = False


# Whisper guesses proper nouns badly unless told they exist, rendering
# "Jarvis" as "java's" or "jovis". Naming the expected vocabulary biases
# decoding and fixes most mishearings at the source rather than afterwards.
WHISPER_PROMPT = os.getenv("WHISPER_PROMPT") or (
    "Jarvis. Commands for Jarvis: open, close, launch, volume, mute, "
    "clipboard, notes, screenshot, timer, reminder, weather, Outlook, "
    "Chrome, Cursor, pgAdmin, YouTube, minimise, system."
)

# Whisper's prompt is capped at around 224 tokens, so only the most recent
# names are added: those are the ones being used.
MAX_PROMPT_NAMES = 40

# The names are re-read this often rather than on every utterance, since
# reading a file between every word would be wasteful.
NAME_REFRESH_SECONDS = 30

_names = {"words": (), "at": 0.0}


def _known_names():
    """Names the user has told JARVIS to accept, for biasing the decoder.

    The spelling ignore list is exactly the set of words this user says but
    a dictionary does not know -- names, places, jargon. Telling Whisper to
    expect them is the same trick that stopped "Jarvis" arriving as
    "java's", applied to the user's own vocabulary.
    """
    now = time.monotonic()

    if _names["words"] and now - _names["at"] < NAME_REFRESH_SECONDS:
        return _names["words"]

    words = []

    try:
        from actions.proofread import ignored_words

        words.extend(
            word for word in ignored_words()
            if word and word.isalpha() and len(word) > 2
        )

    except Exception:
        pass

    try:
        # Contact names, for the same reason. "call dad" arriving as
        # "call that" is the identical failure to "Jarvis" arriving as
        # "java's": the decoder had no reason to expect the word. Fixing
        # it here means correcting the transcription rather than
        # maintaining a list of the ways it can go wrong -- which would
        # only ever describe one voice on one microphone anyway.
        from actions.contacts import names as contact_names

        words.extend(
            name for name in contact_names()
            if name and name.replace(" ", "").isalpha()
        )

    except Exception:
        pass

    words = sorted(set(words))

    _names["words"] = tuple(words[-MAX_PROMPT_NAMES:])
    _names["at"] = now

    return _names["words"]


def _prompt():
    """The decoding prompt, including any names the user has taught."""
    names = _known_names()

    if not names:
        return WHISPER_PROMPT

    return f"{WHISPER_PROMPT} Names: {', '.join(names)}."


@dataclass
class Result:
    text: str
    confidence: float = None


# --------------------------------------------------------------------------
# Vosk
# --------------------------------------------------------------------------

# When the partial transcript stops changing for this long, close the
# utterance rather than waiting for Vosk's own endpointer.
FORCE_ENDPOINT_SILENCE = 0.45
SHORT_UTTERANCE_SILENCE = 1.0
PARTIAL_UTTERANCE_SILENCE = 0.7


class VoskEngine:
    name = "vosk"
    supports_grammar = True

    def __init__(self, model, block_seconds):
        from vosk import KaldiRecognizer

        self._model = model
        self._make = KaldiRecognizer
        self._block_seconds = block_seconds

        self._recognizer = self._new_recognizer()
        self._last_partial = ""
        self._quiet_blocks = 0

    def _new_recognizer(self):
        recognizer = self._make(self._model, SAMPLE_RATE)
        recognizer.SetWords(True)

        return recognizer

    def reset(self):
        self._recognizer.Reset()
        self._last_partial = ""
        self._quiet_blocks = 0

    @property
    def active(self):
        """True while part-way through hearing something."""
        return bool(self._last_partial)

    def _delay_for(self, partial):
        words = len(partial.split())

        if words <= 2:
            return SHORT_UTTERANCE_SILENCE

        if words == 3:
            return PARTIAL_UTTERANCE_SILENCE

        return FORCE_ENDPOINT_SILENCE

    def feed(self, block):
        if self._recognizer.AcceptWaveform(block):
            return self._finish()

        partial = json.loads(
            self._recognizer.PartialResult()
        ).get("partial", "").strip()

        if partial != self._last_partial:
            self._last_partial = partial
            self._quiet_blocks = 0
            return None

        if not partial:
            return None

        self._quiet_blocks += 1
        quiet_seconds = self._quiet_blocks * self._block_seconds

        if quiet_seconds >= self._delay_for(partial):
            return self._finish()

        return None

    def _finish(self):
        payload = json.loads(self._recognizer.Result())

        self._last_partial = ""
        self._quiet_blocks = 0

        text = payload.get("text", "").strip()

        if not text:
            return None

        words = payload.get("result") or []
        scores = [
            word["conf"] for word in words
            if isinstance(word, dict) and "conf" in word
        ]

        confidence = sum(scores) / len(scores) if scores else None

        return Result(text, confidence)


# --------------------------------------------------------------------------
# Whisper
# --------------------------------------------------------------------------

# Speech is detected by loudness relative to the room. The floor adapts, so a
# noisy room raises the bar rather than triggering constantly.
_NOISE_MARGIN = 2.5
_MIN_THRESHOLD = 0.004

# Audio kept from before speech was detected, so the first word survives.
_PRE_ROLL_BLOCKS = 3

# Silence that ends an utterance, and a ceiling so a noisy room cannot buffer
# forever. Shorter silence means less waiting before transcription starts.
_END_SILENCE_SECONDS = 0.55

# An utterance is over when the level falls to this fraction of its own peak.
# Judging against the peak rather than a fixed threshold means background
# noise cannot keep the recording open indefinitely.
_END_FRACTION = 0.30

# How quickly the room's noise floor is learned, and how much the level is
# smoothed. Both trade responsiveness against stability.
_FLOOR_ALPHA = 0.25
_LEVEL_ALPHA = 0.45
# Commands are short. If the end of speech is somehow missed, this caps how
# long JARVIS can sit recording before giving up and transcribing what it has.
_MAX_UTTERANCE_SECONDS = 7.0
_MIN_UTTERANCE_SECONDS = 0.3


class SegmentingEngine:
    """Buffers a complete utterance, then hands it to a transcriber.

    Whisper is not a streaming model, so speech has to be detected, captured
    with a pre-roll so the first word survives, and closed on silence.
    """

    name = "segmenting"
    supports_grammar = False

    def __init__(self, block_seconds):
        self._block_seconds = block_seconds
        self._pre_roll = deque(maxlen=_PRE_ROLL_BLOCKS)
        self._buffer = []
        self._speaking = False
        self._quiet_blocks = 0
        self._noise_floor = _MIN_THRESHOLD
        self._peak = 0.0
        self._started = None
        self._smoothed = 0.0

    def reset(self):
        self._pre_roll.clear()
        self._buffer = []
        self._speaking = False
        self._quiet_blocks = 0
        self._peak = 0.0
        self._started = None

    @property
    def active(self):
        """True while capturing an utterance, before transcription runs."""
        return self._speaking

    @staticmethod
    def _to_float(block):
        return np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0

    def feed(self, block):
        samples = self._to_float(block)
        raw = float(np.sqrt(np.mean(np.square(samples)))
                    ) if len(samples) else 0.0

        # Smooth the level: a single loud block of background noise should
        # not reset the silence counter and hold the recording open.
        self._smoothed = (
            (1 - _LEVEL_ALPHA) * self._smoothed + _LEVEL_ALPHA * raw
        )
        level = self._smoothed

        threshold = max(self._noise_floor * _NOISE_MARGIN, _MIN_THRESHOLD)

        if not self._speaking:
            # Track the room while nothing is being said.
            self._noise_floor = (
                (1 - _FLOOR_ALPHA) * self._noise_floor + _FLOOR_ALPHA * level
            )
            self._pre_roll.append(samples)

            if level > threshold:
                self._speaking = True
                self._quiet_blocks = 0
                self._peak = level
                self._started = time.monotonic()
                self._buffer = list(self._pre_roll)

            return None

        self._buffer.append(samples)
        self._peak = max(self._peak, level)

        # Ending on a fixed threshold fails in a noisy room: the level never
        # returns below it, so the utterance runs to the ceiling and JARVIS
        # sits recording in silence. Instead, close when the level falls back
        # most of the way from this utterance's peak toward the room's own
        # noise floor, which works at any background level.
        floor = self._noise_floor
        quiet = level < floor + (self._peak - floor) * _END_FRACTION

        if quiet:
            self._quiet_blocks += 1
        else:
            self._quiet_blocks = 0

        quiet_seconds = self._quiet_blocks * self._block_seconds
        spoken_seconds = len(self._buffer) * self._block_seconds

        if quiet_seconds >= _END_SILENCE_SECONDS:
            return self._finish()

        if spoken_seconds >= _MAX_UTTERANCE_SECONDS:
            print("[JARVIS] utterance hit the length limit")
            return self._finish()

        return None

    def _finish(self):
        audio = np.concatenate(self._buffer) if self._buffer else np.array([])

        if TIMING and self._started:
            print(
                f"[timing] captured {len(audio) / SAMPLE_RATE:.2f}s of audio "
                f"over {time.monotonic() - self._started:.2f}s",
                flush=True,
            )

        self.reset()

        if len(audio) < SAMPLE_RATE * _MIN_UTTERANCE_SECONDS:
            return None

        try:
            text = self._transcribe(audio)
        except Exception as error:
            print(f"[JARVIS] transcription failed: {error}")
            return None

        if not text:
            return None

        # Whisper punctuates and capitalises; the command layer expects plain
        # lowercase words.
        text = text.strip(" .,!?").casefold()

        return Result(text, None)

    def _transcribe(self, audio):
        raise NotImplementedError


class LocalWhisperEngine(SegmentingEngine):
    """Whisper running on this machine via faster-whisper."""

    name = "whisper"

    def __init__(self, block_seconds):
        super().__init__(block_seconds)

        from faster_whisper import WhisperModel

        print(f"[JARVIS] loading Whisper model {WHISPER_MODEL}...")

        self._model = WhisperModel(
            WHISPER_MODEL, device="cpu", compute_type="int8"
        )

        print("[JARVIS] Whisper ready")

    def _transcribe(self, audio):
        segments, _ = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=_prompt(),
        )

        return " ".join(segment.text.strip() for segment in segments).strip()


class OpenAIWhisperEngine(SegmentingEngine):
    """Whisper running locally on PyTorch.

    Slower than faster-whisper, but it avoids ctranslate2's unsigned native
    library, which Windows Application Control blocks on some machines.
    """

    name = "openai-whisper"

    def __init__(self, block_seconds):
        super().__init__(block_seconds)

        import whisper

        print(f"[JARVIS] loading local Whisper model {WHISPER_MODEL}...")

        self._model = whisper.load_model(WHISPER_MODEL)

        print("[JARVIS] Whisper ready")

    def _transcribe(self, audio):
        result = self._model.transcribe(
            audio,
            language="en",
            fp16=False,
            condition_on_previous_text=False,
            initial_prompt=_prompt(),
            # A single temperature stops Whisper retrying the decode up to
            # six times when its quality thresholds fail, which is the main
            # source of multi-second delays on short commands.
            temperature=0.0,
            without_timestamps=True,
            # These thresholds trigger those retries, so they are disabled.
            compression_ratio_threshold=None,
            logprob_threshold=None,
            no_speech_threshold=None,
        )

        return (result.get("text") or "").strip()


class GroqWhisperEngine(SegmentingEngine):
    """Whisper large-v3-turbo on Groq's servers, with local Whisper standing by.

    The most accurate engine JARVIS has, and free within Groq's allowance
    (2,000 requests and about eight hours of audio a day). Nothing is
    compiled or installed locally, which matters on machines where Windows
    blocks unsigned native libraries.

    A cloud engine can fail mid-session: the network drops, the allowance
    runs out, Groq has a bad minute. Local Whisper is loaded in the
    background at start-up, so when a request fails the same utterance is
    transcribed locally on the spot -- nothing has to be said twice -- and
    Groq is rested for a while before being tried again, longer each time
    it keeps failing, and as long as it asks when it is rate limiting.
    """

    name = "groq-whisper"

    # A cloud request that has not answered by now is abandoned for the
    # local model. Groq usually answers in well under a second.
    CLOUD_TIMEOUT = 6.0

    # How long Groq is left alone after a failure, doubling while it keeps
    # failing, up to the ceiling. A success clears it.
    REST_SECONDS = 30.0
    MAX_REST_SECONDS = 600.0

    def __init__(self, block_seconds, local_factories=None):
        super().__init__(block_seconds)

        import io
        import wave

        from openai import OpenAI

        self._io = io
        self._wave = wave

        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is needed for cloud transcription")

        # No retries: a retry is time the local model could be answering in.
        self._client = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
            timeout=self.CLOUD_TIMEOUT,
            max_retries=0,
        )

        self._model_name = os.getenv(
            "GROQ_WHISPER_MODEL") or "whisper-large-v3-turbo"

        self._rest_until = 0.0
        self._rest = 0.0
        self._local = None
        self._local_ready = threading.Event()
        self._local_factories = (
            local_factories if local_factories is not None
            else (LocalWhisperEngine, OpenAIWhisperEngine)
        )

        threading.Thread(target=self._load_local, daemon=True).start()

        print(f"[JARVIS] cloud transcription via {self._model_name}, local Whisper standing by")

    def _load_local(self):
        """Load the local stand-in once, quietly, in the background."""
        try:
            for factory in self._local_factories:
                try:
                    self._local = factory(self._block_seconds)
                    return
                except Exception as error:
                    print(f"[JARVIS] local stand-in {getattr(factory, 'name', factory)} unavailable: {error}")

            print("[JARVIS] no local Whisper to stand in for the cloud")
        finally:
            self._local_ready.set()

    def _to_wav(self, audio):
        buffer = self._io.BytesIO()

        with self._wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(
                (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            )

        buffer.seek(0)
        buffer.name = "speech.wav"

        return buffer

    def _cloud(self, audio):
        response = self._client.audio.transcriptions.create(
            file=self._to_wav(audio),
            model=self._model_name,
            language="en",
            temperature=0,
            prompt=_prompt(),
        )

        return (response.text or "").strip()

    def _rest_after(self, error):
        """Leave Groq alone for a while, as long as it asked if it said."""
        wait = self._retry_after(error)

        if wait is None:
            self._rest = min(self.MAX_REST_SECONDS, self._rest * 2 if self._rest else self.REST_SECONDS)
            wait = self._rest

        self._rest_until = time.monotonic() + wait

        print(
            f"[JARVIS] cloud transcription failed ({type(error).__name__}: {error}); "
            f"using local Whisper, trying Groq again in {int(wait)}s",
            flush=True,
        )

    @staticmethod
    def _retry_after(error):
        """Seconds a rate-limited response asked to wait, or None."""
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None) or {}

        try:
            value = headers.get("retry-after")
            return max(1.0, float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _transcribe(self, audio):
        if time.monotonic() >= self._rest_until:
            try:
                text = self._cloud(audio)
            except Exception as error:
                self._rest_after(error)
            else:
                if self._rest:
                    print("[JARVIS] cloud transcription is back", flush=True)

                self._rest = 0.0
                return text

        return self._transcribe_locally(audio)

    def _transcribe_locally(self, audio):
        # Normally long since loaded; at start-up it may still be loading.
        self._local_ready.wait(timeout=60)

        if self._local is None:
            raise RuntimeError("cloud transcription failed and no local Whisper is available")

        return self._local._transcribe(audio)


def build_engine(model, block_size):
    """Create the configured engine, falling back when one cannot start.

    STT_ENGINE accepts:
      "vosk"    - streaming, instant, least accurate (default)
      "whisper" - local Whisper; tries faster-whisper, then PyTorch
      "groq"    - Whisper via Groq's API, the only option that uses network;
                  local Whisper stands in on the spot when a request fails

    A local choice never silently falls back to the cloud, since that would
    spend API quota on commands the user expects to be free. It falls back to
    Vosk instead.
    """
    block_seconds = block_size / SAMPLE_RATE

    if ENGINE in ("whisper", "local", "local-whisper"):
        attempts = [LocalWhisperEngine, OpenAIWhisperEngine]
    elif ENGINE in ("groq", "groq-whisper", "cloud"):
        # Without a key or the openai package, local Whisper rather than
        # Vosk: the user chose accuracy.
        attempts = [GroqWhisperEngine, LocalWhisperEngine, OpenAIWhisperEngine]
    else:
        attempts = []

    for factory in attempts:
        try:
            return factory(block_seconds)
        except Exception as error:
            print(f"[JARVIS] could not start {factory.name} ({error})")

    if attempts:
        print("[JARVIS] falling back to Vosk (no network used)")

    return VoskEngine(model, block_seconds)


_remote_lock = threading.Lock()


def transcribe_pcm16(pcm_bytes):
    """Transcribe 16-bit mono PCM using JARVIS's existing STT engine.

    The phone sends raw PCM rather than browser-specific WebM/Opus so no
    ffmpeg or other decoder is needed.
    """
    if not pcm_bytes:
        return ""

    samples = (
        np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
        / 32768.0
    )

    if not len(samples):
        return ""

    with _remote_lock:
        # The live engine is created by voice.py after transcriber.py
        # finishes importing. Import it here at call time to avoid the
        # transcriber <-> voice circular import during startup.
        from voice import engine as active_engine

        if isinstance(active_engine, VoskEngine):
            from vosk import KaldiRecognizer

            recognizer = KaldiRecognizer(
                active_engine._model,
                SAMPLE_RATE,
            )
            recognizer.SetWords(False)
            recognizer.AcceptWaveform(pcm_bytes)

            payload = json.loads(recognizer.FinalResult())

            return (payload.get("text") or "").strip().casefold()

        text = active_engine._transcribe(samples)

    return (text or "").strip().casefold()


def transcribe_wav(wav_bytes):
    """Decode a mono PCM WAV and send its audio to the configured STT engine."""
    import io
    import wave

    if not wav_bytes:
        return ""

    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if channels != 1 or width != 2 or rate != SAMPLE_RATE:
        raise ValueError(
            "Phone audio must be 16-bit mono PCM at 16000 Hz."
        )

    return transcribe_pcm16(frames)
