import json
import queue
import time
from difflib import SequenceMatcher

import sounddevice as sd
from vosk import KaldiRecognizer, Model

from speech import is_speaking, speech_epoch


MODEL_PATH = "model"
SAMPLE_RATE = 16000

# Audio is handed to Vosk in blocks of this many samples. 8000 is half a
# second, which coarsens endpoint detection; 4000 is a quarter second and
# makes JARVIS notice you have stopped talking sooner.
BLOCK_SIZE = 4000

# Endpointing: how long a silence ends an utterance. Vosk's default waits
# noticeably longer, which is the single biggest source of lag.
ENDPOINT_SILENCE = 0.6
ENDPOINT_START_MAX = 3.0
ENDPOINT_MAX = 20.0

# Many Vosk builds expose no endpointer controls. When the partial transcript
# stops changing for this long, finalise it ourselves. Audio is still fed to
# Vosk continuously; only the decision to close the utterance is ours.
#
# The wait scales with how much has been said: one word is very likely the
# start of something longer ("clear..." before "my clipboard"), so it gets
# more patience, while a full phrase can be closed quickly.
FORCE_ENDPOINT_SILENCE = 0.45
SHORT_UTTERANCE_SILENCE = 1.0
PARTIAL_UTTERANCE_SILENCE = 0.7


def _endpoint_delay(partial):
    """How long to wait before closing an utterance of this length."""
    words = len(partial.split())

    # One or two words is very often the start of something longer
    # ("clear my..." before "notes"), so wait properly.
    if words <= 2:
        return SHORT_UTTERANCE_SILENCE

    if words == 3:
        return PARTIAL_UTTERANCE_SILENCE

    return FORCE_ENDPOINT_SILENCE


# Set True to print how long Vosk takes to finalise an utterance.
TIMING = False

# Reject a result when Vosk's own average word confidence is below this.
MIN_CONFIDENCE = 0.70

# Single short words that noise commonly decodes into. These are only
# rejected when they arrive ALONE -- "the" inside a real command is fine.
FILLERS = frozenset({
    "huh", "but", "a", "the", "oh", "uh", "um", "eh", "hm", "hmm",
    "and", "i", "it", "he", "she", "you", "to", "so", "no", "yeah",
    "yes", "what", "who", "that", "this", "of", "or", "on", "in",
})

# Set REQUIRE_WAKE_WORD to False to act on everything heard.
REQUIRE_WAKE_WORD = True

# Set to False to skip the spoken "Yes, sir?" acknowledgement, which is the
# slowest part of the two-stage flow. One-breath commands are unaffected.
ACKNOWLEDGE_WAKE = True

WAKE_WORD = "jarvis"

# A second recogniser restricted to this grammar does the real wake detection.
# When Vosk may only answer "jarvis" or "[unk]", near-misses like "job is"
# resolve to the wake word instead of an unrelated common word.
WAKE_GRAMMAR = json.dumps([WAKE_WORD, "[unk]"])

# Kept as a fallback for when grammar mode is unavailable, and to strip the
# name out of a one-breath command.
WAKE_VARIANTS = frozenset({
    "jarvis", "jarvas", "jervis", "javis", "jarviss",
    "jarvace", "charvis", "jarv", "jarvis's", "jarvis.",
})

WAKE_RATIO = 0.75

# Used only once grammar mode has already confirmed the name was spoken, so
# it can be far looser: "job is" scores 0.50, while a real word like
# "dentist" scores 0.31 and must survive.
WAKE_STRIP_RATIO = 0.45

# Words close enough to trip the fuzzy test but clearly not the wake word.
WAKE_BLOCKLIST = frozenset({
    "travis", "java", "jarhead", "service", "harvest", "chris",
    "jarred", "carbis", "marvis", "javan",
})

# Once woken, JARVIS accepts a bare command for this many seconds.
ARMED_SECONDS = 10.0

# Audio captured in this window after JARVIS speaks is discarded, so he
# never mistakes his own voice for a command.
SETTLE_SECONDS = 0.35

audio_queue = queue.Queue()
model = Model(MODEL_PATH)

_armed_until = 0.0
_wake_listener = None

# Set once we know whether this model supports grammar-constrained recognition.
_grammar_supported = None


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


def _make_wake_recognizer():
    """A recogniser that can only report the wake word, or None."""
    global _grammar_supported

    if _grammar_supported is False:
        return None

    try:
        recognizer = KaldiRecognizer(model, SAMPLE_RATE, WAKE_GRAMMAR)
        _grammar_supported = True
        return recognizer

    except Exception as error:
        if _grammar_supported is None:
            print(
                f"[JARVIS] grammar wake detection unavailable ({error}); "
                "falling back to fuzzy matching"
            )

        _grammar_supported = False

        return None


def _tune_endpointing(recognizer):
    """Shorten Vosk's end-of-speech delay, where the build supports it."""
    try:
        recognizer.SetEndpointerDelays(
            ENDPOINT_START_MAX, ENDPOINT_SILENCE, ENDPOINT_MAX
        )
        return True

    except Exception:
        # Older Vosk builds have no endpointer controls; the defaults apply.
        return False


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
    """Remove the wake word when the grammar recogniser found one.

    The full recogniser often transcribes the name as something else, and
    sometimes as two words ("job is"). Since grammar has already confirmed
    the name was said, a loose match is safe here -- but only loose enough
    to catch a mangled name, never a real word like "dentist".
    """
    addressed, remainder = _split_wake(text)

    if addressed:
        return remainder

    tokens = text.split()

    if len(tokens) < 2:
        return ""

    best = None
    best_score = 0.0

    # The name leads the sentence, so only look near the start.
    for start in range(min(3, len(tokens))):
        for length in (2, 1):
            end = start + length

            if end > len(tokens):
                continue

            candidate = " ".join(tokens[start:end])
            score = SequenceMatcher(None, candidate, WAKE_WORD).ratio()

            # Prefer the better match, and a two-word span when tied, since
            # "job is" beats "is" for the same score.
            if score > best_score or (
                score == best_score and best and length > best[1] - best[0]
            ):
                best_score = score
                best = (start, end)

    if best and best_score >= WAKE_STRIP_RATIO:
        start, end = best
        return " ".join(tokens[:start] + tokens[end:]).strip()

    # Nothing resembled the name, so the whole utterance is the command.
    return text


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
    recognizer.SetWords(True)

    tuned = _tune_endpointing(recognizer)

    waker = _make_wake_recognizer() if REQUIRE_WAKE_WORD else None

    if waker:
        _tune_endpointing(waker)

    if TIMING and not tuned:
        print("[timing] this Vosk build has no endpointer controls")

    # Anything buffered from before this call is stale -- typically JARVIS's
    # own replies from the previous turn.
    _drain_queue()

    epoch = speech_epoch()
    wake_pending = False
    speech_started = None
    last_partial = ""
    last_change = time.monotonic()

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
                recognizer.Reset()

                if waker:
                    waker.Reset()

                wake_pending = False
                last_partial = ""
                last_change = time.monotonic()
                speech_started = None
                epoch = speech_epoch()
                continue

            data = audio_queue.get()

            # The grammar recogniser sees the same audio and is the primary
            # wake signal, being far harder to confuse than fuzzy matching.
            if waker and waker.AcceptWaveform(data):
                heard = json.loads(waker.Result()).get("text", "")

                if WAKE_WORD in heard.split():
                    wake_pending = True

            final = None

            if recognizer.AcceptWaveform(data):
                final = json.loads(recognizer.Result())

            else:
                partial = json.loads(
                    recognizer.PartialResult()
                ).get("partial", "").strip()

                if partial != last_partial:
                    last_partial = partial
                    last_change = time.monotonic()

                    if partial and speech_started is None:
                        speech_started = time.monotonic()

                elif partial and (
                    time.monotonic() - last_change >= _endpoint_delay(partial)
                ):
                    # Vosk has stopped changing its mind, so close the
                    # utterance rather than waiting for its own endpointer.
                    final = json.loads(recognizer.Result())

                    if TIMING:
                        print("[timing] forced endpoint after silence")

            if final is None:
                continue

            last_partial = ""
            last_change = time.monotonic()

            if TIMING and speech_started is not None:
                print(
                    f"[timing] vosk finalised "
                    f"{time.monotonic() - speech_started:.2f}s after speech began"
                )

            speech_started = None

            result = final
            text = result.get("text", "").strip()

            if not text or not _accept(text, result):
                wake_pending = False
                continue

            if not REQUIRE_WAKE_WORD:
                print(f"You said: {text}")
                return text

            # Already woken: treat whatever we hear as the command.
            if _armed():
                _disarm()
                wake_pending = False

                addressed, remainder = _split_wake(text)
                command = remainder if addressed and remainder else text

                print(f"You said: {command}")
                return command

            addressed, remainder = _split_wake(text)

            if wake_pending and not addressed:
                # Grammar caught the name even though the full transcription
                # rendered it as something else.
                remainder = _strip_wake(text)
                addressed = True
                print(f'[wake] grammar matched on "{text}"')

            wake_pending = False

            if not addressed:
                print(f'[ignored] "{text}" (no wake word)')
                continue

            # "Jarvis, open chrome" -- command came in the same breath.
            if remainder:
                print(f"You said: {remainder}")
                return remainder

            # Bare "Jarvis" -- wake up and wait for the command.
            print("Woken. Awaiting command...")

            if ACKNOWLEDGE_WAKE and _wake_listener:
                try:
                    _wake_listener()
                except Exception as error:
                    print(f"[JARVIS] wake listener error: {error}")

                time.sleep(SETTLE_SECONDS)
                _drain_queue()
                recognizer.Reset()

                if waker:
                    waker.Reset()

                epoch = speech_epoch()

            _arm()
