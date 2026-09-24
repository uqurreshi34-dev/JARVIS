"""Thanks, praise and "you good?": things said to JARVIS rather than asked of him.

None of these is a request, so none of them is worth a model call, and
answering "thank you" with "I'm not equipped for that yet" is the wrong
kind of wrong. They are matched here, locally and for free, the same way
presence checks are: by shape rather than by a list of sentences.

A shape is a few interchangeable parts -- an optional lead-in ("great",
"excellent"), the heart of it ("thank you", "cheers", "much appreciated"),
an optional amount ("so much", "a lot"), an optional reason ("for that",
"for the help"), and an optional way of addressing him ("jarvis", "mate").
People improvise these every time; a fixed list would catch four and send
the fifth to a model that charges to say "you're welcome".

Every shape has to be the whole utterance. That is what keeps "thanks, now
open chrome", "how are you getting the data" and "no thanks" out: anything
more than the pleasantry itself is a command, and goes where commands go.
"""

import re

import phrases


# How JARVIS may be addressed at the end of any of these.
_ADDRESS = r"(?:\s+(?:jarvis|sir|mate|buddy|pal|man|bro|my\s+friend|my\s+man|old\s+friend))?"

# Words that often come first and change nothing: "great, thanks".
_LEAD = (
    r"(?:(?:great|excellent|brilliant|perfect|nice|lovely|awesome|cool|good|fantastic|"
    r"amazing|superb|wonderful|right|alright|ok|okay|oh|ah|aw|cheers|yeah|yes|so|well|um|uh|and|that\s+s\s+great|"
    r"thats\s+great|that\s+was\s+(?:great|quick|fast|helpful|perfect))\s+)*"
)

_THANKS = (
    r"(?:thank\s+(?:you|u|ya|yous)|thankyou|thanks|thanx|thx|ty|cheers|ta|"
    r"much\s+appreciated|(?:i\s+)?(?:really\s+)?appreciate\s+(?:it|that|this|you))"
)

_AMOUNT = r"(?:\s+(?:so|very)\s+much|\s+a\s+(?:lot|bunch|million|ton)|\s+loads|\s+again)?"

_REASON = (
    r"(?:\s+for\s+(?:that|this|it|everything|all\s+that|all\s+of\s+that|helping|"
    r"your\s+help|the\s+help|the\s+\w+|your\s+\w+))?"
)

_PRAISE = (
    r"(?:(?:good|great|nice|excellent|brilliant|awesome|top|amazing)\s+(?:job|work|one|stuff)|"
    r"well\s+done|nicely\s+done|nice\s+going|"
    r"(?:you\s+are|youre|you\s+re)\s+(?:the\s+best|brilliant|amazing|a\s+star|a\s+legend|a\s+genius|awesome)|"
    r"(?:what\s+a\s+)?legend|(?:what\s+a\s+)?star)"
)

_GRATITUDE = re.compile(
    rf"^{_LEAD}(?:{_PRAISE}{_ADDRESS}\s+)?{_THANKS}{_AMOUNT}{_REASON}{_ADDRESS}$"
)

_PRAISED = re.compile(rf"^{_LEAD}{_PRAISE}{_ADDRESS}$")

_WELLBEING = re.compile(
    r"^(?:"
    r"(?:are\s+)?(?:you|u)\s+(?:good|ok|okay|alright|all\s+right|well|doing\s+(?:ok|okay|alright|well|good|fine))"
    r"|how\s+(?:are|r)\s+(?:you|u)(?:\s+(?:doing|getting\s+on|holding\s+up|keeping|feeling))?"
    r"(?:\s+(?:today|tonight|this\s+(?:morning|afternoon|evening)))?"
    r"|how\s+(?:you|u)\s+doing"
    r"|how\s+(?:is|s)\s+it\s+going|hows\s+it\s+going"
    r"|how\s+are\s+things|hows\s+things|how\s+s\s+things"
    r"|whats\s+up|what\s+s\s+up|sup|wassup"
    r"|you\s+all\s+good|all\s+good"
    r")"
    rf"{_ADDRESS}$"
)


# Only the ways of getting his attention come off the front. The command
# router's normaliser also trims words like "well" and "okay" from the
# ends, which would turn "well done" into "done" and "are you okay" into
# "are you", so the raw utterance is normalised here instead.
_CALLED = re.compile(r"^(?:jarvis|hey|hi|oi|please)\s+")


def _normalise(said):
    text = str(said or "").casefold().replace("'", "").replace("\u2019", "")
    text = " ".join(re.sub(r"[^\w\s]", " ", text).split())

    previous = None

    while previous != text:
        previous = text
        text = _CALLED.sub("", text)

    return text


def kind(said):
    """'gratitude', 'praise', 'wellbeing', or None, for a raw utterance."""
    text = _normalise(said)

    if not text:
        return None

    if _GRATITUDE.match(text):
        return "gratitude"

    if _PRAISED.match(text):
        return "praise"

    if _WELLBEING.match(text):
        return "wellbeing"

    return None


def reply(which):
    """What JARVIS says back. Varied, and never the same line twice running."""
    if which == "gratitude":
        return phrases.pick("welcome")

    if which == "praise":
        return phrases.pick("praised")

    if which == "wellbeing":
        return _wellbeing()

    return None


def _wellbeing():
    """Fine, unless something honestly is not.

    A language model resting after a rate limit is the one thing JARVIS can
    notice about himself, so "you good?" gets the truth about it rather than
    a cheerful line that hides it.
    """
    try:
        import providers

        resting = [p.name for p in providers._pool if p.resting]
        total = len(providers._pool)
    except Exception:
        resting, total = [], 0

    if resting and total and len(resting) < total:
        return (
            "Mostly well, sir. One of my language models is resting after a "
            "busy spell, but the others are covering for it."
        )

    if resting and total:
        return (
            "A little short of breath, sir. My language models are all "
            "resting for a few minutes, so anything needing one will wait."
        )

    return phrases.pick("wellbeing")
