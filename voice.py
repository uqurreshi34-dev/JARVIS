import json
import queue
import time
from difflib import SequenceMatcher

import sounddevice as sd
from vosk import KaldiRecognizer, Model


MODEL_PATH = "model"
SAMPLE_RATE = 16000

# Reject a result when Vosk's own average word confidence is below this.
MIN_CONFIDENCE = 0.70

# Single short words that noise commonly decodes into. These are only
# rejected when they arrive ALONE -- "the" inside a real command is fine.
FILLERS = frozenset({
    "huh", "but", "a", "the", "oh", "uh", "um", "eh", "hm", "hmm",
    "and", "i", "it", "he", "she", "you", "to", "so", "no", "yeah",
    "yes", "what", "who", "that", "this", "of", "or", "on", "in",
})

# Set REQUIRE_WAKE_WORD to False to go back to acting on everything heard.
REQUIRE_WAKE_WORD = True

WAKE_WORD = "jarvis"

# Vosk mishears the name in predictable ways; accept the close ones.
WAKE_VARIANTS = frozenset({
    "jarvis", "jarvas", "jervis", "javis", "jarviss",
    "jarvace", "charvis", "jarv", "jarvis's",
})

# Fuzzy threshold for anything not in the variant list above.
WAKE_RATIO = 0.75

# Once woken, JARVIS accepts a bare command for this many seconds.
ARMED_SECONDS = 10.0

# Audio captured in this window after JARVIS speaks is discarded, so he
# never mistakes his own voice for a command.
SETTLE_SECONDS = 0.35

audio_queue = queue.Queue()
model = Model(MODEL_PATH)

_armed_until = 0.0
_wake_listener = None


def set_wake_listener(listener):
    """Register a callable invoked when JARVIS is woken with no command."""
    global _wake_listener
    _wake_listener = listener


def audio_callback(indata, frames, time_info, status):
    if status:
        print(status)

    audio_queue.put(bytes(indata))


def _drain_queue():
    """Discard any buffered audio, e.g. JARVIS hearing his own voice."""
    dropped = 0

    while True:
        try:
            audio_queue.get_nowait()
            dropped += 1
        except queue.Empty:
            break

    return dropped


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


def _is_wake_token(token):
    token = token.strip(".,!?'")

    if token in WAKE_VARIANTS:
        return True

    return SequenceMatcher(None, token, WAKE_WORD).ratio() >= WAKE_RATIO


def _split_wake(text):
    """Return (addressed, command) after removing the wake word."""
    tokens = text.split()

    for index, token in enumerate(tokens):
        if _is_wake_token(token):
            remainder = " ".join(tokens[index + 1:]).strip()
            return True, remainder

    return False, ""


def _armed():
    return time.monotonic() < _armed_until


def _arm():
    global _armed_until
    _armed_until = time.monotonic() + ARMED_SECONDS


def _disarm():
    global _armed_until
    _armed_until = 0.0


def listen():
    """Block until an addressed command is heard, then return it."""
    if REQUIRE_WAKE_WORD:
        print("Waiting for wake word...")
    else:
        print("Listening...")

    recognizer = KaldiRecognizer(model, SAMPLE_RATE)

    # Ask Vosk for per-word confidence scores so noise can be filtered.
    recognizer.SetWords(True)

    # Anything buffered from before this call is stale -- typically JARVIS's
    # own replies from the previous turn.
    _drain_queue()

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=8000,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        while True:
            data = audio_queue.get()

            if not recognizer.AcceptWaveform(data):
                continue

            result = json.loads(recognizer.Result())
            text = result.get("text", "").strip()

            if not text or not _accept(text, result):
                continue

            if not REQUIRE_WAKE_WORD:
                print(f"You said: {text}")
                return text

            # Already woken: treat whatever we hear as the command.
            if _armed():
                _disarm()

                addressed, remainder = _split_wake(text)
                command = remainder if addressed and remainder else text

                print(f"You said: {command}")
                return command

            addressed, remainder = _split_wake(text)

            if not addressed:
                print(f'[ignored] "{text}" (no wake word)')
                continue

            # "Jarvis, open chrome" -- command came in the same breath.
            if remainder:
                print(f"You said: {remainder}")
                return remainder

            # Bare "Jarvis" -- wake up and wait for the command.
            print("Woken. Awaiting command...")

            if _wake_listener:
                try:
                    _wake_listener()
                except Exception as error:
                    print(f"[JARVIS] wake listener error: {error}")

            # The mic was live while JARVIS spoke, so throw away everything
            # captured during the acknowledgement plus a short settle window.
            time.sleep(SETTLE_SECONDS)
            _drain_queue()
            recognizer.Reset()

            # Start the armed window only once JARVIS has finished speaking.
            _arm()
