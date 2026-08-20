import json
import queue
import time
from difflib import SequenceMatcher

import sounddevice as sd
from vosk import KaldiRecognizer, Model

import transcriber
from speech import is_speaking, speech_epoch


MODEL_PATH = "model"
SAMPLE_RATE = transcriber.SAMPLE_RATE

# Audio is handed to the engine in blocks of this many samples. 4000 is a
# quarter of a second.
BLOCK_SIZE = 4000

# Set True to print how long transcription takes.
TIMING = False

# Reject a Vosk result below this average word confidence. The wake word is
# what guards against stray speech, so this only needs to catch outright
# noise. Whisper does not report confidence and is not filtered this way.
MIN_CONFIDENCE = 0.45

# Single short words that noise commonly decodes into. These are only
# rejected when they arrive ALONE -- "the" inside a real command is fine.
FILLERS = frozenset({
    "huh", "but", "a", "the", "oh", "uh", "um", "eh", "hm", "hmm",
    "and", "i", "it", "he", "she", "you", "to", "so", "no", "yeah",
    "yes", "what", "who", "that", "this", "of", "or", "on", "in",
})

# Set REQUIRE_WAKE_WORD to False to act on everything heard.
REQUIRE_WAKE_WORD = True

# Set to False to skip the spoken "Yes, sir?" acknowledgement.
ACKNOWLEDGE_WAKE = True

WAKE_WORD = "jarvis"

# A second recogniser restricted to this grammar does the real wake detection
# when the engine supports it.
WAKE_GRAMMAR = json.dumps([WAKE_WORD, "[unk]"])

WAKE_VARIANTS = frozenset({
    "jarvis", "jarvas", "jervis", "javis", "jarviss",
    "jarvace", "charvis", "jarv", "jarvis's", "jarvis.",
})

WAKE_RATIO = 0.75

# Used only once the wake word is already confirmed, so it can be looser.
WAKE_STRIP_RATIO = 0.45

WAKE_BLOCKLIST = frozenset({
    "travis", "java", "jarhead", "service", "harvest", "chris",
    "jarred", "carbis", "marvis", "javan",
})

# Once woken, JARVIS accepts a bare command for this many seconds.
ARMED_SECONDS = 10.0

# After acting he stays listening this long, so a follow-up needs no wake word.
FOLLOW_UP_SECONDS = 8.0

# Audio captured in this window after JARVIS speaks is discarded.
SETTLE_SECONDS = 0.35

audio_queue = queue.Queue()
model = Model(MODEL_PATH)

engine = transcriber.build_engine(model, BLOCK_SIZE)

print(f"[JARVIS] speech engine: {engine.name}")

_armed_until = 0.0
_wake_listener = None
_status_listener = None
_last_status = None


def set_wake_listener(listener):
    """Register a callable invoked when JARVIS is woken with no command."""
    global _wake_listener
    _wake_listener = listener


def set_status_listener(listener):
    """Register a callable taking "listening" or "standby".

    The listener is called whenever the state actually changes, including
    when a follow-up window expires while waiting, which the main loop cannot
    observe on its own.
    """
    global _status_listener
    _status_listener = listener


def _report_status():
    global _last_status

    status = "listening" if _armed() else "standby"

    if status == _last_status:
        return

    _last_status = status

    if _status_listener:
        try:
            _status_listener(status)
        except Exception as error:
            print(f"[JARVIS] status listener error: {error}")


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


def _make_wake_recognizer():
    """A recogniser that can only report the wake word, or None."""
    if not getattr(engine, "supports_grammar", False):
        return None

    try:
        return KaldiRecognizer(model, SAMPLE_RATE, WAKE_GRAMMAR)
    except Exception as error:
        print(f"[JARVIS] grammar wake detection unavailable ({error})")
        return None


def _accept(result):
    """Decide whether a transcription is real speech or noise."""
    text = result.text
    words = text.split()

    if len(words) == 1 and words[0] in FILLERS:
        print(f'[filtered] "{text}" (single filler word)')
        return False

    if result.confidence is not None and result.confidence < MIN_CONFIDENCE:
        print(f'[filtered] "{text}" (confidence {result.confidence:.2f})')
        return False

    return True


def _is_wake_token(token):
    token = token.strip(".,!?'")

    if token in WAKE_BLOCKLIST:
        return False

    if token in WAKE_VARIANTS:
        return True

    return SequenceMatcher(None, token, WAKE_WORD).ratio() >= WAKE_RATIO


def _split_wake(text):
    """Return (addressed, command) after removing the wake word."""
    tokens = text.split()

    for index, token in enumerate(tokens):
        if _is_wake_token(token):
            return True, " ".join(tokens[index + 1:]).strip()

    return False, ""


def _strip_wake(text):
    """Remove the wake word when grammar confirmed it but spelling differs."""
    addressed, remainder = _split_wake(text)

    if addressed:
        return remainder

    tokens = text.split()

    if len(tokens) < 2:
        return ""

    best = None
    best_score = 0.0

    for start in range(min(3, len(tokens))):
        for length in (2, 1):
            end = start + length

            if end > len(tokens):
                continue

            candidate = " ".join(tokens[start:end])
            score = SequenceMatcher(None, candidate, WAKE_WORD).ratio()

            if score > best_score or (
                score == best_score and best and length > best[1] - best[0]
            ):
                best_score = score
                best = (start, end)

    if best and best_score >= WAKE_STRIP_RATIO:
        start, end = best
        return " ".join(tokens[:start] + tokens[end:]).strip()

    return text


def _armed():
    return time.monotonic() < _armed_until


def _disarm():
    global _armed_until
    _armed_until = 0.0


def is_armed():
    """True while a bare follow-up command will be accepted."""
    return _armed()


def arm_follow_up(seconds=FOLLOW_UP_SECONDS):
    """Listen for a bare follow-up command without the wake word."""
    global _armed_until
    _armed_until = time.monotonic() + seconds


def _arm():
    global _armed_until
    _armed_until = time.monotonic() + ARMED_SECONDS


def listen():
    """Block until an addressed command is heard, then return it."""
    engine.reset()

    waker = _make_wake_recognizer() if REQUIRE_WAKE_WORD else None

    _drain_queue()
    _report_status()

    epoch = speech_epoch()
    wake_pending = False

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        while True:
            # A reminder can speak at any moment, from its own thread. Throw
            # away everything the microphone hears while that happens.
            if is_speaking():
                _drain_queue()
                time.sleep(0.05)
                continue

            if speech_epoch() != epoch:
                time.sleep(SETTLE_SECONDS)
                _drain_queue()
                engine.reset()

                if waker:
                    waker.Reset()

                wake_pending = False
                epoch = speech_epoch()
                continue

            try:
                data = audio_queue.get(timeout=0.25)
            except queue.Empty:
                # Nothing heard; the follow-up window may have just expired.
                _report_status()
                continue

            if waker and waker.AcceptWaveform(data):
                heard = json.loads(waker.Result()).get("text", "")

                if WAKE_WORD in heard.split():
                    wake_pending = True

            started = time.monotonic() if TIMING else None

            result = engine.feed(data)

            if result is None:
                continue

            if TIMING:
                print(
                    f"[timing] transcribed in {time.monotonic() - started:.2f}s")

            if not result.text or not _accept(result):
                wake_pending = False
                continue

            text = result.text

            if not REQUIRE_WAKE_WORD:
                print(f"You said: {text}")
                return text

            if _armed():
                _disarm()
                wake_pending = False

                addressed, remainder = _split_wake(text)
                command = remainder if addressed and remainder else text

                print(f"You said: {command}")
                _report_status()

                return command

            addressed, remainder = _split_wake(text)

            if wake_pending and not addressed:
                remainder = _strip_wake(text)
                addressed = True
                print(f'[wake] grammar matched on "{text}"')

            wake_pending = False

            if not addressed:
                print(f'[ignored] "{text}" (no wake word)')
                continue

            if remainder:
                print(f"You said: {remainder}")
                return remainder

            print("Woken. Awaiting command...")

            if ACKNOWLEDGE_WAKE and _wake_listener:
                try:
                    _wake_listener()
                except Exception as error:
                    print(f"[JARVIS] wake listener error: {error}")

                time.sleep(SETTLE_SECONDS)
                _drain_queue()
                engine.reset()

                if waker:
                    waker.Reset()

                epoch = speech_epoch()

            _arm()
            _report_status()
