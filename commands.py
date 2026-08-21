import re
import webbrowser
from difflib import SequenceMatcher
from urllib.parse import urlparse

from actions.applications import ApplicationManager
from actions.desktop import (
    describe_volume,
    minimise_all,
    next_track,
    play_pause,
    previous_track,
    restore_all,
    set_mute,
    set_volume,
    toggle_mute,
    volume_down,
    volume_up,
)
from actions.knowledge import answer
from actions.projects import ProjectManager
from actions.reminders import ReminderManager, describe_duration, to_seconds
from actions import clipboard, files, notes
from actions.screen import describe_capture
from actions.system import describe_system, describe_time, describe_weather
from llm import CommandInterpreter


_application_manager = ApplicationManager()
_project_manager = ProjectManager()
reminder_manager = ReminderManager()
_interpreter = CommandInterpreter()

# Every command sends these to the LLM, so keeping the list short directly
# reduces token use. Recent projects are the ones people actually ask for.
_PROJECT_CANDIDATES = 8


def _open_website(url):
    return bool(webbrowser.open(url))


def _website_label(url):
    host = urlparse(url).netloc or url

    if host.startswith("www."):
        host = host[4:]

    return host


def _action(intent, response, action):
    """A command that does something; JARVIS confirms when it succeeds."""
    return {
        "kind": "action",
        "intent": intent,
        "response": response,
        "action": action,
    }


def _query(intent, action):
    """A command that finds something out; the action returns what to say."""
    return {
        "kind": "query",
        "intent": intent,
        "response": None,
        "action": action,
    }


# Intents that simply run a function and need no argument. Each entry is
# the spoken confirmation and the function to call.
_SIMPLE_ACTIONS = {
    "volume_up": ("Turning it up, sir.", volume_up),
    "volume_down": ("Turning it down, sir.", volume_down),
    "toggle_mute": ("Toggling mute, sir.", toggle_mute),
    "mute": ("Muting, sir.", lambda: set_mute(True)),
    "unmute": ("Unmuting, sir.", lambda: set_mute(False)),
    "media_play_pause": ("Certainly, sir.", play_pause),
    "media_next": ("Skipping ahead, sir.", next_track),
    "media_previous": ("Going back, sir.", previous_track),
    "minimise_all": ("Clearing the desktop, sir.", minimise_all),
    "restore_all": ("Bringing them back, sir.", restore_all),
}


def _to_number(value):
    """Coerce an LLM-supplied amount to a float, or None.

    The model sometimes sends the string "null" rather than JSON null, or a
    number as text, so this accepts both and rejects anything unusable.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().rstrip("%").strip()

    if text.casefold() in ("", "null", "none", "nil"):
        return None

    try:
        return float(text)
    except ValueError:
        return None


# Commands matched here never reach the LLM. That removes roughly half a
# second of latency and, just as usefully, spends no API tokens at all.
_FAST_PHRASES = (
    (("what time is it", "whats the time", "what is the time",
      "the time", "time please"), "get_time"),
    (("whats the weather", "what is the weather", "the weather",
      "hows the weather", "whats the weather like",
      "what is the weather like", "the forecast"), "get_weather"),
    (("take a screenshot", "screenshot", "capture my screen",
      "take a screen shot", "grab my screen"), "take_screenshot"),
    (("mute", "mute it", "silence", "be quiet"), "mute"),
    (("unmute", "un mute", "unmute it", "sound on"), "unmute"),
    (("turn it up", "volume up", "louder", "turn the volume up"),
     "volume_up"),
    (("turn it down", "volume down", "quieter", "turn the volume down"),
     "volume_down"),
    (("how loud is it", "whats the volume", "what is the volume"),
     "get_volume"),
    (("minimise everything", "minimize everything", "show the desktop",
      "clear the desktop", "minimise all", "minimize all"), "minimise_all"),
    (("bring my windows back", "restore my windows", "bring them back",
      "restore everything"), "restore_all"),
    (("hows my system", "how is my system", "system status",
      "hows my system doing", "how is my system doing", "hows my pc",
      "how is my pc", "system report", "hows my computer",
      "how is my computer", "how is my machine", "hows my machine",
      "how is my laptop", "hows my laptop", "system stats",
      "how is my memory", "how much memory am i using",
      "how much battery do i have", "whats my battery"),
     "get_system_status"),
    (("whats on my clipboard", "what is on my clipboard",
      "read my clipboard", "check my clipboard"), "read_clipboard"),
    (("list my files", "what files do i have", "show me my files",
      "how many files do i have", "whats in my folder",
      "what is in my jarvis folder", "whats in my jarvis folder"),
     "count_files"),
    (("name my files", "read my files", "read out my files",
      "what are my files called", "what are they called"), "list_files"),
    (("read my notes", "what are my notes", "read back my notes",
      "whats on my notes", "check my notes", "my notes",
      "whats in my notes", "what is in my notes", "whats in my notes file",
      "what is in my notes file", "show me my notes", "list my notes",
      "read out my notes"), "read_notes"),
    (("clear my notes", "delete my notes", "wipe my notes",
      "clear all my notes"), "clear_notes"),
    (("clear my clipboard", "empty my clipboard", "clear the clipboard",
      "empty the clipboard", "wipe my clipboard", "clear clipboard"),
     "clear_clipboard"),
    (("pause", "play", "pause the video", "resume the video",
      "pause the music", "resume the music", "play the video",
      "pause it", "resume it"), "media_play_pause"),
    (("next track", "skip this track", "skip this song", "next song"),
     "media_next"),
    (("previous track", "last track", "go back a track"), "media_previous"),
    (("what timers are running", "what reminders do i have",
      "list my timers", "list my reminders", "whats pending"),
     "list_reminders"),
    (("cancel my reminders", "cancel my timers", "cancel everything"),
     "cancel_reminders"),
)

_FAST_LOOKUP = {
    phrase: intent
    for phrases, intent in _FAST_PHRASES
    for phrase in phrases
}

# Speech recognition mangles words ("notes" becomes "know"), so a close
# match still counts.
_FUZZY_THRESHOLD = 0.78

# These delete something, and near misses are dangerous: "read my clipboard"
# and "clear my clipboard" score 0.86 against each other. They must be said
# clearly enough to match exactly.
_NEVER_FUZZY = frozenset({
    "clear_clipboard",
    "clear_notes",
    "cancel_reminders",
})


def _fuzzy_intent(text):
    """Find a fast-path intent for a near miss, or None."""
    if len(text) < 6:
        return None

    best_score = 0.0
    best_intent = None

    for phrase, intent in _FAST_LOOKUP.items():
        if intent in _NEVER_FUZZY:
            continue

        # Length filter first; SequenceMatcher on every phrase is wasteful.
        if abs(len(phrase) - len(text)) > 6:
            continue

        score = SequenceMatcher(None, text, phrase).ratio()

        if score > best_score:
            best_score = score
            best_intent = intent

    if best_score >= _FUZZY_THRESHOLD:
        return best_intent

    return None


_OPEN_PREFIXES = ("open ", "launch ", "start ", "run ")
_CLOSE_PREFIXES = (
    "close ", "quit ", "exit ", "shut ",
    "im done with ", "i am done with ", "done with ",
    # Whisper merges "with outlook" into "without luck", so the split form
    # is handled and the leading "out" put back on the app name.
    "im done without ", "i am done without ", "done without ",
)

# Applied only after an exact application lookup has failed, and only inside
# an explicit open/close phrase. Real app names score 0.71 and above against
# this list; unrelated phrases top out around 0.70.
_APP_FUZZY_THRESHOLD = 0.71


def _resolve_app(target):
    """Find an application, tolerating mangled speech. Returns a name or None."""
    app = _application_manager.find(target)

    if app:
        return app.name

    squashed = target.replace(" ", "")
    best_name = None
    best_score = 0.0

    for name in _application_manager.applications:
        lowered = name.casefold()

        score = max(
            SequenceMatcher(None, target, lowered).ratio(),
            SequenceMatcher(None, squashed, lowered.replace(" ", "")).ratio(),
        )

        if score > best_score:
            best_score = score
            best_name = name

    if best_score >= _APP_FUZZY_THRESHOLD:
        return best_name

    return None


_LEADING_NOISE = re.compile(
    r"^(jarvis|please|hey|ok|okay|could you|can you|would you)\s+"
)

# Vosk frequently tacks these onto an utterance ("the open chrome the"), which
# would otherwise stop an exact phrase from matching. Deliberately narrow:
# words like "it" and "is" carry meaning ("what time is it") and must stay.
_EDGE_FILLER = frozenset({
    "the", "a", "an", "and", "so", "um", "uh", "er", "eh", "well",
    "just", "now", "then", "like", "yeah", "okay",
})


def _trim_filler(text):
    """Drop filler words from both ends, keeping the meaningful middle."""
    tokens = text.split()

    while tokens and tokens[0] in _EDGE_FILLER:
        tokens.pop(0)

    while tokens and tokens[-1] in _EDGE_FILLER:
        tokens.pop()

    return " ".join(tokens)


def _normalise(command):
    """Lowercase, strip punctuation and pleasantries, collapse spaces."""
    text = (command or "").casefold()

    # Remove apostrophes outright so "what's" matches "whats", rather than
    # becoming "what s" and missing every phrase.
    text = text.replace("'", "").replace("\u2019", "")

    text = re.sub(r"[^\w\s]", " ", text)
    text = " ".join(text.split())

    previous = None

    while previous != text:
        previous = text
        text = _LEADING_NOISE.sub("", text).strip()

    return _trim_filler(text)


_VOLUME_WORDS = {
    "half": 50, "full": 100, "max": 100, "maximum": 100,
    "all the way up": 100, "zero": 0, "nothing": 0,
    "a quarter": 25, "three quarters": 75,
}

_VOLUME_PATTERN = re.compile(
    r"^(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+|at\s+)?(.+)$"
)

# Sites people ask for by name. Anything not here still goes to the LLM, so
# this is a shortcut rather than a limit on what JARVIS can open.
_WEBSITES = {
    "youtube": "https://www.youtube.com",
    "you tube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "google maps": "https://maps.google.com",
    "maps": "https://maps.google.com",
    "github": "https://github.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.co.uk",
    "reddit": "https://www.reddit.com",
    "bbc": "https://www.bbc.co.uk",
    "bbc news": "https://www.bbc.co.uk/news",
    "sky sports": "https://www.skysports.com",
    "skysports": "https://www.skysports.com",
    "linkedin": "https://www.linkedin.com",
    "spotify": "https://open.spotify.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "whatsapp": "https://web.whatsapp.com",
    "chat gpt": "https://chat.openai.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "stack overflow": "https://stackoverflow.com",
    "wikipedia": "https://www.wikipedia.org",
}


def _volume_level(text):
    """Extract a target volume percentage from a phrase, or None."""
    match = _VOLUME_PATTERN.match(text)

    if not match:
        return None

    tail = match.group(1).strip()
    tail = tail.removesuffix(" percent").removesuffix("%").strip()

    if tail in _VOLUME_WORDS:
        return _VOLUME_WORDS[tail]

    digits = re.fullmatch(r"(\d{1,3})", tail)

    if digits:
        return max(0, min(100, int(digits.group(1))))

    return None


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "a": 1, "an": 1, "half": 0,
}

_UNIT_TO_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}

# The count must be digits or number words only. Allowing any words here
# makes the pattern swallow the message ("to call that in ten seconds").
_NUMBER_TOKEN = (
    r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|an?)"
)

_DURATION = re.compile(
    rf"\b({_NUMBER_TOKEN}(?:\s+{_NUMBER_TOKEN})*)\s+"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b"
)

_TIMER_PATTERNS = (
    re.compile(r"^(?:set\s+)?(?:a\s+)?timer\s+(.+)$"),
    re.compile(r"^remind\s+me\s+(.+)$"),
)


def _spoken_number(text):
    """Turn '30' or 'thirty five' into a number, or None."""
    text = text.strip()

    if not text:
        return None

    digits = re.fullmatch(r"\d+", text)

    if digits:
        return int(text)

    total = 0
    matched = False

    for word in text.split():
        if word not in _NUMBER_WORDS:
            return None

        value = _NUMBER_WORDS[word]

        # "one hundred" multiplies rather than adds.
        if value == 100 and total:
            total *= 100
        else:
            total += value

        matched = True

    return total if matched else None


_CONNECTORS = ("in", "for", "after", "to", "and", "me")


def _strip_connectors(text):
    """Remove joining words left behind when the duration is cut out."""
    tokens = text.split()

    while tokens and tokens[0] in _CONNECTORS:
        tokens.pop(0)

    while tokens and tokens[-1] in _CONNECTORS:
        tokens.pop()

    return " ".join(tokens)


def _parse_duration(text):
    """Return (seconds, message) from a timer phrase, or (None, None).

    The duration may come before the message ("in ten seconds to stretch") or
    after it ("to call mum in ten seconds"); both are handled.
    """
    match = _DURATION.search(text)

    if not match:
        return None, None

    count = _spoken_number(match.group(1))
    unit = _UNIT_TO_SECONDS.get(match.group(2))

    if count is None or not unit or count <= 0:
        return None, None

    seconds = count * unit

    if not 1 <= seconds <= 24 * 60 * 60:
        return None, None

    # Whatever sits either side of the duration is the message.
    before = _strip_connectors(text[:match.start()].strip())
    after = _strip_connectors(text[match.end():].strip())

    message = " ".join(part for part in (before, after) if part).strip()

    return seconds, message or None


# Speech recognition routinely confuses short function words: "add" becomes
# "at", "and" or "had". The trailing "to my notes" makes the intent clear
# regardless, so the leading verb is treated loosely.
_ADD_VERBS = r"(?:add|at|and|had|put|save|stick|pop)"

# "to", "onto", "into" and the mishearings "too"/"two" all appear here.
_TO_WORDS = r"(?:to|onto|into|on|in|too|two)"

_COPY_PATTERNS = (
    # "copy hello world to my clipboard" / "add hello world onto the clipboard"
    re.compile(rf"^(?:copy|{_ADD_VERBS})\s+(.+?)\s+{_TO_WORDS}\s+"
               r"(?:my|the)?\s*clipboard$"),
    # "copy hello world"
    re.compile(r"^copy\s+(.+)$"),
)

# Used to trim a trailing destination the fallback pattern would otherwise
# capture as part of the text.
_TRAILING_CLIPBOARD = re.compile(rf"\s+{_TO_WORDS}\s+(?:my|the)?\s*clipboard$")


_NOTE_PATTERNS = (
    re.compile(r"^(?:make|take|write|add|at|and|jot)\s+(?:me\s+)?a\s+note\s+"
               r"(?:that\s+|saying\s+|about\s+|to\s+)?(.+)$"),
    # "add dentist appointment to my notes", including "at ... to my notes"
    re.compile(rf"^{_ADD_VERBS}\s+(.+?)\s+{_TO_WORDS}\s+"
               r"(?:my\s+|the\s+)?notes$"),
    re.compile(r"^note\s+(?:that\s+|down\s+)?(.+)$"),
    re.compile(r"^remember\s+(?:that\s+)?(.+)$"),
    # The leading verb is sometimes lost entirely ("and" is stripped as
    # filler), but "... to my notes" still says exactly what is wanted.
    # Questions are excluded: "what's in my notes" is a request to read them.
    re.compile(r"^(?!what|whats|which|show|read|check|list|clear|delete|"
               rf"empty|wipe)(.+?)\s+{_TO_WORDS}\s+(?:my|the)\s+notes$"),
)


def _note_request(text):
    """Extract a note to save, or None."""
    for pattern in _NOTE_PATTERNS:
        match = pattern.match(text)

        if not match:
            continue

        note = match.group(1).strip()

        if note and note not in ("this", "that", "it"):
            return note

    return None


def _copy_request(text):
    """Extract text to place on the clipboard, or None."""
    for pattern in _COPY_PATTERNS:
        match = pattern.match(text)

        if not match:
            continue

        payload = match.group(1).strip()

        # The broad "copy ..." pattern can swallow the destination, so remove
        # it if it is still attached.
        payload = _TRAILING_CLIPBOARD.sub("", payload).strip()

        # "copy that" and similar need context only the LLM might infer.
        if not payload or payload in ("this", "that", "it", "clipboard"):
            continue

        return payload

    return None


def _timer_request(text):
    """Resolve a timer or reminder locally, or return None."""
    for pattern in _TIMER_PATTERNS:
        match = pattern.match(text)

        if not match:
            continue

        seconds, message = _parse_duration(match.group(1))

        if seconds:
            return seconds, message

    return None


def _original_case(command, payload):
    """Recover the user's original casing, since the matched text is lowercased."""
    original = (command or "").strip()
    start = original.casefold().find(payload)

    if start == -1:
        return payload

    return original[start:start + len(payload)]


_FILE_FORMAT = r"(?:a\s+|an\s+)?(text|txt|plain text|markdown|md|word|word document|word doc|doc|docx|pdf|pdf document|csv|spreadsheet)\s*(?:file|document)?"

_CREATE_FILE = re.compile(
    rf"^(?:create|make|new|start)\s+{_FILE_FORMAT}\s+"
    r"(?:called|named|for)\s+(.+)$"
)

_CREATE_FILE_PLAIN = re.compile(
    r"^(?:create|make|new|start)\s+(?:a\s+|an\s+)?file\s+"
    r"(?:called|named|for)\s+(.+)$"
)

_ADD_TO_FILE = re.compile(
    rf"^{_ADD_VERBS}\s+(.+?)\s+{_TO_WORDS}\s+(?:my\s+|the\s+)?(.+?)\s+file$"
)


# Reading and copying are gated on the file existing, so "read my notes" and
# "open chrome" can never be mistaken for a file request. The word "file"
# makes it explicit; without it, the name must match something on disk.
_READ_FILE_EXPLICIT = re.compile(
    r"^(?:read|show me|open|display)\s+(?:me\s+)?(?:my|the)?\s*"
    r"(?:file\s+)?(?:called\s+|named\s+)?(.+?)(?:\s+file)?$"
)

_COPY_FILE = re.compile(
    r"^(?:copy|duplicate|back up|backup)\s+(?:my|the)?\s*"
    r"(?:file\s+)?(.+?)(?:\s+file)?"
    r"(?:\s+(?:to|as|into)\s+(?:a\s+)?(?:file\s+)?(?:called\s+|named\s+)?(.+?))?$"
)

# Never treated as filenames, since they belong to other skills.
_NOT_FILENAMES = frozenset({
    "notes", "note", "clipboard", "files", "file", "my notes",
    "my clipboard", "my files", "them", "it", "this", "that",
})


def _file_target(name):
    """A filename that exists in the folder, or None."""
    name = (name or "").strip()

    if not name or name in _NOT_FILENAMES:
        return None

    if files.exists(name):
        return name

    # Spoken names lose their extension, so try the known formats too.
    for suffix in (".txt", ".md", ".docx", ".pdf", ".csv"):
        if files.exists(name, suffix):
            return name

    return None


def _read_file_request(text):
    """Resolve a request to read a file, or None."""
    match = _READ_FILE_EXPLICIT.match(text)

    if not match:
        return None

    return _file_target(match.group(1))


def _copy_file_request(text):
    """Resolve a request to copy a file, returning (source, target) or None."""
    match = _COPY_FILE.match(text)

    if not match:
        return None

    source = _file_target(match.group(1))

    if not source:
        return None

    return source, (match.group(2) or "").strip() or None


def _file_request(text):
    """Resolve a file command locally, or return None."""
    match = _CREATE_FILE.match(text)

    if match:
        suffix = files.FORMATS.get(match.group(1), ".txt")

        return ("create", match.group(2).strip(), suffix, None)

    match = _CREATE_FILE_PLAIN.match(text)

    if match:
        return ("create", match.group(1).strip(), ".txt", None)

    match = _ADD_TO_FILE.match(text)

    if match:
        return ("append", match.group(2).strip(), None, match.group(1).strip())

    return None


def _blank_result(intent, **fields):
    result = {
        "intent": intent, "application": None, "website": None,
        "project": None, "amount": None, "text": None, "unit": None,
    }
    result.update(fields)

    return result


def _fast_path(command):
    """Resolve an unambiguous command locally, or return None."""
    text = _normalise(command)

    if not text:
        return None

    intent = _FAST_LOOKUP.get(text)

    if intent:
        return _blank_result(intent)

    level = _volume_level(text)

    if level is not None:
        return _blank_result("set_volume", amount=level)

    timer = _timer_request(text)

    if timer:
        seconds, message = timer

        return _blank_result(
            "set_reminder", amount=seconds, unit="seconds", text=message
        )

    target = _read_file_request(text)

    if target:
        return _blank_result("read_file", text=target)

    copy_request = _copy_file_request(text)

    if copy_request:
        source, destination = copy_request

        return _blank_result("copy_file", text=source, project=destination)

    note = _note_request(text)

    if note:
        return _blank_result("make_note", text=_original_case(command, note))

    request = _file_request(text)

    if request:
        kind, name, suffix, content = request

        if kind == "create":
            return _blank_result(
                "create_file", text=_original_case(command, name),
                unit=suffix,
            )

        return _blank_result(
            "append_file", text=_original_case(command, content),
            project=_original_case(command, name),
        )

    payload = _copy_request(text)

    if payload:
        return _blank_result(
            "copy_to_clipboard", text=_original_case(command, payload)
        )

    for prefix, intent in (
        *((p, "open_application") for p in _OPEN_PREFIXES),
        *((p, "close_application") for p in _CLOSE_PREFIXES),
    ):
        if not text.startswith(prefix):
            continue

        target = text[len(prefix):].strip()

        if not target:
            continue

        # "done without luck" is "done with" + "out luck". Whether the "out"
        # rejoins the next word ("outlook") or stands apart ("out look")
        # depends on the app, so try both and keep whichever resolves.
        if prefix.endswith("without "):
            name = _resolve_app(
                f"out{target}") or _resolve_app(f"out {target}")

            if name:
                return _blank_result(intent, application=name)

            continue

        # A known website, but only for opening; closing a tab is different.
        if intent == "open_application" and target in _WEBSITES:
            return _blank_result("open_website", website=_WEBSITES[target])

        name = _resolve_app(target)

        if name:
            return _blank_result(intent, application=name)

    # Last resort before the LLM: a near miss on a known phrase, which covers
    # speech-recognition slips like "how is my sister".
    fuzzy = _fuzzy_intent(text)

    if fuzzy:
        print(f'[fast] fuzzy match on "{text}"')
        return _blank_result(fuzzy)

    return None


def _is_rate_limit(error):
    """True when a Groq error is a quota or rate limit rejection."""
    name = type(error).__name__.lower()

    if "ratelimit" in name:
        return True

    text = str(error).lower()

    return "rate_limit" in text or "429" in text


_YES = frozenset({
    "yes", "yeah", "yep", "yes please", "go ahead", "do it", "confirm",
    "confirmed", "affirmative", "sure", "ok", "okay", "please do",
    "overwrite", "replace it", "yes do it",
})

_NO = frozenset({
    "no", "nope", "cancel", "stop", "forget it", "never mind",
    "no thanks", "dont", "do not", "leave it", "negative", "abort",
})

# An action waiting on a spoken yes or no.
_pending = None


def _confirm(intent, question, action):
    """Ask before doing something, and remember what to do if approved."""
    global _pending

    _pending = {"intent": intent, "action": action}

    return {
        "kind": "query",
        "intent": intent,
        "response": None,
        "action": lambda: question,
    }


def _resolve_pending(text):
    """Handle a yes or no reply to an earlier question, or return None."""
    global _pending

    if not _pending:
        return None

    answer = _normalise(text)

    if answer in _YES:
        pending = _pending
        _pending = None

        return {
            "kind": "action",
            "intent": pending["intent"],
            "response": "Very good, sir.",
            "action": pending["action"],
        }

    if answer in _NO:
        _pending = None

        return _query("cancelled", lambda: "Cancelled, sir.")

    # Anything else is a new command, so the question lapses.
    _pending = None

    return None


def handle_command(command):
    answered = _resolve_pending(command)

    if answered is not None:
        return answered

    result = _fast_path(command)

    if result is not None:
        print(f"[fast] {result['intent']} (no API call)")

    else:
        candidates = _application_manager.candidates(command)

        try:
            result = _interpreter.interpret(
                command,
                candidates,
                _project_manager.names(limit=_PROJECT_CANDIDATES),
            )

        except Exception as error:
            if _is_rate_limit(error):
                print(f"[JARVIS] rate limited: {error}")

                return _query(
                    "rate_limited",
                    lambda: (
                        "I've reached my daily language limit, sir. "
                        "It will reset in a few hours."
                    ),
                )

            raise

    intent = result["intent"]
    application = result.get("application")
    website = result.get("website")
    project = result.get("project")
    amount = _to_number(result.get("amount"))
    text = _trim_filler(_normalise(result.get("text") or "")) or None
    unit = result.get("unit")

    if intent == "open_application" and application:
        return _action(
            intent,
            f"Opening {application}, sir.",
            lambda: _application_manager.launch(application),
        )

    if intent == "close_application" and application:
        return _action(
            intent,
            f"Closing {application}, sir.",
            lambda: _application_manager.close(application),
        )

    if intent == "open_website" and website:
        return _action(
            intent,
            f"Opening {_website_label(website)}, sir.",
            lambda: _open_website(website),
        )

    if intent == "open_project" and project:
        return _action(
            intent,
            f"Opening {project}, sir.",
            lambda: _project_manager.open(project) is not None,
        )

    if intent == "close_project" and project:
        return _action(
            intent,
            f"Closing {project}, sir.",
            lambda: _project_manager.close(project) is not None,
        )

    if intent == "list_projects":
        return _query(intent, _project_manager.describe)

    if intent == "set_volume" and amount is not None:
        level = max(0, min(100, round(amount)))

        return _action(
            intent,
            f"Setting volume to {level} percent, sir.",
            lambda: set_volume(level),
        )

    if intent == "get_volume":
        return _query(intent, describe_volume)

    if intent in _SIMPLE_ACTIONS:
        response, function = _SIMPLE_ACTIONS[intent]

        return _action(intent, response, function)

    if intent == "make_note" and text:
        return _action(
            intent,
            "Noted, sir.",
            lambda: notes.add(text),
        )

    if intent == "read_notes":
        return _query(intent, notes.describe)

    if intent == "clear_notes":
        return _action(
            intent,
            "Clearing your notes, sir.",
            lambda: notes.clear() >= 0,
        )

    if intent == "create_file" and text:
        suffix = unit or ".txt"
        body = result.get("website") or ""

        if files.exists(text, suffix):
            # Kept short and identical every time, so it plays from cache
            # rather than needing fresh synthesis, and leaves the microphone
            # deaf for a fraction of the time.
            return _confirm(
                intent,
                "That file exists. Overwrite it, sir?",
                lambda: files.write(
                    text, body, default_suffix=suffix, overwrite=True
                ) is not None,
            )

        return _action(
            intent,
            f"Creating {files.spoken_name(files.safe_name(text, suffix))}, sir.",
            lambda: files.write(text, body, default_suffix=suffix) is not None,
        )

    if intent == "append_file" and text and project:
        return _action(
            intent,
            "Added, sir.",
            lambda: files.append(project, text) is not None,
        )

    if intent == "read_file" and text:
        return _query(intent, lambda: files.describe_read(text))

    if intent == "copy_file" and text:
        return _action(
            intent,
            "Copying, sir.",
            lambda: files.copy(text, project) is not None,
        )

    if intent == "count_files":
        return _query(intent, files.describe_listing)

    if intent == "list_files":
        return _query(intent, files.describe_listing_named)

    if intent == "read_clipboard":
        return _query(intent, clipboard.describe)

    if intent == "copy_to_clipboard" and text:
        return _action(
            intent,
            "Copied, sir.",
            lambda: clipboard.write(text),
        )

    if intent == "clear_clipboard":
        return _action(
            intent,
            "Clearing your clipboard, sir.",
            clipboard.clear,
        )

    if intent == "take_screenshot":
        return _query(intent, describe_capture)

    if intent == "get_time":
        return _query(intent, describe_time)

    if intent == "get_weather":
        return _query(intent, describe_weather)

    if intent == "get_system_status":
        return _query(intent, describe_system)

    if intent == "set_reminder":
        seconds = to_seconds(amount, unit)

        if seconds:
            spoken = describe_duration(seconds)
            note = (text or "").strip()

            if note:
                response = f"I'll remind you to {note} in {spoken}, sir."
            else:
                response = f"Timer set for {spoken}, sir."

            return _action(
                intent,
                response,
                lambda: reminder_manager.add(seconds, note) is not None,
            )

    if intent == "list_reminders":
        return _query(intent, reminder_manager.describe)

    if intent == "cancel_reminders":
        return _action(
            intent,
            "Clearing them, sir.",
            lambda: reminder_manager.cancel_all() >= 0,
        )

    if intent == "answer_question":
        return _query(intent, lambda: answer(command))

    return None
