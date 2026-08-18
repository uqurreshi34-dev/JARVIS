import pyttsx3


class SpeechEngine:
    """Windows text-to-speech engine with isolated utterance lifecycle."""

    def __init__(self, rate=175):
        self.rate = rate

    def speak(self, text):
        print(f"JARVIS: {text}")

        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        engine.say(text)
        engine.runAndWait()
        engine.stop()


speech = SpeechEngine()


def speak(text):
    speech.speak(text)
