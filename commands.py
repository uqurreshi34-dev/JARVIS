import re
import webbrowser
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
from actions import clipboard
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
      "hows my system doing", "hows my pc", "system report"),
     "get_system_status"),
    (("whats on my clipboard", "what is on my clipboard",
      "read my clipboard", "check my clipboard"), "read_clipboard"),
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

_OPEN_PREFIXES = ("open ", "launch ", "start ", "run ")
_CLOSE_PREFIXES = ("close ", "quit ", "exit ", "shut ")

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

_DURATION = re.compile(
    r"(?:for|in)?\s*([\w\s]+?)\s+(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b"
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


def _parse_duration(text):
    """Return (seconds, message) from a timer phrase, or (None, None)."""
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

    # Anything after "to ..." is what to be reminded about.
    tail = text[match.end():].strip()
    message = tail.removeprefix("to ").strip() or None

    return seconds, message


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

    for prefix, intent in (
        *((p, "open_application") for p in _OPEN_PREFIXES),
        *((p, "close_application") for p in _CLOSE_PREFIXES),
    ):
        if not text.startswith(prefix):
            continue

        target = text[len(prefix):].strip()

        if not target:
            continue

        # A known website, but only for opening; closing a tab is different.
        if intent == "open_application" and target in _WEBSITES:
            return _blank_result("open_website", website=_WEBSITES[target])

        # Only take the fast path when the application resolves cleanly.
        # Anything fuzzy, or a project, goes to the LLM.
        app = _application_manager.find(target)

        if app:
            return _blank_result(intent, application=app.name)

    return None


def _is_rate_limit(error):
    """True when a Groq error is a quota or rate limit rejection."""
    name = type(error).__name__.lower()

    if "ratelimit" in name:
        return True

    text = str(error).lower()

    return "rate_limit" in text or "429" in text


def handle_command(command):
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
