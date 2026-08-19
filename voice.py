import json
import queue

import sounddevice as sd
from vosk import KaldiRecognizer, Model


MODEL_PATH = "model"
SAMPLE_RATE = 16000

# Reject a result when Vosk's own average word confidence is below this.
# Real speech typically scores well above 0.8; noise misheard as a word
# usually scores far lower.
MIN_CONFIDENCE = 0.70

# Single short words that noise commonly decodes into. These are only
# rejected when they arrive ALONE -- "the" inside a real command is fine.
FILLERS = frozenset({
    "huh", "but", "a", "the", "oh", "uh", "um", "eh", "hm", "hmm",
    "and", "i", "it", "he", "she", "you", "to", "so", "no", "yeah",
    "yes", "what", "who", "that", "this", "of", "or", "on", "in",
})

audio_queue = queue.Queue()
model = Model(MODEL_PATH)


def audio_callback(indata, frames, time, status):
    if status:
        print(status)

    audio_queue.put(bytes(indata))


def _confidence(result):
    """Average confidence across the recognised words, or None if absent."""
    words = result.get("result") or []

    scores = [
        word["conf"]
        for word in words
        if isinstance(word, dict) and "conf" in word
    ]

    if not scores:
        return None

    return sum(scores) / len(scores)


def _accept(text, result):
    """Decide whether a Vosk result is real speech or noise."""
    words = text.split()
    confidence = _confidence(result)

    if len(words) == 1 and words[0] in FILLERS:
        print(f'[filtered] "{text}" (single filler word)')
        return False

    if confidence is not None and confidence < MIN_CONFIDENCE:
        print(f'[filtered] "{text}" (confidence {confidence:.2f})')
        return False

    return True


def listen():
    print("Listening...")

    recognizer = KaldiRecognizer(model, SAMPLE_RATE)

    # Ask Vosk for per-word confidence scores so noise can be filtered.
    recognizer.SetWords(True)

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

                if text and _accept(text, result):
                    print(f"You said: {text}")
                    return text
