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

# Names people use that are not the catalogue's spelling. Deliberately
# short: the catalogue's own englishName and englishNameTranslation
# already cover most of it, and a long list of guesses would be a long
# list of things to be wrong about.
_ALIASES = {
    "fatiha": 1,
    "fatihah": 1,
    "the opening": 1,
    "baqarah": 2,
    "baqara": 2,
    "the cow": 2,
    "imran": 3,
    "yaseen": 36,
    "yasin": 36,
    "ya sin": 36,
    "rahman": 55,
    "waqiah": 56,
    "mulk": 67,
    "kahf": 18,
    "ikhlas": 112,
    "falaq": 113,
    "nas": 114,
}

# Verses with names of their own.
NAMED_VERSES = {
    "ayat al kursi": (2, 255),
    "ayatul kursi": (2, 255),
    "the throne verse": (2, 255),
}

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


def _key(text):
    """A spelling-insensitive form for comparing transliterations.

    Editions disagree about Faatiha and Fatiha, Baqara and Baqarah,
    Yaseen and Yasin. Dropping a leading "al", collapsing doubled
    vowels and dropping a trailing h makes those the same string,
    which is cheaper and more predictable than fuzzy distance.
    """
    text = _normalise(text)
    text = re.sub(r"^al\s+", "", text)
    text = re.sub(r"([aeiou])\1+", r"\1", text)
    text = re.sub(r"h\b", "", text)

    return re.sub(r"[^a-z0-9]", "", text)


def find_surah(text):
    """Resolve a spoken surah reference to its number, or None.

    Tries, in order: a plain number, a known alias, and then the
    catalogue's own names -- the transliteration, the English
    translation, and the Arabic.
    """
    text = _normalise(text)

    if not text:
        return None

    if text.isdigit():
        number = int(text)

        return number if 1 <= number <= 114 else None

    if text in _ALIASES:
        return _ALIASES[text]

    for entry in catalogue():
        names = (
            _normalise(entry.get("englishName")),
            _normalise(entry.get("englishNameTranslation")),
            _normalise(entry.get("name")),
        )

        if text in names:
            return entry.get("number")

    # Spelling-insensitive, tried only after the exact names have failed
    # so a real name is never beaten by a near miss on another.
    wanted = _key(text)

    if len(wanted) < 3:
        return None

    for entry in catalogue():
        keys = (
            _key(entry.get("englishName")),
            _key(entry.get("englishNameTranslation")),
        )

        if wanted in keys:
            return entry.get("number")

    return None


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


_REFERENCE = re.compile(
    r"(?:surah|sura|chapter)?\s*"
    r"(?P<surah>[a-z' \-]+?|\d{1,3})\s*"
    r"(?:,|\s)\s*"
    r"(?:ayah|aya|ayat|verse|v)\s*"
    r"(?P<ayah>\d{1,3})\s*$"
)


def parse(command):
    """Understand a recitation request, or return None.

    Deliberately local and deliberately narrow. Anything this does not
    recognise falls through to the usual routing rather than being
    guessed at -- a misheard surah number should not become a
    confidently wrong recitation.
    """
    text = _normalise(command)

    if not text:
        return None

    asked = any(word in text for word in ("recite", "read", "play"))
    book = any(word in text for word in BOOK_WORDS)

    for name, (number, ayah) in NAMED_VERSES.items():
        if name in text:
            return {"surah": number, "ayah": ayah, "whole": False}

    if not asked and not book:
        return None

    # Strip the framing so what is left is the reference itself.
    body = re.sub(
        r"\b(recite|read|play|from|the|me|please|of|in|to)\b", " ", text
    )

    # Only after the reference pattern has had its chance, since it uses
    # "surah" and "chapter" as anchors itself.
    stripped = re.sub(r"\b(surah|sura|chapter)\b", " ", body)

    for word in BOOK_WORDS:
        body = body.replace(word, " ")

    body = re.sub(r"\s+", " ", body).strip()

    match = _REFERENCE.match(body)

    if match:
        number = find_surah(match.group("surah"))

        if number:
            return {
                "surah": number,
                "ayah": int(match.group("ayah")),
                "whole": False,
            }

    number = find_surah(re.sub(r"\s+", " ", stripped).strip())

    if number:
        return {"surah": number, "ayah": 1, "whole": True}

    return None


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
