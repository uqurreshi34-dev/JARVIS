import re
import time
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
import phrases
from actions import camera, charts, clipboard, files, news, notes, screen_control
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
    "media_play_pause": (phrases.pick("acknowledge"), play_pause),
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
    (("show me the news", "whats the news", "what is the news",
      "read me the news", "the news", "news", "any news",
      "open the news", "open news", "bring up the news",
      "whats happening", "what is happening", "top stories",
      "headlines", "the headlines", "show me the headlines"),
     "show_news"),
    (("what do you see", "what can you see", "look at this",
      "take a look", "have a look", "what is this", "whats this",
      "look through the camera", "access camera", "access the camera",
      "use the camera", "open the camera", "what am i holding",
      "what am i holding in my hand", "how about now", "and now",
      "what about now", "look again"), "look"),
    (("close the camera", "stop looking", "camera off",
      "turn the camera off", "hide the camera"), "stop_looking"),
    (("save the picture", "save that picture", "save this picture",
      "save the photo", "save that photo", "save this photo",
      "keep that picture", "keep that photo", "save the image",
      "save that image", "keep the picture"), "save_picture"),
    (("whats on my screen", "what is on my screen", "whats on the screen",
      "what is on the screen", "what am i looking at", "describe my screen",
      "describe the screen", "tell me whats on my screen"),
     "describe_screen"),
    (("close the news", "hide the news", "close news", "hide news",
      "dismiss the news", "get rid of the news"), "hide_news"),
    (("how many files do i have", "how many files are there",
      "how many files"), "count_files"),
    (("list my files", "name my files", "read my files", "show me my files",
      "read out my files", "what files do i have", "what are my files",
      "what are my files called", "whats in my folder",
      "whats in my jarvis folder", "what is in my jarvis folder"),
     "list_files"),
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
    # "open the news" and "close the news" differ by one word and score 0.85
    # against each other, so closing must be said exactly.
    "hide_news",
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
_DESTINATION_ONLY = re.compile(
    rf"^{_TO_WORDS}\s+(?:my|the)?\s*\w+$"
)

_TRAILING_CLIPBOARD = re.compile(rf"\s+{_TO_WORDS}\s+(?:my|the)?\s*clipboard$")


# "remove juice from my notes" used to reach the language model, which read
# it as an instruction to clear the lot. It is handled locally now.
_REMOVE_FROM = re.compile(
    r"^(?:remove|delete|take out|get rid of|drop|erase)\s+(.+?)\s+"
    r"(?:from|out of|off)\s+(?:my\s+|the\s+)?(.+?)(?:\s+file)?$"
)

_NOTE_TARGETS = frozenset({"notes", "note", "note list", "notes list"})


def _remove_request(text):
    """Return (what, where) for a removal, or None."""
    match = _REMOVE_FROM.match(text)

    if not match:
        return None

    what = match.group(1).strip()
    where = match.group(2).strip()

    if not what or not where:
        return None

    return what, where


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

        # "copy to my clipboard" leaves only the destination behind, which is
        # not something to put on the clipboard.
        if _DESTINATION_ONLY.match(payload):
            continue

        return payload

    return None


# "on"/"the" are optional filler ("click on the submit button" vs "click
# submit"); the trailing "button"/"link"/"icon" is stripped since UIA names
# rarely include it. "go to" is included because it's how people phrase
# navigating to a menu item or section, not just literal buttons.
_CLICK_PATTERNS = (
    re.compile(
        r"^(?:click|press|select|tap|hit|choose)(?:\s+on)?\s+"
        r"(?:the\s+)?(.+?)(?:\s+(?:button|link|icon|option))?$"
    ),
    re.compile(r"^go\s+(?:to|into)\s+(?:the\s+)?(.+)$"),
)

_TYPE_PATTERN = re.compile(r"^(?:type|dictate)\s+(.+)$")


def _click_request(text):
    """Extract what to click, or None."""
    for pattern in _CLICK_PATTERNS:
        match = pattern.match(text)

        if not match:
            continue

        target = match.group(1).strip()

        if target and target not in ("this", "that", "it"):
            return target

    return None


def _type_request(text):
    """Extract what to type, or None."""
    match = _TYPE_PATTERN.match(text)

    if not match:
        return None

    payload = match.group(1).strip()

    return payload or None


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


_news_listener = None
_highlight_listener = None
_picture_listener = None
_chart_listener = None

# Whatever the panel is currently showing, so a story can be referred to by
# its number without fetching again.
_on_screen = []


def set_news_listener(listener):
    """Register a callable taking (region, headlines) to show the panel."""
    global _news_listener
    _news_listener = listener


def set_highlight_listener(listener):
    """Register a callable taking a story number to emphasise, or None."""
    global _highlight_listener
    _highlight_listener = listener


def set_picture_listener(listener):
    """Register a callable taking (image bytes, caption)."""
    global _picture_listener
    _picture_listener = listener


_camera_listener = None


def set_camera_listener(listener):
    """Register a callable taking (png bytes, caption)."""
    global _camera_listener
    _camera_listener = listener


def _look(question=None):
    """Glance through the camera and say what is there."""
    answer, image = camera.look(question)

    if image and _camera_listener:
        try:
            _camera_listener(image, question or "Camera")
        except Exception as error:
            print(f"[JARVIS] could not show the picture: {error}")

    return answer


def _stop_looking():
    camera.release()

    if _camera_listener:
        try:
            _camera_listener(b"", "")
        except Exception:
            pass

    return True


def _save_picture():
    """Save the camera's last picture to the JARVIS folder."""
    image = camera.last_image()

    if not image:
        return "There's no picture to save yet, sir."

    if not charts.save(image, "photo"):
        return "I couldn't save that picture, sir."

    return phrases.pick("saved")


def _click_thing(name):
    """Find something on the active window and click it, asking first if
    its name suggests something hard to undo."""
    element, label, risky = screen_control.find_clickable(name)

    if not element:
        return _query(
            "click_thing",
            lambda: f"I can't find anything called {name} on screen, sir.",
        )

    if risky:
        return _confirm(
            "click_thing",
            f"That looks like it might {label}, sir. Go ahead?",
            lambda: screen_control.click(element),
        )

    return _query(
        "click_thing",
        lambda: (
            f"Clicking {label}, sir." if screen_control.click(element)
            else f"I found {label}, sir, but couldn't click it."
        ),
    )


def _type_text(text):
    return _action(
        "type_text",
        phrases.pick("acknowledge"),
        lambda: screen_control.type_text(text),
    )


def set_chart_listener(listener):
    """Register a callable taking (png bytes, title) to show a chart."""
    global _chart_listener
    _chart_listener = listener


_AXIS_SPLIT = re.compile(
    r"^(.+?)\s+(?:on|as|for)?\s*(?:the\s+)?x(?:\s*axis)?\s*"
    r"(?:and|,|then)?\s*(?:and\s+)?(.+?)\s+"
    r"(?:on|as|for)?\s*(?:the\s+)?y(?:\s*axis)?$"
)

_PLAIN_PAIR = re.compile(r"^(.+?)\s+(?:and|against|versus|vs|by)\s+(.+)$")


def _columns_from_answer(text, headers):
    """Work out which two columns were asked for, or return None."""
    answer = _normalise(text)

    if not answer:
        return None

    match = _AXIS_SPLIT.match(answer)

    if match:
        first, second = match.group(1), match.group(2)
    else:
        match = _PLAIN_PAIR.match(answer)

        if not match:
            return None

        first, second = match.group(1), match.group(2)

    x_index = charts.match_column(headers, first)
    y_index = charts.match_column(headers, second)

    if x_index is None or y_index is None or x_index == y_index:
        return None

    return x_index, y_index


def _offer_to_save(data, title):
    """Ask whether to keep the chart, and remember the answer."""
    return _confirm(
        "save_chart",
        "Shall I save it to your JARVIS folder, sir?",
        lambda: charts.save(data, title) is not None,
        yes_text=phrases.pick("saved"),
        no_text=phrases.pick("declined"),
    )


def _plot_columns(name, headers, text):
    """Handle the answer to which columns to plot."""
    chosen = _columns_from_answer(text, headers)

    if not chosen:
        return _query(
            "plot_chart",
            lambda: (
                "I didn't catch which columns, sir. "
                f"The file has {charts.describe_columns(headers)}."
            ),
        )

    x_index, y_index = chosen
    data, spoken = charts.plot(name, x_index, y_index)

    if not data:
        return _query("plot_chart", lambda: spoken)

    title = f"{headers[y_index]} by {headers[x_index]}"

    if _chart_listener:
        try:
            _chart_listener(data, title)
        except Exception as error:
            print(f"[JARVIS] could not show the chart: {error}")

    # The chart is on screen, so now offer to keep it.
    offer = _offer_to_save(data, f"{name} {headers[y_index]}")
    question = offer["action"]()

    return _query("plot_chart", lambda: f"{spoken} {question}")


def _start_plot(name):
    """Read the file and ask which two columns to plot."""
    headers, rows = charts.read_columns(name)

    if not headers:
        return _query(
            "plot_chart",
            lambda: f"I couldn't read a spreadsheet called {name}, sir.",
        )

    if not rows:
        return _query("plot_chart", lambda: f"{name} has no data, sir.")

    question = (
        f"That has {charts.describe_columns(headers)}. "
        "Which two shall I plot, sir?"
    )

    return _ask(
        "plot_chart",
        question,
        lambda answer: _plot_columns(name, headers, answer),
    )


def _hide_chart():
    if _chart_listener:
        try:
            _chart_listener(b"", "")
        except Exception:
            pass

    return True


def _show_picture(number):
    """Show the picture for a story on screen, if it has one."""
    if not _on_screen:
        return "There is no news on screen, sir."

    try:
        index = int(number) - 1
    except (TypeError, ValueError):
        return "Which story, sir?"

    if not 0 <= index < len(_on_screen):
        return f"There are only {len(_on_screen)} stories, sir."

    item = _on_screen[index]
    url = item.get("image")

    if not url:
        return f"There's no picture with story {index + 1}, sir."

    data = news.image_bytes(url)

    if not data:
        return "I couldn't fetch that picture, sir."

    if _highlight_listener:
        try:
            _highlight_listener(index)
        except Exception:
            pass

    if _picture_listener:
        try:
            _picture_listener(data, item.get("title", ""))
        except Exception as error:
            print(f"[JARVIS] could not show the picture: {error}")
            return "I couldn't show that picture, sir."

    return f"Here's the picture for story {index + 1}, sir."


def _clear_picture():
    if _picture_listener:
        try:
            _picture_listener(b"", "")
        except Exception:
            pass

    return True


def _show_news(region):
    """Fetch headlines, hand them to the panel, and say a short summary."""
    global _on_screen

    items = news.headlines(region)
    _on_screen = items

    _clear_picture()

    if _news_listener:
        try:
            _news_listener(region, items)
        except Exception as error:
            print(f"[JARVIS] could not show the news: {error}")

    return news.describe(region)


def _hide_news():
    global _on_screen

    _on_screen = []

    if _news_listener:
        try:
            _news_listener(None, None)
        except Exception as error:
            print(f"[JARVIS] could not hide the news: {error}")

    return True


def _expand_story(number):
    """Read out a story that is currently on screen."""
    if not _on_screen:
        return "There is no news on screen, sir."

    try:
        index = int(number) - 1
    except (TypeError, ValueError):
        return "Which story, sir?"

    if not 0 <= index < len(_on_screen):
        return f"There are only {len(_on_screen)} stories, sir."

    item = _on_screen[index]

    if _highlight_listener:
        try:
            _highlight_listener(index)
        except Exception as error:
            print(f"[JARVIS] could not highlight the story: {error}")

    summary = (item.get("summary") or "").strip()
    source = item.get("source") or "the wire"

    if not summary:
        return f"Story {index + 1}, from {source}: {item['title']}."

    return f"{item['title']}. From {source}: {summary}"


def _copy_file_to_clipboard(name):
    """Put a whole file's contents on the clipboard, and report the size."""
    content = files.read(name)

    if content is None:
        return f"I couldn't find a file called {name}, sir."

    if not content.strip():
        return f"{files.spoken_name(name)} is empty, sir."

    if not clipboard.write(content):
        return "I couldn't reach the clipboard, sir."

    words = len(content.split())
    label = files.spoken_name(files.safe_name(name) or name)

    return f"Copied {label} to your clipboard, sir. {words} words."


def _original_case(command, payload):
    """Recover the user's original wording, since matching runs against
    heavily normalised text — lowercased, with punctuation stripped
    (see _normalise). That means a payload like "50 off" no longer
    matches its own source literally: the original command has "50%
    off", not "50 off", so an exact substring search fails and any
    symbol the payload lost stays lost. Instead, search for the
    payload's words in order, allowing any run of stripped punctuation
    between them, and return the real substring — that recovers "50%
    off" from a payload of "50 off" because the gap between "50" and
    "off" in the original is exactly the kind of non-word run this
    allows for.
    """
    original = (command or "").strip()

    words = payload.split()

    if not words:
        return payload

    pattern = r"\b" + r"\W*".join(re.escape(word) for word in words) + r"\b"
    match = re.search(pattern, original, re.IGNORECASE)

    if not match:
        return payload

    return match.group(0)


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
    rf"^{_ADD_VERBS}\s+(.+?)\s+{_TO_WORDS}\s+(?:my\s+|the\s+)?(.+?)"
    r"(?:\s+file)?$"
)


# Reading and copying are gated on the file existing, so "read my notes" and
# "open chrome" can never be mistaken for a file request. The word "file"
# makes it explicit; without it, the name must match something on disk.
_READ_FILE_EXPLICIT = re.compile(
    r"^(?:read|show me|open|display|whats in|what is in|whats on|"
    r"what is on|read out|read me)\s+(?:me\s+)?(?:my|the)?\s*"
    r"(?:file\s+)?(?:called\s+|named\s+)?(.+?)(?:\s+file)?$"
)

_COPY_FILE = re.compile(
    r"^(?:copy|duplicate|back up|backup)\s+(?:my|the)?\s*"
    r"(?:file\s+)?(.+?)(?:\s+file)?"
    r"(?:\s+(?:to|as|into)\s+(?:a\s+)?(?:file\s+)?(?:called\s+|named\s+)?(.+?))?$"
)

# Never treated as filenames, since they name another skill outright rather
# than a file. "notes" is deliberately absent: the notes phrases are matched
# exactly before any file lookup runs, so "copy my notes" can still copy
# notes.txt while "read my notes" still reaches the notes skill.
_NOT_FILENAMES = frozenset({
    "clipboard", "files", "file", "my clipboard", "my files",
    "them", "it", "this", "that",
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


_BARE_TO_CLIPBOARD = re.compile(
    rf"^(?:copy|put|send|save)\s+{_TO_WORDS}\s+(?:my|the)?\s*clipboard$"
)

_FILE_TO_CLIPBOARD = re.compile(
    rf"^(?:copy|put|send)\s+(?:the\s+)?(?:contents?\s+of\s+)?"
    rf"(?:my|the)?\s*(.+?)(?:\s+file)?\s+{_TO_WORDS}\s+"
    r"(?:my|the)?\s*clipboard$"
)


def _file_to_clipboard_request(text):
    """Resolve copying a whole file to the clipboard, or None."""
    # Speech often drops the small word: "copy it to my clipboard" arrives as
    # "copy to my clipboard". With something remembered, that is unambiguous.
    if _BARE_TO_CLIPBOARD.match(text):
        subject = _current_subject()

        return _file_target(subject) if subject else None

    match = _FILE_TO_CLIPBOARD.match(text)

    if not match:
        return None

    return _file_target(match.group(1))


def _read_file_request(text):
    """Resolve a request to read a file.

    Returns a name, or None. When the phrase says "file" outright the name is
    returned even if no such file exists, so JARVIS says it cannot find it
    rather than fuzzy-matching to some other command.
    """
    match = _READ_FILE_EXPLICIT.match(text)

    if not match:
        return None

    name = (match.group(1) or "").strip()

    if not name or name in _NOT_FILENAMES:
        return None

    existing = _file_target(name)

    if existing:
        return existing

    # "read my bugs file" is unambiguous even when bugs does not exist.
    if re.search(r"\bfiles?\b", text) and not text.endswith("my files"):
        return name

    return None


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
        name = match.group(2).strip()

        # Saying "file" makes it explicit; otherwise the name has to match a
        # real file, so "add milk to my notes" still goes to the notes skill.
        explicit = text.endswith(" file")

        if explicit or _file_target(name):
            return ("append", name, None, match.group(1).strip())

    return None


_STORY_WORDS = {
    "one": 1, "won": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4,
    "for": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ate": 8,
    "nine": 9, "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4,
    "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
    "tenth": 10, "last": 10,
}

_SHOW_PICTURE = re.compile(
    r"^(?:expand|show|show me|open|display)\s+(?:the\s+)?"
    r"(?:image|picture|photo|photograph|pic)\s*"
    r"(?:on|of|for|from)?\s*(?:the\s+)?"
    r"(?:story|headline|article|number|item)?\s*"
    r"(?:number\s*)?(\w+)$"
)

_HIDE_PICTURE = frozenset({
    "hide the image", "close the image", "hide the picture",
    "close the picture", "hide image", "close image",
})


def _picture_request(text):
    """The story number whose picture is wanted, or None."""
    match = _SHOW_PICTURE.match(text)

    if not match:
        return None

    return _story_number(match.group(1))


_EXPAND_STORY = re.compile(
    r"^(?:expand|open|read|tell me|show me)?\s*"
    r"(?:me\s+)?(?:more\s+(?:on|about)\s+|about\s+)?"
    r"(?:the\s+)?(?:story|headline|article|number|item)\s*"
    r"(?:number\s*)?(\w+)$"
)

_EXPAND_SHORT = re.compile(
    r"^(?:expand|read|open)\s+(\w+)$"
)


def _story_number(word):
    """A story number from digits or words, or None."""
    word = (word or "").strip()

    if word.isdigit():
        return int(word)

    return _STORY_WORDS.get(word.casefold())


def _expand_request(text):
    """The story number the user is asking about, or None."""
    for pattern in (_EXPAND_STORY, _EXPAND_SHORT):
        match = pattern.match(text)

        if not match:
            continue

        number = _story_number(match.group(1))

        if number:
            return number

    return None


_PLOT_REQUEST = re.compile(
    r"^(?:plot|chart|graph|draw a chart of|draw a graph of|visualise|"
    r"visualize)\s+(?:my\s+|the\s+)?(.+?)"
    r"(?:\s+(?:csv|file|spreadsheet|data))?$"
)

_HIDE_CHART = frozenset({
    "close the chart", "hide the chart", "close chart", "hide chart",
    "close the graph", "hide the graph", "dismiss the chart",
})


def _plot_request(text):
    """The spreadsheet to plot, or None."""
    match = _PLOT_REQUEST.match(text)

    if not match:
        return None

    name = match.group(1).strip()

    if not name or name in _NOT_FILENAMES:
        return None

    return name


_NEWS_REQUEST = re.compile(
    r"^(?:show me|read me|give me|whats|what is|tell me)?\s*"
    r"(?:the\s+)?(.+?)\s+"
    r"(?:news|headlines|stories)$"
)


def _news_request(text):
    """A news request naming a region, returning the region or None."""
    match = _NEWS_REQUEST.match(text)

    if not match:
        return None

    words = match.group(1).strip()

    # Try the whole phrase first, so "united states" beats "states".
    region = news.region_for(words)

    if region:
        return region

    for word in reversed(words.split()):
        region = news.region_for(word)

        if region:
            return region

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

    to_clipboard = _file_to_clipboard_request(text)

    if to_clipboard:
        return _blank_result("file_to_clipboard", text=to_clipboard)

    target = _read_file_request(text)

    if target:
        return _blank_result("read_file", text=target)

    copy_request = _copy_file_request(text)

    if copy_request:
        source, destination = copy_request

        return _blank_result("copy_file", text=source, project=destination)

    removal = _remove_request(text)

    if removal:
        what, where = removal

        if where in _NOTE_TARGETS:
            return _blank_result("remove_note", text=what)

        if _file_target(where):
            return _blank_result("remove_line", text=what, project=where)

    note = _note_request(text)

    if note:
        return _blank_result("make_note", text=_original_case(command, note))

    click_target = _click_request(text)

    if click_target:
        return _blank_result(
            "click_thing", text=_original_case(command, click_target)
        )

    type_payload = _type_request(text)

    if type_payload:
        return _blank_result(
            "type_text", text=_original_case(command, type_payload)
        )

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

        # An installed application always wins over a website of the same
        # name: "open netflix" should launch the Netflix app if it is
        # installed, and only fall back to the site if it is not.
        name = _resolve_app(target)

        if name:
            return _blank_result(intent, application=name)

        if intent == "open_application" and target in _WEBSITES:
            return _blank_result("open_website", website=_WEBSITES[target])

    if _on_screen:
        if text in _HIDE_PICTURE:
            return _blank_result("hide_picture")

        picture = _picture_request(text)

        if picture:
            return _blank_result("show_picture", amount=picture)

        number = _expand_request(text)

        if number:
            return _blank_result("expand_story", amount=number)

    if text in _HIDE_CHART:
        return _blank_result("hide_chart")

    plot = _plot_request(text)

    if plot:
        return _blank_result("plot_chart", text=plot)

    region = _news_request(text)

    if region:
        return _blank_result("show_news", text=region)

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

# What the last command was about, so a follow-up can say "it" instead of
# naming the thing again. Deliberately narrow: only an explicit pronoun
# resolves, it lapses after a minute, and it never applies to anything
# destructive, where a wrong guess costs data.
_CONTEXT_SECONDS = 60.0

_PRONOUNS = frozenset({"it", "that", "this"})
_PRONOUN_PHRASES = ("that one", "the same one", "the same")

# A misread pronoun here would delete something, so these never resolve.
_DESTRUCTIVE_OPENERS = (
    "remove", "delete", "clear", "wipe", "erase", "take out",
    "get rid", "drop", "cancel",
)

# Intents whose "text" field names a thing worth remembering. Notes and
# clipboard text are content, not subjects, so they are excluded.
_SUBJECT_INTENTS = frozenset({
    "read_file", "append_file", "copy_file", "create_file",
    "file_to_clipboard", "plot_chart", "open_project", "close_project",
})

_context = {"subject": None, "at": 0.0}


def _remember(subject):
    """Note what the last command was about."""
    subject = (subject or "").strip()

    if subject:
        _context["subject"] = subject
        _context["at"] = time.monotonic()


def _forget():
    _context["subject"] = None
    _context["at"] = 0.0


def _current_subject():
    """What "it" currently refers to, or None."""
    subject = _context.get("subject")

    if not subject:
        return None

    if time.monotonic() - _context.get("at", 0.0) > _CONTEXT_SECONDS:
        return None

    return subject


def _resolve_pronouns(text):
    """Replace a standalone pronoun with the last subject.

    Returns the text unchanged when there is nothing to resolve, so the
    command is then handled exactly as it would have been anyway.
    """
    subject = _current_subject()

    if not subject:
        return text

    lowered = text.strip()

    if lowered.startswith(_DESTRUCTIVE_OPENERS):
        return text

    for phrase in _PRONOUN_PHRASES:
        if f" {phrase}" in f" {lowered} " or lowered.endswith(f" {phrase}"):
            return lowered.replace(phrase, subject, 1)

    tokens = lowered.split()

    if not any(token in _PRONOUNS for token in tokens):
        return text

    replaced = []
    done = False

    for token in tokens:
        if not done and token in _PRONOUNS:
            replaced.append(subject)
            done = True
        else:
            replaced.append(token)

    return " ".join(replaced)


# An action waiting on a spoken yes or no.
_pending = None

# A question waiting on a spoken answer, such as which columns to plot. The
# handler is given the next thing said and returns a result, or None to give
# up and let the command be treated normally.
_awaiting = None


def _ask(intent, question, handler):
    """Ask something and hand the next utterance to the handler."""
    global _awaiting, _pending

    _pending = None
    _awaiting = {"intent": intent, "handler": handler}

    return {
        "kind": "query",
        "intent": intent,
        "response": None,
        "action": lambda: question,
    }


def _resolve_awaiting(text):
    """Give the answer to whatever asked the question, or return None."""
    global _awaiting

    if not _awaiting:
        return None

    handler = _awaiting["handler"]
    _awaiting = None

    try:
        return handler(text)
    except Exception as error:
        print(f"[JARVIS] could not use that answer: {error}")
        return None


def _confirm(intent, question, action, yes_text=None, no_text=None):
    """Ask before doing something, and remember what to do if approved."""
    global _pending, _awaiting

    _awaiting = None
    _pending = {
        "intent": intent,
        "action": action,
        "yes": yes_text or phrases.pick("acknowledge"),
        "no": no_text or phrases.pick("cancelled"),
    }

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
            "response": pending.get("yes", "Very good, sir."),
            "action": pending["action"],
        }

    if answer in _NO:
        message = _pending.get("no", "Cancelled, sir.")
        _pending = None

        return _query("cancelled", lambda: message)

    # Anything else is a new command, so the question lapses.
    _pending = None

    return None


def handle_command(command):
    command = _resolve_pronouns(command)

    answered = _resolve_awaiting(command)

    if answered is not None:
        return answered

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

    if not result:
        return None

    intent = result["intent"]
    application = result.get("application")

    # Whatever this command was about becomes what "it" means next.
    _remember(
        result.get("application")
        or result.get("project")
        or (result.get("text") if intent in _SUBJECT_INTENTS else None)
    )
    website = result.get("website")
    project = result.get("project")
    amount = _to_number(result.get("amount"))
    # Two versions of the same text: `text` is normalised (lowercased,
    # punctuation stripped) because most intents below use it for
    # matching — filenames, note lookups, removal targets. But that
    # normalisation is exactly what was eating the "%" out of "50% off"
    # for type_text and copy_to_clipboard: those two don't match
    # anything, they just need to reproduce what was said, verbatim.
    # result.get("text") already carries the recovered original wording
    # (see _original_case) for the fast path, or the LLM's own text for
    # the interpreter path — either way it's what should actually be
    # typed or copied, so it's kept alongside rather than only using
    # the flattened version.
    raw_text = (result.get("text") or "").strip()
    text = _trim_filler(_normalise(raw_text)) or None
    verbatim_text = raw_text or None
    unit = result.get("unit")

    if intent == "open_application" and application:
        return _action(
            intent,
            phrases.pick("opening", name=application),
            lambda: _application_manager.launch(application),
        )

    if intent == "close_application" and application:
        return _action(
            intent,
            phrases.pick("closing", name=application),
            lambda: _application_manager.close(application),
        )

    if intent == "open_website" and website:
        return _action(
            intent,
            phrases.pick("opening", name=_website_label(website)),
            lambda: _open_website(website),
        )

    if intent == "open_project" and project:
        return _action(
            intent,
            phrases.pick("opening", name=project),
            lambda: _project_manager.open(project) is not None,
        )

    if intent == "close_project" and project:
        return _action(
            intent,
            phrases.pick("closing", name=project),
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
            phrases.pick("acknowledge"),
            lambda: notes.add(text),
        )

    if intent == "remove_note" and text:
        return _query(intent, lambda: notes.describe_removal(text))

    if intent == "remove_line" and text and project:
        return _query(
            intent, lambda: files.describe_removal(project, text)
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
            phrases.pick(
                "creating",
                name=files.spoken_name(files.safe_name(text, suffix)),
            ),
            lambda: files.write(text, body, default_suffix=suffix) is not None,
        )

    if intent == "append_file" and text and project:
        return _action(
            intent,
            phrases.pick("added"),
            lambda: files.append(project, text) is not None,
        )

    if intent == "file_to_clipboard" and text:
        return _query(intent, lambda: _copy_file_to_clipboard(text))

    if intent == "read_file" and text:
        return _query(intent, lambda: files.describe_read(text))

    if intent == "copy_file" and text:
        return _action(
            intent,
            "Copying, sir.",
            lambda: files.copy(text, project) is not None,
        )

    if intent == "plot_chart" and text:
        return _start_plot(text)

    if intent == "hide_chart":
        return _action(intent, "Closing the chart, sir.", _hide_chart)

    if intent == "look":
        question = (text or "").strip() or None

        return _query(intent, lambda: _look(question))

    if intent == "stop_looking":
        return _action(intent, "Camera off, sir.", _stop_looking)

    if intent == "save_picture":
        return _query(intent, _save_picture)

    if intent == "click_thing" and text:
        return _click_thing(text)

    if intent == "type_text" and (verbatim_text or text):
        return _type_text(verbatim_text or text)

    if intent == "describe_screen":
        return _query(intent, screen_control.describe)

    if intent == "show_news":
        region = text if text in news.FEEDS else news.DEFAULT_REGION

        return _query(intent, lambda: _show_news(region))

    if intent == "show_picture" and amount is not None:
        return _query(intent, lambda: _show_picture(amount))

    if intent == "hide_picture":
        return _action(intent, "Closing the picture, sir.", _clear_picture)

    if intent == "expand_story" and amount is not None:
        return _query(intent, lambda: _expand_story(amount))

    if intent == "hide_news":
        return _action(intent, "Closing the news, sir.", _hide_news)

    if intent == "count_files":
        return _query(intent, files.describe_listing_count)

    if intent == "list_files":
        return _query(intent, files.describe_listing_named)

    if intent == "read_clipboard":
        return _query(intent, clipboard.describe)

    if intent == "copy_to_clipboard" and (verbatim_text or text):
        return _action(
            intent,
            phrases.pick("copied"),
            lambda: clipboard.write(verbatim_text or text),
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
