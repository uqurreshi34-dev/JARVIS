"""Recitation, fetched once and then owned locally.

The catalogue of 114 surahs -- their names and how many verses each one
has -- is downloaded on first use and kept in the JARVIS folder. After
that every lookup, every name match and every bounds check happens with
no network at all, which is what makes "Al-Baqarah has 286 verses" an
answer JARVIS can give instantly rather than one he has to go and ask
about.

The same goes for the audio. An ayah is fetched once and cached as an
mp3; hearing it again is a local file read. A surah listened to twice
costs the network nothing the second time.

Nothing here decides how anything is said. It returns a path and the
text; speech.py owns playback, which is what keeps the stop button, the
speaking state and the HUD waveform working without this module knowing
they exist.

Bismillah is not treated as a verse. The API already numbers it that
way -- Al-Baqarah is 286 verses, and verse 1 is where Alif Lam Meem is
-- but the Arabic of verse 1 carries the Bismillah in front of it, so
strip_opening() is offered for callers that would rather display the
verse alone. At-Tawbah has no Bismillah at all, which that function
handles by finding nothing to strip.
"""

import json
import os
import re
import threading
import urllib.error
import urllib.request

from actions import files


_API = "https://api.alquran.cloud/v1"

# Audio bitrate offered by the CDN. 128 is the usual choice; the files
# are small enough that a whole surah caches in a few megabytes.
_BITRATE = 128

DEFAULT_RECITER = "ar.alafasy"

# Saheeh International, for the translation under the Arabic.
DEFAULT_TRANSLATION = "en.sahih"

_CATALOGUE_NAME = "quran-surahs.json"
_RECITERS_NAME = "quran-reciters.json"
_AUDIO_DIR = "quran-audio"

_TIMEOUT = 20

# The Bismillah as the API prefixes it to the first verse of every surah
# but Al-Fatiha, where it is the verse, and At-Tawbah, where there is
# none. Matched loosely on the letters so that differences in vowel
# marks between editions do not defeat it.
_BISMILLAH = re.compile(
    r"^\s*بِ?سْ?مِ?\s*ٱ?للَّ?هِ?\s*ٱ?لرَّ?حْ?مَ?ٰ?نِ?\s*ٱ?لرَّ?حِ?يمِ?\s*"
)

# Spoken forms that all mean the same book. Kept here rather than in the
# router so the router only has to ask this module.
BOOK_WORDS = ("quran", "qur'an", "quraan", "koran", "kuran", "qoran")

_lock = threading.Lock()
_catalogue = None


def _folder():
    root = files.root()

    return root


def _cached_json(name, url, key=None):
    """Read a cached JSON document, fetching it once if it is not there."""
    root = _folder()

    if not root:
        return None

    path = os.path.join(root, name)

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[JARVIS] could not read {name}: {error}")

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

    except (urllib.error.URLError, ValueError, OSError) as error:
        print(f"[JARVIS] could not reach the Quran API: {error}")
        return None

    data = payload.get("data") if isinstance(payload, dict) else None

    if key:
        data = data.get(key) if isinstance(data, dict) else None

    if not data:
        return None

    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
    except OSError as error:
        print(f"[JARVIS] could not cache {name}: {error}")

    return data


def catalogue():
    """All 114 surahs: number, names, and how many verses each has."""
    global _catalogue

    with _lock:
        if _catalogue is None:
            _catalogue = _cached_json(_CATALOGUE_NAME, f"{_API}/surah") or []

    return _catalogue


def reciters():
    """Arabic recitations only, for the HUD's dropdown.

    The API offers 189 audio editions, but most are translations read
    aloud in other languages. Someone choosing a reciter wants the ones
    reciting the Arabic.
    """
    editions = _cached_json(_RECITERS_NAME, f"{_API}/edition/format/audio")

    if not editions:
        return []

    return [
        {
            "identifier": edition.get("identifier"),
            "name": edition.get("englishName") or edition.get("name"),
        }
        for edition in editions
        if edition.get("language") == "ar" and edition.get("identifier")
    ]


def surah(number):
    """One surah's catalogue entry, or None."""
    for entry in catalogue():
        if entry.get("number") == number:
            return entry

    return None


def verse_count(number):
    """How many verses a surah has, or 0 when it is not known."""
    entry = surah(number)

    return int(entry.get("numberOfAyahs", 0)) if entry else 0


def _normalise(text):
    text = (text or "").casefold()
    text = text.replace("'", "").replace("-", " ").replace("_", " ")

    return re.sub(r"\s+", " ", text).strip()


def find_surah(text):
    """A surah number from a spoken reference, or None.

    Numbers only, deliberately. Transliterated names arrive from speech
    recognition in too many spellings to match reliably, and a misheard
    name becoming a confidently wrong surah is worse than not
    understanding at all. The catalogue still carries the names, for
    display.
    """
    text = _normalise(text)

    if not text.isdigit():
        return None

    number = int(text)

    return number if 1 <= number <= 114 else None


def strip_opening(arabic):
    """The verse without the Bismillah the API prefixes to it.

    Al-Fatiha is left alone: there the Bismillah is the verse rather
    than an opening to it. At-Tawbah has none, so nothing matches.
    """
    return _BISMILLAH.sub("", arabic or "").strip()


def _audio_path(number, ayah, reciter):
    root = _folder()

    if not root:
        return None

    folder = os.path.join(root, _AUDIO_DIR, reciter)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not make the recitation cache: {error}")
        return None

    return os.path.join(folder, f"{number:03d}{ayah:03d}.mp3")


def fetch_verse(number, ayah, reciter=DEFAULT_RECITER,
                translation=DEFAULT_TRANSLATION):
    """One verse: its Arabic, its translation, and a local mp3.

    Returns None when the verse cannot be had at all. The audio is
    downloaded once; afterwards this costs one file check.
    """
    path = _audio_path(number, ayah, reciter)

    if not path:
        return None

    url = f"{_API}/ayah/{number}:{ayah}/editions/{reciter},{translation}"

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

    except (urllib.error.URLError, ValueError, OSError) as error:
        print(f"[JARVIS] could not fetch {number}:{ayah}: {error}")

        # The text is gone, but a cached recitation is still playable.
        if os.path.exists(path):
            return {
                "surah": number,
                "ayah": ayah,
                "arabic": "",
                "english": "",
                "audio": path,
            }

        return None

    editions = payload.get("data") or []

    arabic = ""
    english = ""
    audio_url = ""

    for edition in editions:
        identifier = (edition.get("edition") or {}).get("identifier")

        if identifier == reciter:
            arabic = edition.get("text") or ""
            audio_url = edition.get("audio") or ""

        elif identifier == translation:
            english = edition.get("text") or ""

    if not os.path.exists(path) and audio_url:
        if not _download(audio_url, path):
            return None

    if not os.path.exists(path):
        return None

    return {
        "surah": number,
        "ayah": ayah,
        "arabic": arabic,
        "english": english,
        "audio": path,
    }


def _download(url, path):
    """Fetch an mp3 to a temporary name, then move it into place.

    Written aside first so an interrupted download cannot leave a
    half-file that later looks cached and plays as a click.
    """
    partial = f"{path}.part"

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            data = response.read()

        if not data:
            return False

        with open(partial, "wb") as handle:
            handle.write(data)

        os.replace(partial, path)

        return True

    except (urllib.error.URLError, OSError) as error:
        print(f"[JARVIS] could not download recitation: {error}")

        try:
            if os.path.exists(partial):
                os.remove(partial)
        except OSError:
            pass

        return False


# "surah 2 verse 255", "chapter 36", "sura 112 ayah 1". The verse is
# optional: without one the whole surah is meant.
_REFERENCE = re.compile(
    r"\b(?:surah|sura|chapter)\s*(?P<surah>\d{1,3})"
    r"(?:\s*[, ]\s*(?:ayah|aya|ayat|verse|v)\s*(?P<ayah>\d{1,3}))?"
)

_ASKED = re.compile(r"\b(recite|play|read)\b")


# Spoken numbers, because speech recognition writes them as words. The
# list stops at ninety-nine and relies on "hundred" for the rest, which
# covers every surah (114) and every verse (286) without enumerating
# anything.
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}

_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}

# What Whisper writes instead. These are real words in their own right --
# "to" and "for" above all -- which is why they are only ever consulted
# in the slot straight after "surah" or "verse", never anywhere else in
# the sentence. "play the video" is untouched; "sura too" is not.
_MISHEARD = {
    "to": 2, "too": 2, "tu": 2,
    "for": 4, "fore": 4, "faw": 4,
    "won": 1, "wun": 1,
    "ate": 8, "ait": 8,
    "tree": 3, "free": 3, "thre": 3,
    "sicks": 6, "sax": 6,
    "nain": 9,
    "fife": 5,
    "tan": 10,
}

_NUMBER_WORDS = set(_ONES) | set(_TENS) | set(_MISHEARD) | {"hundred", "and"}

# The words a number can follow.
_SLOTS = frozenset({
    "surah", "sura", "chapter", "ayah", "aya", "ayat", "verse", "v",
})


def _run_value(words):
    """The number a run of number-words adds up to, or None.

    Reads them the way they are said: hundreds multiply what came just
    before, tens and units add. "one hundred and fourteen" is 114,
    "thirty six" is 36, and a bare "and" is ignored rather than ending
    the run.
    """
    total = 0
    current = 0
    seen = False

    for word in words:
        if word == "and":
            continue

        if word == "hundred":
            current = (current or 1) * 100
            seen = True
            continue

        value = _ONES.get(word)

        if value is None:
            value = _TENS.get(word)

        if value is None:
            value = _MISHEARD.get(word)

        if value is None:
            return None

        current += value
        seen = True

    total += current

    return total if seen else None


def _digitise(text):
    """Turn spoken numbers into digits, but only where one belongs.

    Restricted to the words straight after "surah", "chapter", "verse"
    and their kin. That restriction is the whole point: "to" and "for"
    are ordinary English everywhere else, and rewriting them wherever
    they appeared would turn "play the video for me" into nonsense.
    """
    words = text.split()
    out = []
    index = 0

    while index < len(words):
        word = words[index]
        out.append(word)
        index += 1

        if word not in _SLOTS:
            continue

        # Gather the run of number-words that follows, stopping at the
        # next slot word so "chapter two verse four" keeps its two
        # numbers apart.
        run = []

        while index < len(words) and words[index] in _NUMBER_WORDS:
            if words[index] in _SLOTS:
                break

            run.append(words[index])
            index += 1

        if not run:
            continue

        value = _run_value(run)

        if value is None:
            out.extend(run)
            continue

        out.append(str(value))

    return " ".join(out)


def parse(command):
    """Understand a recitation request, or return None.

    Narrow on purpose. It wants an asking word, something that marks the
    request as being about the Quran, and a surah number -- all three,
    or it declines. Anything it does not recognise falls through to the
    usual routing rather than being guessed at.
    """
    text = _normalise(command)

    if not text or not _ASKED.search(text):
        return None

    # "surah" and "sura" are specific enough to qualify on their own;
    # "chapter" is not, which is why it is absent here. Without this,
    # "play chapter 3" of anything at all would become recitation.
    marked = (
        any(word in text for word in BOOK_WORDS)
        or re.search(r"\b(surah|sura)\b", text)
    )

    if not marked:
        return None

    match = _REFERENCE.search(_digitise(text))

    if not match:
        return None

    number = find_surah(match.group("surah"))

    if not number:
        return None

    ayah = match.group("ayah")

    if ayah is None:
        return {"surah": number, "ayah": 1, "whole": True}

    return {"surah": number, "ayah": int(ayah), "whole": False}


def bounds_message(number, ayah):
    """Why a verse number will not do, or None when it is fine."""
    total = verse_count(number)

    if not total:
        return None

    if 1 <= ayah <= total:
        return None

    entry = surah(number) or {}
    name = entry.get("englishName") or f"surah {number}"

    return (
        f"{name} has {total} verses, sir. "
        f"Pick one between 1 and {total}."
    )
