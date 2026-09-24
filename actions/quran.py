"""Recitation, fetched once and then owned locally.

The catalogue of 114 surahs -- their names and how many verses each one
has -- is downloaded on first use and kept in the JARVIS folder. After
that every lookup, every name match and every bounds check happens with
no network at all, which is what makes "Al-Baqarah has 286 verses" an
answer JARVIS can give instantly rather than one he has to go and ask
about.

The same goes for a verse itself. Its audio is cached as an mp3 and its
Arabic and translation as a small JSON file beside it, so a verse that
has been heard once needs no request at all the next time -- text
included. A surah listened to twice costs the network nothing the second
time, and plays with no internet at all.

The text is kept beside the recitation rather than in one central place
so that nothing here has to assume two audio editions carry identical
Arabic. Switching reciter refetches both halves, which it was going to
do for the audio regardless.

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

# Which editions actually have verse-by-verse audio, learned by asking.
_RECITER_AUDIO_NAME = "quran-reciter-audio.json"

# The cheapest possible question: the first verse of the first surah.
# An edition that has no audio for that has none at all.
_PROBE = (1, 1)
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


def _support_path():
    root = _folder()

    return os.path.join(root, _RECITER_AUDIO_NAME) if root else None


def _load_support():
    """What is already known about which editions can be played."""
    path = _support_path()

    if not path or not os.path.exists(path):
        return {}

    try:
        with open(path, encoding="utf-8") as handle:
            known = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"[JARVIS] could not read reciter support: {error}")
        return {}

    return known if isinstance(known, dict) else {}


def _save_support(known):
    path = _support_path()

    if not path:
        return

    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(known, handle, indent=1, sort_keys=True)
    except OSError as error:
        print(f"[JARVIS] could not cache reciter support: {error}")


def _has_verse_audio(identifier):
    """Whether an edition offers audio for a single verse.

    True, False, or None when the question could not be put.

    The API lists 176 Arabic audio editions and only a minority have
    verse-by-verse files. The rest are whole-surah recordings and
    catalogue entries: they answer the text endpoint perfectly and come
    back with no audio url at all. Choosing one used to end a recitation
    on its first verse, because there was nothing to play and nothing
    else to try.

    No field distinguishes them, so the only honest test is to ask, and
    the answer is kept so it is only ever asked once per edition.
    """
    number, ayah = _PROBE
    url = f"{_API}/ayah/{number}:{ayah}/editions/{identifier}"

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

    except (urllib.error.URLError, ValueError, OSError):
        # Unreachable is not the same as unsupported. Returning None
        # leaves it unrecorded, so a dropped connection cannot blacklist
        # a perfectly good reciter for ever.
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    editions = data if isinstance(data, list) else [data]

    for edition in editions:
        if isinstance(edition, dict) and edition.get("audio"):
            return True

    return False


def reciters():
    """Arabic recitations that can actually be played, for the dropdown.

    Two filters, not one. Language, because most audio editions are
    translations read aloud in other languages; and whether the edition
    has verse-by-verse audio, because offering one that has not is
    offering a choice that cannot work.

    The second filter costs one request per unknown edition, once ever.
    Called from a background thread at startup, so the first run is the
    only slow one and nothing waits on it.
    """
    editions = _cached_json(_RECITERS_NAME, f"{_API}/edition/format/audio")

    if not editions:
        return []

    arabic = [
        {
            "identifier": edition.get("identifier"),
            "name": edition.get("englishName") or edition.get("name"),
        }
        for edition in editions
        if edition.get("language") == "ar" and edition.get("identifier")
    ]

    known = _load_support()
    learned = False
    playable = []

    for entry in arabic:
        identifier = entry["identifier"]
        state = known.get(identifier)

        if state is None:
            state = _has_verse_audio(identifier)

            if state is not None:
                known[identifier] = state
                learned = True

        if state:
            playable.append(entry)

    if learned:
        _save_support(known)

    # Nothing verified and nothing remembered means the network was down
    # throughout. An unchecked list is more use than an empty one, and
    # the default reciter is known good regardless.
    return playable if (playable or known) else arabic


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
    """Lower case, and nothing between the words but spaces.

    Whisper punctuates what it hears -- "recite, surah, too" -- and a
    trailing comma is enough to stop "surah" being recognised as the
    word a number follows. Dropping punctuation here fixes it once, for
    the slot reader and the pattern both.
    """
    text = (text or "").casefold()
    text = text.replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)

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


def _text_path(number, ayah, reciter):
    """Where a verse's Arabic and translation are kept: beside its mp3."""
    path = _audio_path(number, ayah, reciter)

    return f"{path[:-4]}.json" if path else None


def _read_text(path, translation):
    """Cached text for a verse, or None when it cannot be trusted."""
    if not path or not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None

    if not isinstance(cached, dict):
        return None

    # Which translation it was is recorded so that changing
    # DEFAULT_TRANSLATION refetches rather than quietly serving English
    # from the edition that is no longer wanted.
    return cached if cached.get("t") == translation else None


def _write_text(path, translation, arabic, english, audio=""):
    """Keep a verse's text so the next play needs no request.

    The audio url is kept with it. Without that, a verse whose text is
    cached but whose mp3 is not still has to ask the API where the sound
    lives, which would undo the whole point of fetching a surah's text
    in one go.
    """
    if not path or not (arabic or english):
        return

    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "t": translation,
                    "arabic": arabic,
                    "english": english,
                    "audio": audio,
                },
                handle,
                ensure_ascii=False,
            )
    except OSError as error:
        print(f"[JARVIS] could not cache verse text: {error}")


def cache_surah_text(number, reciter=DEFAULT_RECITER,
                     translation=DEFAULT_TRANSLATION):
    """Fetch a whole surah's text in one request. Returns verses written.

    Al-Baqarah played verse by verse asked the API 286 times for text it
    could have had in one answer. The audio cannot be batched -- those
    are separate files on the CDN and are still fetched as playback
    reaches them -- but the text can, and asking a free service 286
    times for one surah was never reasonable.

    Verses already cached are left alone, so this is cheap to call again
    and safe to call on a surah half heard.
    """
    url = f"{_API}/surah/{number}/editions/{reciter},{translation}"

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

    except (urllib.error.URLError, ValueError, OSError) as error:
        print(f"[JARVIS] could not fetch surah {number}: {error}")
        return 0

    data = payload.get("data") if isinstance(payload, dict) else None

    if not isinstance(data, list):
        return 0

    arabic = {}
    english = {}
    audio = {}

    for edition in data:
        if not isinstance(edition, dict):
            continue

        identifier = (edition.get("edition") or {}).get("identifier")

        for ayah in edition.get("ayahs") or []:
            if not isinstance(ayah, dict):
                continue

            position = ayah.get("numberInSurah")

            if not position:
                continue

            if identifier == reciter:
                arabic[position] = ayah.get("text") or ""
                audio[position] = ayah.get("audio") or ""

            elif identifier == translation:
                english[position] = ayah.get("text") or ""

    written = 0

    for position in sorted(arabic):
        path = _text_path(number, position, reciter)

        if not path or _read_text(path, translation):
            continue

        _write_text(
            path,
            translation,
            arabic[position],
            english.get(position, ""),
            audio.get(position, ""),
        )

        written += 1

    return written


def _from_cache(number, ayah, path, cached):
    """Whatever is already on disk, when the API cannot be relied on."""
    if not path or not os.path.exists(path):
        return None

    return {
        "surah": number,
        "ayah": ayah,
        "arabic": (cached or {}).get("arabic") or "",
        "english": (cached or {}).get("english") or "",
        "audio": path,
    }


def fetch_verse(number, ayah, reciter=DEFAULT_RECITER,
                translation=DEFAULT_TRANSLATION):
    """One verse: its Arabic, its translation, and a local mp3.

    Returns None when the verse cannot be had at all. Both halves are
    fetched once; afterwards this costs two file checks and a small
    read, with no request of any kind.
    """
    path = _audio_path(number, ayah, reciter)

    if not path:
        return None

    text_path = _text_path(number, ayah, reciter)
    cached = _read_text(text_path, translation)

    # Both halves present means the verse is entirely local. This is the
    # check that was missing: the request below used to fire on every
    # play, cached or not, so a surah heard twice still cost a call per
    # ayah and would not play at all without a connection.
    if cached and os.path.exists(path):
        return {
            "surah": number,
            "ayah": ayah,
            "arabic": cached.get("arabic") or "",
            "english": cached.get("english") or "",
            "audio": path,
        }

    # Text cached but no mp3 yet -- the usual state after a surah's text
    # has been fetched in one go. The url came with it, so the sound can
    # be had straight from the CDN without asking the API a second time.
    # Falls through to the request below if that download fails.
    if cached and cached.get("audio") and _download(cached["audio"], path):
        return {
            "surah": number,
            "ayah": ayah,
            "arabic": cached.get("arabic") or "",
            "english": cached.get("english") or "",
            "audio": path,
        }

    url = f"{_API}/ayah/{number}:{ayah}/editions/{reciter},{translation}"

    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

    except (urllib.error.URLError, ValueError, OSError) as error:
        print(f"[JARVIS] could not fetch {number}:{ayah}: {error}")

        # The text is gone, but a cached recitation is still playable.
        return _from_cache(number, ayah, path, cached)

    editions = payload.get("data")

    # Errors come back in the same envelope as success, with data as a
    # message string rather than a list of editions -- a rate limit
    # after a run of quick fetches is the usual way to see one. Reading
    # that string as editions raised inside the recitation thread and
    # killed the whole session, so the shape is checked rather than
    # assumed, and the code is logged so the next one is diagnosable.
    if not isinstance(editions, list):
        code = payload.get("code") or payload.get("status") or "something"
        print(f"[JARVIS] the Quran API answered {code} for {number}:{ayah}")

        return _from_cache(number, ayah, path, cached)

    arabic = ""
    english = ""
    audio_url = ""

    for edition in editions:
        if not isinstance(edition, dict):
            continue

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

    _write_text(text_path, translation, arabic, english, audio_url)

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


def _reference(number, ayah=None):
    """One parsed reference, in the shape both parsers return."""
    number = find_surah(number)

    if not number:
        return None

    if ayah is None:
        return {"surah": number, "ayah": 1, "whole": True}

    return {"surah": number, "ayah": int(ayah), "whole": False}


def parse_reference(command):
    """Which chapter and verse a command names, or None.

    No asking word and no Quran marker required. This is for the case
    where something else has already established that recitation is what
    was meant and the only question left is which verse -- which is
    exactly the situation when the model classifies the intent.

    That separation is what makes a misheard verb survive. Whisper
    writes 'reslight' or 'resight' for recite and 'gouran' for Quran,
    and it will invent new manglings tomorrow; none of them touch the
    reference, which is carried by a marker word and a number. Nothing
    here has to know how a word was mangled, so nothing here needs
    updating when it is mangled differently.
    """
    text = _digitise(_normalise(command))

    if not text:
        return None

    match = _REFERENCE.search(text)

    if match:
        return _reference(match.group("surah"), match.group("ayah"))

    # No marker word survived either -- 'resight 3 of the gouran'. The
    # intent is already settled, so bare numbers are the reference: one
    # is a chapter, two are a chapter and a verse. Three or more is
    # genuinely ambiguous and is declined rather than guessed at.
    numbers = re.findall(r"\b\d{1,3}\b", text)

    if len(numbers) == 1:
        return _reference(numbers[0])

    if len(numbers) == 2:
        return _reference(numbers[0], numbers[1])

    return None


def parse(command):
    """Understand a recitation request, or return None.

    Narrow on purpose, because this one guards the free path. It wants
    an asking word, something that marks the request as being about the
    Quran, and a surah number -- all three, or it declines and lets the
    usual routing have the command. parse_reference is the loose
    counterpart, for once the intent is no longer in question.
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

    return _reference(match.group("surah"), match.group("ayah"))


# Asking for the recitation to go away, read by shape rather than from a
# list of sentences -- the same way aircraft.dismissed reads "close the
# radar". Something that ends a thing, then the thing, a few words apart
# at most: "close the quran", "stop reciting", "end the recitation now".
#
# The book's own spellings come from BOOK_WORDS, so a spelling added
# there for asking is understood here for closing with no second edit.
_ENDS = (
    r"close|hide|dismiss|shut|stop|end|finish|exit|quit|cancel|kill"
    r"|get rid of|turn off|switch off|put away"
)

_RECITED = "|".join(sorted(
    {_normalise(word) for word in BOOK_WORDS}
    | {"surah", "sura", "recitation", "recitations", "reciting",
       "recital", "reciter"},
    key=len, reverse=True,
))

# Up to three words between the halves -- "stop playing the holy quran".
# Bounded, so a sentence that merely contains both, far apart, is not
# mistaken for an instruction to close anything.
_DISMISS = re.compile(
    rf"\b(?:{_ENDS})\b(?:\s+\S+){{0,3}}?\s+(?:{_RECITED})\b"
    # Or the verb split around it: "turn the quran off", "put it away".
    rf"|\b(?:turn|switch|shut|put)\b(?:\s+\S+){{0,3}}?\s+(?:{_RECITED})"
    rf"(?:\s+\S+){{0,2}}?\s+(?:off|away|down)\b"
)

# A question about the book is not an instruction to close it: "does the
# quran say to stop", "what does the recitation end with". Requests that
# happen to be phrased as questions -- "can you close the quran" -- start
# with a different word and are unaffected.
_QUESTION = re.compile(
    r"^(?:what|why|how|when|where|who|which|does|do|did|is|are|should)\b"
)


def dismissed(command):
    """Whether a command is asking for the recitation to stop and go."""
    text = _normalise(command)

    if not text or _QUESTION.match(text):
        return False

    return bool(_DISMISS.search(text))


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
