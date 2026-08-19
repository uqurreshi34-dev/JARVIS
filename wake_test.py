"""Measure how reliably Vosk hears the wake word.

Run:  python wake_test.py
Then say "Jarvis" clearly, once, each time it prompts. Ctrl+C to stop.
"""

import json
import queue

import sounddevice as sd
from vosk import KaldiRecognizer, Model

import voice


print("Loading model from", voice.MODEL_PATH)
model = voice.model

print()
print("=== grammar mode check ===")

grammar_recognizer = None

try:
    grammar_recognizer = KaldiRecognizer(
        model, voice.SAMPLE_RATE, voice.WAKE_GRAMMAR
    )
    print("grammar mode:      SUPPORTED")
except Exception as error:
    print("grammar mode:      UNAVAILABLE —", error)

full_recognizer = KaldiRecognizer(model, voice.SAMPLE_RATE)
full_recognizer.SetWords(True)

audio = queue.Queue()


def callback(indata, frames, time_info, status):
    audio.put(bytes(indata))


print()
print("Say 'Jarvis' on its own, repeatedly. Ctrl+C to finish.")
print("Columns: what the full model heard | what grammar mode heard | verdict")
print("-" * 74)

attempts = 0
grammar_hits = 0
fuzzy_hits = 0

try:
    with sd.RawInputStream(
        samplerate=voice.SAMPLE_RATE,
        blocksize=8000,
        dtype="int16",
        channels=1,
        callback=callback,
    ):
        while True:
            data = audio.get()

            grammar_text = ""

            if grammar_recognizer and grammar_recognizer.AcceptWaveform(data):
                grammar_text = json.loads(
                    grammar_recognizer.Result()
                ).get("text", "")

            if not full_recognizer.AcceptWaveform(data):
                continue

            result = json.loads(full_recognizer.Result())
            text = result.get("text", "").strip()

            if not text:
                continue

            attempts += 1

            grammar_ok = voice.WAKE_WORD in grammar_text.split()
            fuzzy_ok, _ = voice._split_wake(text)

            if grammar_ok:
                grammar_hits += 1

            if fuzzy_ok:
                fuzzy_hits += 1

            if grammar_ok or fuzzy_ok:
                verdict = "WAKE"
                if grammar_ok and not fuzzy_ok:
                    verdict = "WAKE (grammar saved it)"
            else:
                verdict = "missed"

            print(f"{text[:30]:30} | {grammar_text[:16]:16} | {verdict}")

except KeyboardInterrupt:
    print()
    print("-" * 74)
    print(f"utterances heard : {attempts}")

    if attempts:
        print(
            f"grammar detected : {grammar_hits} "
            f"({100 * grammar_hits / attempts:.0f}%)"
        )
        print(
            f"fuzzy detected   : {fuzzy_hits} "
            f"({100 * fuzzy_hits / attempts:.0f}%)"
        )
