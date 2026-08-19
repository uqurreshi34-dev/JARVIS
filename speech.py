import asyncio
import os
import tempfile

import edge_tts
import numpy as np
import sounddevice as sd
import soundfile as sf

import pyttsx3


VOICE = "en-GB-RyanNeural"

# Playback chunk size; smaller means the HUD reacts more finely.
_BLOCK = 1024

# Scales raw RMS up to a usable 0..1 range for the HUD.
_GAIN = 6.0


class SpeechEngine:
    """Neural text-to-speech with a local SAPI5 fallback.

    While speaking, the per-block RMS of the audio is reported to an optional
    listener so a UI can pulse in time with the voice.
    """

    def __init__(self, voice=VOICE, rate=175):
        self.voice = voice
        self.rate = rate
        self._amplitude_listener = None

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
        print(f"JARVIS: {text}")

        try:
            self._speak_neural(text)
        except Exception as error:
            print(
                f"[JARVIS] neural voice unavailable ({error}); using fallback.")
            self._speak_fallback(text)
        finally:
            self._report(0.0)

    def _speak_neural(self, text):
        path = os.path.join(tempfile.gettempdir(), "jarvis_tts.mp3")

        asyncio.run(self._synthesize(text, path))

        data, samplerate = sf.read(path, dtype="float32")

        if data.ndim > 1:
            data = data.mean(axis=1)

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

        stream = sd.OutputStream(
            samplerate=samplerate,
            channels=1,
            blocksize=_BLOCK,
            dtype="float32",
            callback=callback,
        )

        with stream:
            while stream.active:
                sd.sleep(20)

    async def _synthesize(self, text, path):
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(path)

    def _speak_fallback(self, text):
        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        engine.say(text)
        engine.runAndWait()
        engine.stop()


speech = SpeechEngine()


def speak(text):
    speech.speak(text)


def set_amplitude_listener(listener):
    speech.set_amplitude_listener(listener)
