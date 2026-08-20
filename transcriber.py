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
from collections import deque
from dataclasses import dataclass

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

# Seconds of audio per block, used to convert block counts into time.
_BLOCK_SECONDS = None


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
# forever.
_END_SILENCE_SECONDS = 0.7
_MAX_UTTERANCE_SECONDS = 20.0
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

    def reset(self):
        self._pre_roll.clear()
        self._buffer = []
        self._speaking = False
        self._quiet_blocks = 0

    @property
    def active(self):
        """True while capturing an utterance, before transcription runs."""
        return self._speaking

    @staticmethod
    def _to_float(block):
        return np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0

    def feed(self, block):
        samples = self._to_float(block)
        level = float(np.sqrt(np.mean(np.square(samples)))
                      ) if len(samples) else 0.0

        threshold = max(self._noise_floor * _NOISE_MARGIN, _MIN_THRESHOLD)
        loud = level > threshold

        if not self._speaking:
            # Track the room while nothing is being said.
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * level
            self._pre_roll.append(samples)

            if loud:
                self._speaking = True
                self._quiet_blocks = 0
                self._buffer = list(self._pre_roll)

            return None

        self._buffer.append(samples)

        if loud:
            self._quiet_blocks = 0
        else:
            self._quiet_blocks += 1

        quiet_seconds = self._quiet_blocks * self._block_seconds
        spoken_seconds = len(self._buffer) * self._block_seconds

        if quiet_seconds >= _END_SILENCE_SECONDS:
            return self._finish()

        if spoken_seconds >= _MAX_UTTERANCE_SECONDS:
            return self._finish()

        return None

    def _finish(self):
        audio = np.concatenate(self._buffer) if self._buffer else np.array([])

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
        )

        return (result.get("text") or "").strip()


class GroqWhisperEngine(SegmentingEngine):
    """Whisper through Groq's audio API.

    Nothing is compiled or installed locally, which matters on machines where
    Windows blocks unsigned native libraries. Audio is billed against a
    seconds-of-audio quota, separate from the chat token allowance.
    """

    name = "groq-whisper"

    def __init__(self, block_seconds):
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

        self._client = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
        )

        self._model_name = os.getenv(
            "GROQ_WHISPER_MODEL") or "whisper-large-v3-turbo"

        print(f"[JARVIS] cloud transcription via {self._model_name}")

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

    def _transcribe(self, audio):
        response = self._client.audio.transcriptions.create(
            file=self._to_wav(audio),
            model=self._model_name,
            language="en",
            temperature=0,
        )

        return (response.text or "").strip()


def build_engine(model, block_size):
    """Create the configured engine, falling back when one cannot start.

    STT_ENGINE accepts:
      "vosk"    - streaming, instant, least accurate (default)
      "whisper" - local Whisper; tries faster-whisper, then PyTorch
      "groq"    - Whisper via Groq's API, the only option that uses network

    A local choice never silently falls back to the cloud, since that would
    spend API quota on commands the user expects to be free. It falls back to
    Vosk instead.
    """
    block_seconds = block_size / SAMPLE_RATE

    if ENGINE in ("whisper", "local", "local-whisper"):
        attempts = [LocalWhisperEngine, OpenAIWhisperEngine]
    elif ENGINE in ("groq", "groq-whisper", "cloud"):
        attempts = [GroqWhisperEngine]
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
