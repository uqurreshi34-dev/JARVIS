import asyncio
import hashlib
import os
import tempfile
import threading
import time
from collections import OrderedDict

import edge_tts
import numpy as np
import sounddevice as sd
import soundfile as sf

import pyttsx3


VOICE = os.getenv("JARVIS_VOICE") or "en-GB-RyanNeural"

# The film's JARVIS is measured and slightly clipped. Slowing the delivery a
# little and dropping the pitch gets closer to that than the stock reading.
# Both accept forms like "-8%" and "-6Hz"; set them empty for the default.
VOICE_RATE = os.getenv("JARVIS_VOICE_RATE", "-7%")
VOICE_PITCH = os.getenv("JARVIS_VOICE_PITCH", "-4Hz")

# Playback chunk size; smaller means the HUD reacts more finely.
_BLOCK = 1024

# Scales raw RMS up to a usable 0..1 range for the HUD.
_GAIN = 6.0

# Synthesised audio is cached on disk and in memory. JARVIS repeats himself
# constantly ("Done, sir."), and every fresh synthesis is a network round trip.
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "jarvis_tts_cache")
_MEMORY_LIMIT = 48

# Set True to print how long synthesis and playback take.
TIMING = False

# Incremented after every completed utterance so the listener can tell that
# JARVIS has spoken, and discard whatever the microphone picked up.
_epoch = 0
_epoch_lock = threading.Lock()

_speaking = threading.Event()

# Set while a real reply is being prepared, so background cache warming
# stands aside rather than making the reply queue behind it.
_priority = threading.Event()

# Pause between warmed phrases, leaving the connection free for real replies.
_PREWARM_GAP = 0.4


def speech_epoch():
    """A counter that changes each time JARVIS finishes speaking."""
    with _epoch_lock:
        return _epoch


def is_speaking():
    """True while audio is actually being played."""
    return _speaking.is_set()


def _bump_epoch():
    global _epoch

    with _epoch_lock:
        _epoch += 1


class SpeechEngine:
    """Neural text-to-speech with caching and a local SAPI5 fallback.

    While speaking, the per-block RMS of the audio is reported to an optional
    listener so a UI can pulse in time with the voice.
    """

    def __init__(self, voice=VOICE, rate=175):
        self.voice = voice
        self.rate = rate
        self._amplitude_listener = None

        # Reminders fire on their own thread, so utterances must not overlap.
        self._lock = threading.Lock()

        # Decoded audio keyed by phrase, most recently used last.
        self._memory = OrderedDict()

        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
        except OSError:
            pass

    def set_amplitude_listener(self, listener):
        """Register a callable taking a float 0..1, or None to clear."""
        self._amplitude_listener = listener

    def _report(self, value):
        if self._amplitude_listener:
            try:
                self._amplitude_listener(float(value))
            except Exception:
                pass

    def speak(self, text):
        print(f"JARVIS: {text}", flush=True)

        _priority.set()

        with self._lock:
            _speaking.set()

            try:
                self._speak_neural(text)
            except Exception as error:
                print(
                    f"[JARVIS] neural voice unavailable ({error}); using fallback.")
                self._speak_fallback(text)
            finally:
                self._report(0.0)
                _speaking.clear()
                _priority.clear()
                _bump_epoch()

    def prewarm(self, phrases):
        """Synthesise phrases ahead of time so they play instantly later.

        This runs in the background at startup and must never delay a real
        reply, so it waits whenever JARVIS is actually speaking and pauses
        between phrases to leave the connection free.
        """
        for phrase in phrases:
            # A real utterance takes priority; wait for it to finish.
            while _priority.is_set():
                time.sleep(0.05)

            try:
                self._audio_for(phrase)
            except Exception as error:
                print(f"[JARVIS] could not prewarm {phrase!r}: {error}")

            # Leave a gap so a command arriving now is not stuck behind a
            # run of back-to-back requests.
            time.sleep(_PREWARM_GAP)

    def _cache_key(self, text):
        digest = hashlib.sha1(
            f"{self.voice}|{VOICE_RATE}|{VOICE_PITCH}|{text}".encode("utf-8")
        ).hexdigest()

        return digest

    def _audio_for(self, text):
        """Return (samples, samplerate), synthesising only when necessary."""
        key = self._cache_key(text)

        cached = self._memory.get(key)

        if cached is not None:
            self._memory.move_to_end(key)
            return cached

        path = os.path.join(_CACHE_DIR, f"{key}.mp3")

        started = time.monotonic()
        synthesised = False

        if not os.path.exists(path):
            asyncio.run(self._synthesize(text, path))
            synthesised = True

        data, samplerate = sf.read(path, dtype="float32")

        if data.ndim > 1:
            data = data.mean(axis=1)

        self._memory[key] = (data, samplerate)
        self._memory.move_to_end(key)

        while len(self._memory) > _MEMORY_LIMIT:
            self._memory.popitem(last=False)

        if TIMING:
            source = "synthesised" if synthesised else "disk cache"
            print(
                f"[timing] {source} in "
                f"{time.monotonic() - started:.2f}s: {text[:40]!r}",
                flush=True,
            )

        return data, samplerate

    def _speak_neural(self, text):
        data, samplerate = self._audio_for(text)

        self._play_reactive(data, samplerate)

    def _play_reactive(self, data, samplerate):
        """Stream the audio, reporting the envelope of each block as it plays."""
        position = 0
        total = len(data)

        def callback(outdata, frames, time_info, status):
            nonlocal position

            if status:
                print(status)

            end = position + frames
            chunk = data[position:end]

            if len(chunk) < frames:
                outdata[:len(chunk), 0] = chunk
                outdata[len(chunk):, 0] = 0.0
            else:
                outdata[:, 0] = chunk

            if len(chunk):
                rms = float(np.sqrt(np.mean(np.square(chunk))))
                self._report(min(1.0, rms * _GAIN))

            position = end

            if position >= total:
                raise sd.CallbackStop

        opening = time.monotonic()

        stream = sd.OutputStream(
            samplerate=samplerate,
            channels=1,
            blocksize=_BLOCK,
            dtype="float32",
            callback=callback,
        )

        with stream:
            if TIMING:
                print(
                    f"[timing] audio device opened in "
                    f"{time.monotonic() - opening:.2f}s, "
                    f"playing {total / samplerate:.2f}s of speech",
                    flush=True,
                )

            playing = time.monotonic()

            while stream.active:
                sd.sleep(20)

            if TIMING:
                print(
                    f"[timing] playback took "
                    f"{time.monotonic() - playing:.2f}s"
                )

    async def _synthesize(self, text, path):
        options = {}

        if VOICE_RATE:
            options["rate"] = VOICE_RATE

        if VOICE_PITCH:
            options["pitch"] = VOICE_PITCH

        communicate = edge_tts.Communicate(text, self.voice, **options)

        # Write to a temporary name first so an interrupted download cannot
        # leave a corrupt file in the cache.
        partial = f"{path}.partial"

        await communicate.save(partial)

        os.replace(partial, path)

    def _speak_fallback(self, text):
        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        engine.say(text)
        engine.runAndWait()
        engine.stop()


speech = SpeechEngine()


# Phrases JARVIS repeats constantly. Synthesised once at startup, then instant.
COMMON_PHRASES = (
    "Yes, sir?",
    "Done, sir.",
    "Certainly, sir.",
    "I don't know how to do that yet.",
    "I couldn't find that out, sir.",
    "I couldn't open that, sir.",
    "I couldn't close that, sir.",
    "That didn't work, sir.",
    "Muting, sir.",
    "Unmuting, sir.",
    "Turning it up, sir.",
    "Turning it down, sir.",
    "Clearing the desktop, sir.",
    "Bringing them back, sir.",
    "That file exists. Overwrite it, sir?",
    "Very good, sir.",
    "Cancelled, sir.",
    "Noted, sir.",
    "Copied, sir.",
    "Added, sir.",
)


def speak(text):
    speech.speak(text)


def set_amplitude_listener(listener):
    speech.set_amplitude_listener(listener)


def prewarm(lines=None):
    """Warm the cache so the most common replies never wait on the network."""
    if lines is None:
        lines = list(COMMON_PHRASES)

        # Every wording variant, so no phrasing is slow the first time.
        try:
            import phrases as phrasebook

            lines.extend(phrasebook.every_fixed_line())
        except Exception as error:
            print(f"[JARVIS] could not list phrase variants: {error}")

    speech.prewarm(tuple(dict.fromkeys(lines)))
