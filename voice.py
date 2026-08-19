import json
import queue

import sounddevice as sd
from vosk import KaldiRecognizer, Model


MODEL_PATH = "model"
SAMPLE_RATE = 16000

audio_queue = queue.Queue()
model = Model(MODEL_PATH)


def audio_callback(indata, frames, time, status):
    if status:
        print(status)

    audio_queue.put(bytes(indata))


def listen():
    print("Listening...")

    recognizer = KaldiRecognizer(model, SAMPLE_RATE)

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=8000,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        while True:
            data = audio_queue.get()

            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                text = result.get("text", "").strip()

                if text:
                    print(f"You said: {text}")
                    return text
