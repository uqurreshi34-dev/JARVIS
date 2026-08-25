"""Varied wording, so JARVIS does not answer identically every time.

Saying "Noted, sir." word for word on every note is the single thing that
most makes an assistant sound like a machine. Each entry below is a small
pool, and the same line is never used twice in a row.

Every variant is a fixed string so the speech cache can hold them all; the
ones taking a name use a format field rather than being built by hand.
"""

import random
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
        "Opening {name}, sir.",
        "Bringing up {name}, sir.",
        "{name}, coming up, sir.",
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
