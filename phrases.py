"""Varied wording, so JARVIS does not answer identically every time.

Saying "Noted, sir." word for word on every note is the single thing that
most makes an assistant sound like a machine. Each entry below is a small
pool, and the same line is never used twice in a row.

Every variant is a fixed string so the speech cache can hold them all; the
ones taking a name use a format field rather than being built by hand.
"""

import random
import re
import threading


_lock = threading.Lock()
_last = {}


POOLS = {
    # Acknowledging something small that succeeded.
    "acknowledge": (
        "Noted, sir.",
        "Consider it handled, sir.",
        "Already seen to, sir.",
        "I've taken the liberty, sir.",
        "Very well, sir.",
        "Certainly, sir.",
        "Happy to, sir.",
    ),
    "done": (
        "That's dealt with, sir.",
        "Consider it done, sir.",
        "All done, sir.",
        "That's done, sir.",
        "All in order, sir."
    ),
    "saved": (
        "Saved, sir.",
        "Filed away, sir.",
        "Saved to your JARVIS folder, sir.",
    ),
    "copied": (
        "Copied, sir.",
        "On your clipboard, sir.",
        "Copied across, sir.",
    ),
    "added": (
        "Added, sir.",
        "Appended, sir.",
        "Added to the file, sir.",
    ),
    "wake": (
        "Yes, sir?",
        "Sir?",
        "At your service, sir.",
        "Listening, sir.",
    ),
    # Answering "are you there" rather than "what do you want". The wake
    # pool prompts for an instruction; this one reassures and asks for
    # nothing, because nothing was asked for.
    "presence": (
        "I'm here, sir.",
        "Always here, sir.",
        "For you, sir. Always.",
        "Awake and listening, sir.",
        "Never far, sir.",
        "Right here, sir.",
        "Here, sir. As ever.",
    ),
    # Said back to thanks. Nothing is asked in return: a question here
    # would reopen a conversation that the thanks was closing.
    "welcome": (
        "You're welcome, sir.",
        "My pleasure, sir.",
        "Any time, sir.",
        "Always a pleasure, sir.",
        "Happy to help, sir.",
        "Glad to be of use, sir.",
    ),
    "praised": (
        "Thank you, sir.",
        "Kind of you to say, sir.",
        "I do my best, sir.",
        "Much appreciated, sir.",
        "High praise, sir. Thank you.",
    ),
    # "You good?" when nothing is wrong. social.py says otherwise when
    # something honestly is.
    "wellbeing": (
        "All systems running smoothly, sir. Thank you for asking.",
        "Never better, sir.",
        "In fine form, sir.",
        "Very well, sir. Thank you for asking.",
        "Running like clockwork, sir.",
    ),
    "declined": (
        "Very well, sir. I'll leave it to you.",
        "As you wish, sir.",
        "Leaving it be, sir.",
        "As you prefer, sir.",
        "I'll leave it in your hands, sir."
    ),
    "cancelled": (
        "Cancelled, sir.",
        "Standing down, sir.",
        "Very well, sir.",
    ),
    "unknown": (
        "That's beyond me for now, sir.",
        "I'm not equipped for that yet, sir.",
    ),
    "failed": (
        "That didn't work, sir.",
        "I'm afraid that failed, sir.",
        "No luck, sir.",
        "I'm afraid that eluded me, sir.",
        "That didn't take, sir. Might I try again?",
    ),
    "cannot_open": (
        "I couldn't open that, sir.",
        "That wouldn't open, sir.",
    ),
    "cannot_close": (
        "I couldn't close that, sir.",
        "That wouldn't close, sir.",
    ),
    "cannot_find": (
        "I couldn't find that out, sir.",
        "I wasn't able to find that, sir.",
    ),
    "wrong": (
        "Something went wrong, sir.",
        "Something's amiss, sir.",
    ),
    # These take a {name} field.
    "opening": (
        "Launching {name}, sir.",
    ),
    "closing": (
        "Closing {name}, sir.",
        "Shutting {name}, sir.",
        "Closing {name} now, sir.",
    ),
    "creating": (
        "Creating {name}, sir.",
        "Making {name}, sir.",
    ),
}


_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def number(value):
    """A small number as a word, for speech.

    The voice reads a bare "4" as something close to "for", which is
    confusing in a sentence like "4 words changed". Larger numbers read
    correctly as digits, so only the small ones are spelled out.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        return str(value)

    if 0 <= count < len(_NUMBER_WORDS):
        return _NUMBER_WORDS[count]

    return str(count)


# The radio alphabet. A callsign is a string of characters, not a word,
# and a speech engine given LOG9LB will helpfully read LB as pounds.
# Spelling it the way it is actually said on the radio removes the
# ambiguity completely and happens to sound the part.
_PHONETIC = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta",
    "E": "Echo", "F": "Foxtrot", "G": "Golf", "H": "Hotel",
    "I": "India", "J": "Juliet", "K": "Kilo", "L": "Lima",
    "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa",
    "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray",
    "Y": "Yankee", "Z": "Zulu",
}


# Digits as the radio says them, where that earns its keep.
#
# ICAO also gives TREE for three and FIFE for five, and they are correct
# procedure -- but they exist for non-native speakers on noisy HF, not
# for a neural voice in a quiet room. Neither three nor five is
# confusable with anything here, and both read as a glitch to anyone who
# does not know the table.
#
# Fower and niner stay because they solve collisions that are real in
# this room: four against for, which is what sent us looking, and nine
# against five over any imperfect channel.
#
# Only used for spelled codes; ordinary prose still counts normally.
_RADIO_DIGITS = (
    "zero", "one", "two", "three", "fower",
    "five", "six", "seven", "eight", "niner",
)


# Spoken numbers, built compositionally rather than listed. Whisper
# writes "50" as often as "fifty" and there is no telling which, so
# anything reading a number out of speech has to cope with both.
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}

_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def digits(text):
    """Spoken numbers turned into numerals, everything else untouched.

    Handles the shapes people actually say -- forty, forty five, a
    hundred, one hundred and twenty -- by adding the parts up rather
    than matching whole phrases, so combinations nobody listed still
    work.
    """
    words = (text or "").casefold().split()

    out = []
    total = 0
    running = False

    def flush():
        nonlocal total, running

        if running:
            out.append(str(total))

        total = 0
        running = False

    for word in words:
        bare = word.strip(".,;:!?")

        if bare in _ONES:
            total += _ONES[bare]
            running = True
        elif bare in _TENS:
            total += _TENS[bare]
            running = True
        elif bare == "hundred":
            # "a hundred" has no number in front of it, so hundred on
            # its own still counts as one of them.
            total = max(1, total) * 100
            running = True
        elif bare in ("a", "and") and running:
            continue
        else:
            flush()
            out.append(word)

    flush()

    return " ".join(out)


def spell(text, phonetic=True):
    """A code said character by character, so it is heard as one.

    Anything that is not a letter or a digit becomes a pause, which is
    what turns G-CLBN into Golf, Charlie Lima Bravo November rather than
    into a word the engine tries to pronounce.

    phonetic=False spells with the bare letters instead, which is
    shorter and still unambiguous.
    """
    if not text:
        return ""

    said = []

    for character in str(text).upper():
        if character.isdigit():
            said.append(_RADIO_DIGITS[int(character)] if phonetic
                        else number(int(character)))
        elif character.isalpha():
            said.append(_PHONETIC.get(character, character)
                        if phonetic else character)
        elif said and said[-1] != ",":
            said.append(",")

    return " ".join(said).replace(" ,", ",")


def pick(key, **fields):
    """A line from the pool, never the same one twice running."""
    pool = POOLS.get(key)

    if not pool:
        return ""

    with _lock:
        previous = _last.get(key)

        if len(pool) > 1:
            choices = [line for line in pool if line != previous]
        else:
            choices = list(pool)

        chosen = random.choice(choices)
        _last[key] = chosen

    return chosen.format(**fields) if fields else chosen


def every_fixed_line():
    """Every variant with no format fields, for warming the speech cache."""
    lines = []

    for pool in POOLS.values():
        for line in pool:
            if "{" not in line:
                lines.append(line)

    return tuple(lines)


# ---- plain words: what a model writes, as it should be said and shown ------

# Models write for a screen: **bold**, *emphasis*, `code`, headings, bullets.
# Read aloud, the voice says "asterisk"; shown on the HUD, the marks are
# clutter. Only paired marks hugging a word are taken, so "5 * 3", a file
# called my_notes.txt, and a lone underscore or asterisk are left alone.
_MARKDOWN = (
    (re.compile(r"\[([^\]\n]+)\]\((?:https?://|mailto:)[^)\s]+\)"), r"\1"),   # [words](link)
    # **bold** only: "__bold__" is how Python names are written (__init__.py).
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"\1"),                         # **bold**
    (re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])"), r"\1"),  # *emphasis*
    (re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])"), r"\1"),     # _emphasis_
    (re.compile(r"`([^`\n]+)`"), r"\1"),                                           # `code`
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE), ""),                          # # heading
    (re.compile(r"^\s{0,3}>\s?", re.MULTILINE), ""),                               # > quote
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),                               # - bullet
)


def plain(text):
    """[text] without the marks a model uses for formatting."""
    if not isinstance(text, str) or not text:
        return text

    for pattern, replacement in _MARKDOWN:
        text = pattern.sub(replacement, text)

    return text
