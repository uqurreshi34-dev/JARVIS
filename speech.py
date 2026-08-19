import asyncio
import os
import tempfile

import edge_tts
import sounddevice as sd
import soundfile as sf

import pyttsx3


VOICE = "en-GB-RyanNeural"


class SpeechEngine:
    """Neural text-to-speech with a local SAPI5 fallback.

    Primary path uses Microsoft's edge-tts neural voices. If that fails for
    any reason (no network, audio decode issue), it falls back to pyttsx3 so
    JARVIS is never left silent.
    """

    def __init__(self, voice=VOICE, rate=175):
        self.voice = voice
        self.rate = rate

    def speak(self, text):
        print(f"JARVIS: {text}")

        try:
            self._speak_neural(text)
        except Exception as error:
            print(
                f"[JARVIS] neural voice unavailable ({error}); using fallback.")
            self._speak_fallback(text)

    def _speak_neural(self, text):
        path = os.path.join(tempfile.gettempdir(), "jarvis_tts.mp3")

        asyncio.run(self._synthesize(text, path))

        data, samplerate = sf.read(path, dtype="float32")

        sd.play(data, samplerate)
        sd.wait()

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
