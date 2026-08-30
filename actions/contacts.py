"""People you can reach, and the phone actions that reach them.

Numbers live in `contacts.txt` in the JARVIS folder, written by hand:

    mum: +447700900123
    dad: +447700900456
    work: +441214960000

Speech only ever matches a name against that file. A number is never
assembled from anything spoken, so a mishearing can at worst pick the
wrong person off your own list -- it can never dial something you did
not write down. Same principle as the task runner, and for the same
reason: transcription is imperfect and a phone number is unforgiving.

None of this can happen on the PC. A desk has no dialler and no
WhatsApp, so these actions produce a link the phone opens, and at the
desk they say so rather than appearing to fail.

Deliberately not journalled. The journal records things that changed
something; offering to open a dialler changes nothing, and a log of who
you were about to ring is a record worth not keeping.
"""

import os
import re
from difflib import SequenceMatcher

from actions import files, safety


FILENAME = "contacts.txt"

# A number as it must be stored: international, digits only after the
# plus. Anything else is refused when read rather than dialled wrongly.
_NUMBER = re.compile(r"^\+\d{7,15}$")

_LINE = re.compile(r"^\s*([^:]{1,40}?)\s*:\s*(.+)$")

# Names already complained about, so a malformed entry is mentioned
# once rather than on every lookup.
_warned = set()

_VOWELS = re.compile(r"[aeiou]")
_DOUBLES = re.compile(r"(.)\1+")

# Taken as certain: dial without asking.
#
# Measured rather than guessed, and the measurement showed a real
# limit. Genuine mishearings of a short name ("done" for "dad") score
# about 0.63, and so does "murk" against "work" -- a word that has
# nothing to do with any contact. No single threshold separates them,
# because at three letters there simply isn't enough signal.
#
# So this sits above that band, and the band below it asks instead of
# assuming. Being asked "did you mean Dad?" costs a word; ringing the
# wrong person costs rather more.
CERTAIN_THRESHOLD = 0.72

# Worth offering, not worth assuming.
SUGGEST_THRESHOLD = 0.55


def _path():
    base = files.root()

    return os.path.join(base, FILENAME) if base else None


def contacts():
    """Everyone in the file, as {name: number}.

    A malformed number is skipped with a warning rather than silently
    kept: a number that cannot be dialled is worse than an absent one,
    because it fails at the moment you needed it.
    """
    path = _path()

    if not path or not os.path.exists(path):
        return {}

    found = {}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                match = _LINE.match(line)

                if not match:
                    continue

                name = match.group(1).strip().casefold()
                number = match.group(2).strip().replace(" ", "")

                if not _NUMBER.match(number):
                    # Warned once, not on every read: contacts are
                    # looked up on each command, and a repeating
                    # complaint would bury everything else.
                    if name not in _warned:
                        _warned.add(name)
                        print(
                            f"[JARVIS] ignoring {name!r} in "
                            f"contacts.txt: {number!r} is not "
                            "+<country><number>"
                        )
                    continue

                found[name] = number

    except OSError as error:
        print(f"[JARVIS] could not read contacts: {error}")

    return found


def _skeleton(word):
    """A word's consonant skeleton, ignoring vowels and repeats.

    The same trick voice.py uses to accept "jovis" and "java's" as the
    wake word: what survives a mishearing is usually the consonants.
    """
    return _DOUBLES.sub(r"\1", _VOWELS.sub("", word.casefold()))


def _closeness(heard, name):
    """How alike two names sound, from 0 to 1.

    Compared both as written and as consonant skeletons, because a
    mishearing mangles vowels far more readily than consonants. The
    skeleton is weighted slightly lower so an exact spelling still
    wins when both match.
    """
    written = SequenceMatcher(None, heard, name).ratio()
    sounded = SequenceMatcher(
        None, _skeleton(heard), _skeleton(name)
    ).ratio()

    return max(written, sounded * 0.95)


def find(spoken):
    """(name, number) for a spoken name, or (None, None).

    Matched only against names already in contacts.txt. Exact first,
    then containment, then by how alike they sound -- which is what
    lets "dan", "dat", "done" and "daddy" all reach Dad without any of
    them being written down as special cases. A list of known
    mishearings would only ever describe one person's voice on one
    phone; comparing sounds works for a contact added tomorrow.

    Nothing here can invent a contact. Every route returns a name that
    is already in the file, or nothing at all.
    """
    wanted = (spoken or "").strip().casefold()

    if not wanted:
        return None, None

    known = contacts()

    if not known:
        return None, None

    if wanted in known:
        return wanted, known[wanted]

    # Longest first, so "mum mobile" wins over "mum" when both exist.
    for name in sorted(known, key=len, reverse=True):
        if name in wanted or wanted in name:
            return name, known[name]

    best_name = None
    best_score = 0.0

    for name in known:
        score = _closeness(wanted, name)

        if score > best_score:
            best_name, best_score = name, score

    if best_score >= CERTAIN_THRESHOLD:
        return best_name, known[best_name]

    return None, None


def close_matches(spoken, limit=3):
    """Contacts that sound somewhat like this, best first.

    For asking "did you mean Dad?" rather than refusing outright --
    a lower bar than find() uses, because a suggestion the user has to
    confirm can afford to be wrong.
    """
    wanted = (spoken or "").strip().casefold()

    if not wanted:
        return []

    scored = [
        (_closeness(wanted, name), name)
        for name in contacts()
    ]

    scored = [
        (score, name) for score, name in scored
        if score >= SUGGEST_THRESHOLD
    ]
    scored.sort(reverse=True)

    return [name for _, name in scored[:limit]]


def names():
    """Everyone known, for saying aloud."""
    return sorted(contacts())


def describe():
    """A spoken list of who JARVIS can reach."""
    known = names()

    if not known:
        return (
            "I don't have any contacts, sir. Add them to contacts.txt "
            "in your JARVIS folder, as name colon plus country code."
        )

    if len(known) == 1:
        return f"One contact, sir: {known[0]}."

    listed = ", ".join(known[:-1]) + f", and {known[-1]}"

    return f"{len(known)} contacts, sir: {listed}."


# --- the actions themselves --------------------------------------------

def call_link(number):
    """A link that opens the dialler with the number ready."""
    return f"tel:{number}"


def whatsapp_link(number):
    """A link that opens the WhatsApp conversation.

    wa.me wants the number without its plus, and falls back to the web
    version when WhatsApp isn't installed, which is friendlier than an
    intent that silently does nothing.
    """
    return f"https://wa.me/{number.lstrip('+')}"


def sms_link(number, message=None):
    """A link that opens a text, optionally already written."""
    if not message:
        return f"sms:{number}"

    # Encoded, because a message is the one part of this that can
    # contain anything at all.
    from urllib.parse import quote

    return f"sms:{number}?body={quote(safety.clean(message, 300))}"


ACTIONS = {
    "call": ("Calling", call_link),
    "whatsapp": ("WhatsApp", whatsapp_link),
    "text": ("Texting", sms_link),
}


def prepare(action, spoken_name, message=None):
    """Work out what the phone should open. Returns a dict.

    Always returns something sayable. The caller decides whether the
    device asking can actually act on it -- a desk cannot, and should
    say so rather than appearing to have done something.
    """
    if action not in ACTIONS:
        return {"ok": False, "spoken": "I can't do that, sir."}

    verb, build = ACTIONS[action]

    name, number = find(spoken_name)

    if not number:
        known = names()

        if not known:
            return {
                "ok": False,
                "spoken": (
                    "I don't have any contacts yet, sir. Add them to "
                    "contacts.txt in your JARVIS folder."
                ),
            }

        return {
            "ok": False,
            "spoken": (
                f"I don't have {safety.clean(spoken_name, 40)} in your "
                "contacts, sir."
            ),
        }

    link = build(number, message) if action == "text" else build(number)

    return {
        "ok": True,
        "name": name,
        "link": link,
        "spoken": f"{verb} {name}, sir.",
    }


def desk_reply(action, spoken_name):
    """What to say when this is asked at the desk.

    The PC has no dialler and no WhatsApp. Saying so plainly beats
    failing in a way that looks like a fault.
    """
    name, number = find(spoken_name)

    if not number and spoken_name:
        return (
            f"I don't have {safety.clean(spoken_name, 40)} in your "
            "contacts, sir."
        )

    # Phrased per action rather than from the verb, which produced
    # "I can't be whatsapp dad from here".
    wording = {
        "call": f"call {name}",
        "whatsapp": f"open WhatsApp for {name}",
        "text": f"text {name}",
    }.get(action, "do that")

    return (
        f"I can't {wording} from here, sir. Ask me from your phone "
        "and I'll open it."
    )
