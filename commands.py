import os
import re
import threading
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
from actions.knowledge import answer, answer_with_documents, commit_message
from actions.projects import ProjectManager
from actions.reminders import ReminderManager, describe_duration, to_seconds
import phrases
from actions import (
    browser,
    camera,
    charts,
    clipboard,
    diary,
    documents,
    files,
    git_tasks,
    image_choices,
    images,
    journal,
    market_report,
    markets,
    memory,
    news,
    proofread,
    notes,
    patterns,
    safety,
    screen_control,
    tasks,
)
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


# What the command in flight was about, so the journal can name it without
# every handler having to pass it along.
_in_flight = {"detail": None}


def _set_subject(result):
    """Note what this command is about, for the journal."""
    _in_flight["detail"] = (
        result.get("application")
        or result.get("project")
        or result.get("website")
        or result.get("text")
        or None
    )


def _record(intent, action, detail=None):
    """Wrap an action so what it did lands in the journal.

    Only intents that change something are recorded, and it happens here
    rather than in each handler, so a new skill is logged without anyone
    having to remember to add a line to it.
    """
    if intent not in _WRITE_INTENTS or not callable(action):
        return action

    def recorded():
        outcome = action()

        succeeded = bool(outcome)

        # An explicit detail wins; otherwise use whatever the command was
        # about; otherwise say nothing beyond the verb.
        said = detail or _in_flight.get("detail") or ""

        journal.action(intent, said, succeeded)

        return outcome

    return recorded


def _action(intent, response, action, detail=None):
    """A command that does something; JARVIS confirms when it succeeds."""
    return {
        "kind": "action",
        "intent": intent,
        "response": response,
        "action": _record(intent, action, detail),
    }


def _query(intent, action, detail=None):
    """A command that finds something out; the action returns what to say."""
    return {
        "kind": "query",
        "intent": intent,
        "response": None,
        "action": _record(intent, action, detail),
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
    (("what have you done", "whats in the log", "what is in the log",
      "read the log", "show me the log", "what did you do",
      "whats your log", "activity log"), "read_log"),
    (("open my project", "open my main project", "open my default project",
      "open the project", "load my project"), "open_default_project"),
    (("whats on my calendar", "whats in my calendar",
      "what is in my calendar", "read my diary",
      "whats on my diary", "what is on my diary", "what is on my calendar", "my calendar",
      "whats in my diary", "what is in my diary", "read my calendar",
      "whats coming up", "what is coming up", "what have i got on",
      "whats my schedule", "check my calendar"), "read_calendar"),
    (("clear my calendar", "empty my calendar", "delete my calendar",
      "wipe my calendar", "clear my diary"), "clear_calendar"),
    (("how are the markets", "how are my markets", "hows the market",
      "hows bitcoin", "how is bitcoin", "whats bitcoin at",
      "whats bitcoin doing", "market prices", "crypto prices",
      "whats the bitcoin price", "how are my crypto",
      "how is crypto doing", "market summary"), "market_summary"),
    (("what are you watching", "whats your market alerts",
      "what are my market alerts", "market alerts",
      "what alerts do i have"), "read_market_alerts"),
    (("what do you know about me", "what do you remember",
      "what do you remember about me", "whats in your memory",
      "what have you remembered"), "recall_memory"),
    (("what have you done today", "how busy have you been",
      "whats your day been like", "summarise the log",
      "summarise your log", "log summary"), "log_summary"),
    (("close the camera", "stop looking", "camera off",
      "turn the camera off", "hide the camera"), "stop_looking"),
    (("show me your mind", "show your mind", "open your mind",
      "show me your brain", "show your brain", "activate your mind",
      "brain view", "show brain view", "neural view",
      "show your neural net", "light up your mind"), "show_brain"),
    (("hide your mind", "close your mind", "hide the brain",
      "close the brain", "hide brain view", "turn off your mind",
      "stop showing your mind"), "hide_brain"),
    (("read this page", "read the page", "read this article",
      "read the article", "read this website", "summarise this page",
      "summarise the page", "summarise this article",
      "what does this page say", "whats this page say",
      "read the page to me"), "read_page"),
    (("whats on this page", "what is on this page", "whats on the page",
      "what is on the page", "describe this page", "describe the page",
      "whats on this website", "what can i click on this page",
      "page overview", "survey this page"), "page_overview"),
    (("what page am i on", "which page am i on", "what page is this",
      "whats open in chrome", "what is open in chrome",
      "what tab am i on", "what website am i on",
      "what tub am i on", "which tub am i on"), "current_page"),
    (("what does this file contain", "what does this document contain",
      "what does the file contain", "what does the document contain",
      "what does this file say", "what does this document say",
      "what does the file say", "what does the document say",
      "whats in this file", "whats in this document",
      "whats in the file", "whats in the document",
      "what is in this file", "what is in this document",
      "what is in the file", "what is in the document",
      "summarise this file", "summarise this document",
      "summarize this file", "summarize this document",
      "summarise the file", "summarise the document",
      "summarize the file", "summarize the document",
      "tell me about this document", "tell me about this file",
      "tell me about the document", "tell me about the file"),
     "answer_question"),
    (("save this page", "save the page", "save this page as a file",
      "take the page as a file", "save this article",
      "save the article", "keep this page"), "page_to_file"),
    (("fix them", "fix the mistakes", "fix the spelling", "correct them",
      "correct the mistakes", "fix those", "sort them out",
      "list them", "read them", "read them out", "list the mistakes",
      "tell me them", "what are they", "list to them",
      "write a report", "write the report", "save a report",
      "write me a report",
      "copy the corrected text", "copy the corrections",
      "copy the corrected version", "put it on my clipboard",
      "copy the fixed text"), "proofread_followup"),
    (("proofread my screen", "proofread the screen", "check my screen",
      "check the spelling on screen", "check my spelling",
      "spell check my screen", "proofread this", "check this",
      "proofread what im writing", "check what im writing",
      "proofread my writing"), "proofread_screen"),
    (("save the chart", "save that chart", "save this chart",
      "save the graph", "save that graph", "save this graph",
      "keep the chart", "keep that chart", "keep the graph",
      "save the plot", "save that plot"), "save_chart"),
    (("save the picture", "save that picture", "save this picture",
      "save the photo", "save that photo", "save this photo",
      "keep that picture", "keep that photo",
      "keep the picture"), "save_picture"),
    (("save the image", "save that image", "save this image",
      "save image", "keep the image", "keep that image",
      "download the image", "download this image"), "save_image"),
    (("close the image", "hide the image", "close that image",
      "dismiss the image", "close this image", "close the images",
      "close image", "hide image",
      "i dont want any of these", "i dont want any of these images",
      "none of these images", "not any of these",
      "not any of these images", "cancel the images",
      "close the picker", "dismiss these"), "hide_image"),
    (("enlarge the image", "enlarge image", "make the image bigger",
      "make image bigger", "zoom in on the image", "bigger image",
      "increase the image size", "make it bigger"), "enlarge_image"),
    (("shrink the image", "shrink image", "make the image smaller",
      "make image smaller", "zoom out on the image", "smaller image",
      "decrease the image size", "make it smaller"), "shrink_image"),
    (("restore the image", "restore image", "reset the image size",
      "restore the image to its original size",
      "restore image to original size", "original size",
      "undo the zoom"), "restore_image"),
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
    (("clear the documents", "clear documents",
      "clear my documents"), "clear_documents"),
    (("what documents do i have", "what documents have i loaded",
      "how many documents do i have", "what have you read",
      "what documents are loaded", "list my documents",
      "list the documents"), "list_documents"),
    (("what patterns have you noticed", "what have you noticed about me",
      "what patterns do you know", "list my patterns",
      "list the patterns", "what patterns are there"),
     "list_patterns"),
    (("forget all patterns", "forget every pattern",
      "clear all patterns", "clear my patterns"),
     "forget_all_patterns"),
    (("propose a commit", "propose a commit message",
      "suggest a commit message", "suggest a commit",
      "write me a commit message", "write a commit message",
      "commit my changes", "whats my commit message",
      "draft a commit message"), "propose_commit"),
    (("what have i changed", "whats changed in my project",
      "what's my git status", "whats my git status", "git status",
      "what's staged", "whats staged", "what have i staged"),
     "git_status"),
    (("what tasks do i have", "what tasks are there", "list my tasks",
      "list the tasks", "what can you run", "what tasks can you run"),
     "list_tasks"),
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
    "clear_documents",
    # Losing every noticed pattern to a near miss is the same risk as
    # clear_documents, for the same reason.
    "forget_all_patterns",
    # "open the news" and "close the news" differ by one word and score 0.85
    # against each other, so closing must be said exactly.
    "hide_news",
    # "close the picture" and "save the picture" differ by one word too, and
    # a near miss there writes a file the user did not ask for.
    "save_picture",
    # Same risk, same reason: "close the image" and "save the image" are
    # one word apart.
    "save_image",
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
    re.compile(
        r"^(?!what|whats|which|show|read|check|list|clear|delete|"
        r"empty|wipe|does|do|did|is|are|can|could|would|will|"
        r"has|have|contains|where|when|why|how)"
        rf"(.+?)\s+{_TO_WORDS}\s+(?:my|the)\s+notes$"
    ),
)


def _looks_like_question(text):
    """True when the utterance is phrased as a question rather than an action."""
    text = (text or "").strip().casefold()

    return (
        text.startswith((
            "does ", "do ", "did ", "is ", "are ", "can ", "could ",
            "would ", "will ", "has ", "have ", "where ", "when ",
            "why ", "how ",
        ))
        or "?" in text
    )


def _note_request(text):
    """Extract a note to save, or None."""
    if _looks_like_question(text):
        return None

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


# Verbs that only ever mean the web. "go to" is deliberately absent: it is
# already a click verb (see _CLICK_PATTERNS), so it is handled separately
# below and only when the target is plainly a website.
_BROWSE_PATTERN = re.compile(
    r"^(?:browse\s+to|navigate\s+to|take\s+me\s+to|pull\s+up)\s+"
    r"(?:the\s+)?(.+)$"
)

# "go to" is shared with clicking, so it is only treated as browsing when
# what follows resolves to an actual site. "open" is left out altogether:
# it already means launch-an-application or open-in-default-browser, and
# quietly redirecting it into the attached Chrome would change what an
# existing command does.
_AMBIGUOUS_BROWSE = re.compile(r"^go\s+to\s+(?:the\s+)?(.+)$")

_SEARCH_PATTERN = re.compile(
    r"^(?:search\s+(?:the\s+web\s+)?for|search\s+google\s+for|"
    r"google|look\s+up|search)\s+(.+)$"
)

# Trailing words that belong to the phrasing rather than the query.
_SEARCH_TAIL = re.compile(
    r"\s+(?:on\s+google|on\s+the\s+web|online|in\s+chrome|"
    r"on\s+the\s+internet)$"
)


def _browse_request(text):
    """Extract a URL to visit, or None.

    Only returns something when the target really is a website. Anything
    vaguer is left alone, so "go to settings" still reaches click_thing
    and "open notepad" still launches an application.
    """
    match = _BROWSE_PATTERN.match(text)

    if match:
        target = match.group(1).strip()

        # An explicit browsing verb means the web even for a bare word,
        # so an unrecognised name becomes a search rather than nothing.
        return browser.resolve_target(target) or browser.search_url(target)

    match = _AMBIGUOUS_BROWSE.match(text)

    if match:
        # Shared verb: only take it when it is unmistakably a site.
        return browser.resolve_target(match.group(1).strip())

    return None


def _search_request(text):
    """Extract a web search query, or None."""
    match = _SEARCH_PATTERN.match(text)

    if not match:
        return None

    query = _SEARCH_TAIL.sub("", match.group(1).strip()).strip()

    # "search my notes", "search my files" and friends belong to other
    # commands. "the" is left alone: "search for the offside rule" is a
    # perfectly ordinary web search.
    if not query or query.split()[0] == "my":
        return None

    return query


def _page_to_file():
    """Save the readable text of the current page into the JARVIS folder."""
    title, url, body = browser.page_text()

    if not url:
        return (
            "Chrome isn't listening for me, sir. Start it with the JARVIS "
            "shortcut and I'll try again."
        )

    if not body:
        return "There's nothing readable on that page to save, sir."

    # The title comes from the page, so it is outside content: it decides
    # a filename here, which is exactly the sort of thing worth cleaning
    # before it touches the file system.
    stem = safety.clean(title, 60) or "page"
    stem = re.sub(r"[^\w\s-]", "", stem).strip() or "page"

    written = files.write(
        stem,
        f"{title}\n{url}\n\n{body}",
        default_suffix=".txt",
        overwrite=True,
    )

    if not written:
        return "I couldn't save that page, sir."

    label = files.spoken_name(files.safe_name(stem, ".txt"))
    words = len(body.split())

    journal.browser("saved", url, f"{stem}.txt, {words} words")

    return f"Saved {label} to your JARVIS folder, sir. {words} words."


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


_brain_listener = None
_brain_visible = False


def set_brain_listener(listener):
    """Register a callable taking a bool: True shows the mind view, False
    hides it."""
    global _brain_listener
    _brain_listener = listener


def _show_brain():
    global _brain_visible
    _brain_visible = True

    if _brain_listener:
        try:
            _brain_listener(True)
        except Exception as error:
            print(f"[JARVIS] could not show the mind: {error}")
            return False

    return True


def _hide_brain():
    global _brain_visible
    _brain_visible = False

    if _brain_listener:
        try:
            _brain_listener(False)
        except Exception as error:
            print(f"[JARVIS] could not hide the mind: {error}")
            return False

    return True


_image_listener = None


def set_image_listener(listener):
    """Register a callable taking
    (png bytes, title, caption, caption_link, scale).

    Called with empty/1.0 values to hide the panel, matching the shape
    the camera and chart listeners already use.
    """
    global _image_listener
    _image_listener = listener


def _push_image():
    """Send the current image (or nothing) to the panel, if one is
    registered. Shared by every intent below that changes what the
    image panel should be showing.

    Uses current_display() rather than current_bytes(): the panel fits
    whatever it is given to its own window, so handing it an
    already-resized image just gets fit straight back to the same size
    — that was why "make it bigger" visibly did nothing. Sending
    rotation-only bytes plus the scale as a separate number lets the
    panel apply zoom on top of its own sizing instead of underneath it.
    """
    if not _image_listener:
        return

    if not images.has_image():
        try:
            _image_listener(b"", "", "", "", 1.0)
        except Exception as error:
            print(f"[JARVIS] could not clear the image panel: {error}")
        return

    data, scale = images.current_display()

    photographer, photographer_link, photo_link = images.attribution()
    caption = f"Photo by {photographer} on Unsplash" if photographer else ""

    try:
        _image_listener(
            data or b"", images.current_title(), caption,
            photo_link or "", scale,
        )
    except Exception as error:
        print(f"[JARVIS] could not update the image panel: {error}")


_choices_listener = None


def set_choices_listener(listener):
    """Register a callable taking the current list of image choices —
    each a dict with "data" (preview bytes) and "title" — or an empty
    list to hide the picker.
    """
    global _choices_listener
    _choices_listener = listener


def _on_choices_changed(choices):
    """Forward image_choices's own notifications to whatever main.py
    registered above. image_choices.py doesn't need to know commands.py
    or main.py exist at all — it just calls whatever listener it has,
    exactly like every other listener in this file.
    """
    if _choices_listener:
        try:
            _choices_listener(choices)
        except Exception as error:
            print(f"[JARVIS] could not update the choices panel: {error}")


image_choices.set_listener(_on_choices_changed)


# Digits and the ordinal words people actually say in reply to "one, two,
# or three".
_CHOICE_WORDS = {
    "1": 1, "one": 1, "first": 1, "1st": 1,
    "2": 2, "two": 2, "second": 2, "2nd": 2,
    "3": 3, "three": 3, "third": 3, "3rd": 3,
}

_CHOICE_CANCEL_WORDS = frozenset({
    "none", "cancel", "never", "nevermind", "stop", "forget",
})

# "One" does double duty in English: the digit 1, and the placeholder
# pronoun for "that option" ("the blue one", "the small one"). Only the
# word right before "one" tells them apart — a colour or size word means
# a photo is being described, not a number picked. This does not apply to
# "two"/"three": they aren't used as a standalone pronoun this way, so
# "the blue two" was never ambiguous to begin with.
_DESCRIPTIVE_BEFORE_ONE = frozenset({
    "blue", "red", "green", "yellow", "orange", "purple", "pink",
    "black", "white", "grey", "gray", "brown", "beige",
    "big", "bigger", "small", "smaller", "large", "little", "tiny",
    "left", "right", "top", "bottom", "middle", "last",
    "dark", "light", "bright", "blurry", "nice", "pretty",
})


def _parse_choice_number(text):
    """1, 2, or 3 from a spoken reply, or None if it can't be read that
    way. Checked as whole words, not substrings — "third" should not
    accidentally match inside some unrelated longer word.
    """
    words = _normalise(text).split()

    for index, word in enumerate(words):
        if (
            word == "one" and index > 0
            and words[index - 1] in _DESCRIPTIVE_BEFORE_ONE
        ):
            # "the blue one" -- describing the photo, not picking a
            # number. Skip just this occurrence; a genuine number word
            # elsewhere in the same sentence is still found below, and
            # "the first one" is unaffected regardless, since "first"
            # itself already matches before this word is even reached.
            continue

        if word in _CHOICE_WORDS:
            return _CHOICE_WORDS[word]

    return None


def _is_choice_cancel(text):
    words = set(_normalise(text).split())

    return bool(words & _CHOICE_CANCEL_WORDS)


def _looks_like_choice_reply(text):
    """True when a reply is unmistakably meant for the picker.

    This is what lets "select image one" resolve as choice #1 instead
    of being read as a click_thing command aimed at something called
    "image one" — "select" is already a registered click verb, so
    without this, the picker would be silently abandoned and JARVIS
    would go looking for something to click that doesn't exist.
    """
    return _is_choice_cancel(text) or _parse_choice_number(text) is not None


def select_image_choice(number):
    """Pick one of the three current image search results.

    Used by both the voice reply handler below and a direct click on a
    card in the HUD's own picker panel — mirroring toggle_brain_view's
    shape, since a click never passes through handle_command at all. A
    click resolving things directly, rather than through
    _resolve_awaiting, means _awaiting has to be cleared here explicitly
    too: without that, JARVIS would still think he's waiting for a
    spoken one/two/three and wrongly intercept whatever is said next.
    """
    global _awaiting

    if not image_choices.select(number):
        return False

    _awaiting = None
    _push_image()

    return True


def _select_image_choice(text):
    """Handle the reply to "which one, sir?" after a three-image search."""
    if _is_choice_cancel(text):
        image_choices.cancel()

        return _query("show_image", lambda: "No problem, sir.")

    number = _parse_choice_number(text)

    if number is None:
        # Didn't catch a clear one/two/three -- ask again rather than
        # silently leaving the picker on screen with nothing left
        # listening for an answer.
        return _ask(
            "show_image", "Sorry, was that one, two, or three, sir?",
            _select_image_choice, recognizes=_looks_like_choice_reply,
        )

    if not select_image_choice(number):
        return _ask(
            "show_image",
            "I couldn't get that one, sir. One, two, or three?",
            _select_image_choice, recognizes=_looks_like_choice_reply,
        )

    return _query("show_image", lambda: "Here you are, sir.")


def _show_image(query):
    """Search Unsplash for up to three candidates and ask which to use.

    Returned directly as the dispatch result (see the show_image branch
    below) rather than nested inside another _query's action: _ask()
    needs to be the top-level result so handle_command hands the very
    next utterance to _select_image_choice instead of treating it as a
    new command.
    """
    if not images.available():
        return _query(
            "show_image",
            lambda: (
                "I don't have an Unsplash key set up, sir. Set "
                "UNSPLASH_ACCESS_KEY and I'll be able to."
            ),
        )

    if not image_choices.search(query):
        return _query(
            "show_image",
            lambda: f"I couldn't find a picture of {query}, sir.",
        )

    return _ask(
        "show_image",
        f"I found a few for {query}, sir. Say or click one, two, or three.",
        _select_image_choice, recognizes=_looks_like_choice_reply,
    )


def _hide_image():
    """Close whichever is actually on top.

    Checked in priority order: the three-candidate picker first, then a
    committed fetched image, and only if neither is showing does
    "close/hide image" fall back to meaning the camera — the same
    assumption this code made before the image feature existed at all,
    kept as the final default now that "image" has more than one thing
    it could mean.
    """
    global _awaiting

    if image_choices.active():
        image_choices.cancel()
        _awaiting = None

        return True

    if images.has_image():
        images.hide()
        _push_image()

        return True

    return _stop_looking()


def _rotate_image(degrees):
    if images.rotate(degrees) is None:
        return "There's no image on screen to rotate, sir."

    _push_image()

    return "Rotated, sir."


def _enlarge_image():
    if images.enlarge() is None:
        return "There's no image on screen to enlarge, sir."

    _push_image()

    return "There you are, sir."


def _shrink_image():
    if images.shrink() is None:
        return "There's no image on screen to shrink, sir."

    _push_image()

    return "There you are, sir."


def _restore_image():
    if images.restore() is None:
        return "There's no image on screen to restore, sir."

    _push_image()

    return "Restored, sir."


def _save_image():
    path = images.save()

    if not path:
        return "There's no image on screen to save, sir."

    return "Saved to your JARVIS images folder, sir."


def toggle_brain_view():
    """Flip the mind view from outside a voice command — the HUD's own
    core click, specifically. Voice commands use the show_brain/hide_brain
    intents below instead, which log through the usual _record/_WRITE_
    INTENTS mechanism; this logs directly since a click never passes
    through handle_command at all, so _record never sees it.
    """
    intent = "hide_brain" if _brain_visible else "show_brain"
    action = _hide_brain if _brain_visible else _show_brain

    succeeded = bool(action())
    journal.action(intent, "", succeeded)

    return succeeded


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


def _click_now(name):
    """Find the control again and click it.

    Used by the confirmation path: between JARVIS asking and the answer
    arriving, a dialog can close or a page can navigate, which would leave
    the stored element pointing at something else. Finding it again at the
    moment of the click means the thing clicked is the thing described.
    """
    element, label, _ = screen_control.find_clickable(name)

    if not element:
        print(f"[JARVIS] {name!r} is no longer on screen")
        return False

    return bool(screen_control.click(element))


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
            f"{label} looks hard to undo, sir. Go ahead?",
            lambda: _click_now(name),
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


# What was last proofread, so a follow-up can act on it without checking
# the whole file again.
_last_check = {
    "name": None,
    "findings": None,
    "total": 0,
    # Set when the text came from the screen rather than a file, since it
    # cannot be corrected in place.
    "screen_text": None,
}


def _proofread_answer(text):
    """Handle the answer to what to do about the mistakes found."""
    answer = _normalise(text)

    name = _last_check.get("name")
    findings = _last_check.get("findings") or []

    if not name or not findings:
        return _query("proofread", lambda: "There's nothing pending, sir.")

    words = set(answer.split())

    if words & {"copy", "clipboard", "paste"}:
        return _query("proofread_copy", _copy_corrected, detail=name)

    if words & {"fix", "correct", "repair", "sort", "amend"}:
        # Screen text belongs to another application, so it is offered on
        # the clipboard rather than typed over the top of someone's work.
        if _last_check.get("screen_text"):
            return _query("proofread_copy", _copy_corrected, detail=name)

        usable = [f for f in findings if f["suggestions"]]

        if not usable:
            return _query(
                "proofread",
                lambda: "None of them have a suggestion I trust, sir.",
            )

        return _confirm(
            "proofread_fix",
            f"That will change {phrases.number(len(usable))} words in "
            f"{name}. Go ahead?",
            lambda: proofread.apply_fixes(name, findings) is not None,
            yes_text="Corrected, sir.",
            no_text="Leaving it as it is, sir.",
        )

    if words & {"report", "write", "save"}:
        def write_report():
            path = proofread.report(name, findings, _last_check["total"])

            if not path:
                return "I couldn't write the report, sir."

            return (
                f"Written to {files.spoken_name(os.path.basename(path))}, sir."
            )

        return _query("proofread_report", write_report, detail=name)

    if words & {"list", "read", "tell", "say"}:
        # The question stays open, so "fix them" still works next.
        _rearm_proofread()

        return _query(
            "proofread", lambda: proofread.spoken_list(findings)
        )

    # Not an answer we recognise, so ask once more rather than giving up.
    _rearm_proofread()

    return _query(
        "proofread",
        lambda: (
            "I can list them, fix them, or write a report, sir. "
            "Which would you like?"
        ),
    )


def _rearm_proofread():
    """Keep the proofread question open for another answer."""
    global _awaiting

    _awaiting = {"intent": "proofread", "handler": _proofread_answer}


def _proofread_named(text):
    """Handle the answer to "which file shall I check?"."""
    spoken = _normalise(text)

    # Strip the wording people naturally add to an answer.
    for prefix in ("my ", "the ", "check ", "proofread "):
        if spoken.startswith(prefix):
            spoken = spoken[len(prefix):]

    for suffix in (" file", " document", " please"):
        if spoken.endswith(suffix):
            spoken = spoken[: -len(suffix)]

    name = spoken.strip()

    if not name:
        return _query(
            "proofread", lambda: "I didn't catch which file, sir."
        )

    if not _file_target(name):
        return _query(
            "proofread",
            lambda: f"I can't find a file called {name}, sir.",
        )

    return _start_proofread(name)


# The event waiting on a date, so "add X to my calendar" can ask.
_pending_event = {"title": None}


def _dated_event(text):
    """Handle the answer to "what date, sir?"."""
    title = _pending_event.get("title")

    if not title:
        return _query("calendar", lambda: "Which event, sir?")

    when = diary.parse_date(text)

    if not when:
        return _ask(
            "calendar",
            "I didn't catch the date, sir. When is it?",
            _dated_event,
        )

    at = diary.parse_time(text)

    def store():
        return diary.add(title, when, at) is not None

    _pending_event["title"] = None

    return _action(
        "add_event",
        f"{title}, {diary.spoken_when(_moment_for(when, at))}. Done, sir.",
        store,
        detail=f"{title} on {when.isoformat()}",
    )


def _moment_for(when, at):
    """A date, or a datetime when a time was given."""
    if not at:
        return when

    from datetime import datetime

    return datetime.combine(when, datetime.min.time()).replace(
        hour=at[0], minute=at[1]
    )


def _start_event(title, spoken_date=None):
    """Add an event, asking for the date if it was not given."""
    title = (title or "").strip()

    if not title:
        return _query("calendar", lambda: "What should I call it, sir?")

    when = diary.parse_date(spoken_date) if spoken_date else None

    if when:
        at = diary.parse_time(spoken_date)

        return _action(
            "add_event",
            f"{title}, {diary.spoken_when(_moment_for(when, at))}. Done, sir.",
            lambda: diary.add(title, when, at) is not None,
            detail=f"{title} on {when.isoformat()}",
        )

    _pending_event["title"] = title

    return _ask("calendar", "What date, sir?", _dated_event)


def _dated_removal(text):
    """Handle the answer to "which date, sir?" when removing."""
    title = _pending_event.get("title")

    if not title:
        return _query("calendar", lambda: "Which event, sir?")

    when = diary.parse_date(text)

    # "all of them" removes every event with that name.
    everywhere = any(
        word in _normalise(text) for word in ("all", "any", "every")
    )

    if not when and not everywhere:
        return _ask(
            "calendar",
            "I didn't catch the date, sir. Which date?",
            _dated_removal,
        )

    _pending_event["title"] = None

    def drop():
        removed = diary.remove(title, None if everywhere else when)

        return removed > 0

    return _action(
        "remove_event",
        f"Removing {title}, sir.",
        drop,
        detail=title,
    )


def _start_remove_event(title, spoken_date=None):
    """Remove an event, asking which date if it was not given."""
    title = (title or "").strip()

    if not title:
        return _query("calendar", lambda: "Which event, sir?")

    when = diary.parse_date(spoken_date) if spoken_date else None

    if when:
        return _action(
            "remove_event",
            f"Removing {title}, sir.",
            lambda: diary.remove(title, when) > 0,
            detail=f"{title} on {when.isoformat()}",
        )

    _pending_event["title"] = title

    return _ask("calendar", "Which date, sir?", _dated_removal)


def _start_screen_proofread():
    """Check whatever is being written on screen."""
    if not proofread.available():
        return _query(
            "proofread",
            lambda: (
                "I don't have a dictionary installed, sir. "
                "Pyspellchecker would give me one."
            ),
        )

    content, title = screen_control.read_text()

    if not content:
        return _query(
            "proofread",
            lambda: (
                "I can't read any text from that window, sir. "
                "Some applications draw their own."
            ),
        )

    findings, total = proofread.check_text(content)

    label = title or "the screen"

    _last_check.update({
        "name": label,
        "findings": findings,
        "total": total,
        "screen_text": content,
    })

    if not findings:
        return _query(
            "proofread",
            lambda: f"{label} looks clean, sir. I checked {total} words.",
        )

    count = len(findings)
    word = "mistake" if count == 1 else "mistakes"

    question = (
        f"I found {phrases.number(count)} possible spelling {word} in "
        f"{label}, sir, out of {phrases.number(total)} words. "
        "Shall I list them, copy a corrected version, or write a report?"
    )

    return _ask("proofread", question, _proofread_answer)


def _copy_corrected():
    """Put the corrected text on the clipboard for pasting."""
    content = _last_check.get("screen_text")
    findings = _last_check.get("findings")

    if not content:
        return "There's no screen text to correct, sir."

    fixed = proofread.for_clipboard(
        proofread.corrected_text(content, findings)
    )

    if fixed == proofread.for_clipboard(content):
        return "Nothing there I can correct with confidence, sir."

    if not clipboard.write(fixed):
        return "I couldn't reach the clipboard, sir."

    changed = sum(1 for f in findings or () if f["suggestions"])

    return (
        f"The corrected text is on your clipboard, sir. "
        f"{phrases.number(changed)} words changed. "
        "Paste it over the original."
    )


def _start_proofread(name):
    """Check a file and ask what to do about what was found."""
    if not proofread.available():
        return _query(
            "proofread",
            lambda: (
                "I don't have a dictionary installed, sir. "
                "Pyspellchecker would give me one."
            ),
        )

    findings, total = proofread.check(name)

    _last_check.update({
        "name": name,
        "findings": findings,
        "total": total,
        "screen_text": None,
    })

    if findings is None:
        return _query(
            "proofread",
            lambda: f"I couldn't read a file called {name}, sir.",
        )

    if not findings:
        return _query(
            "proofread", lambda: proofread.describe(name, findings, total)
        )

    summary = proofread.describe(name, findings, total)

    # More than a handful is unusable by voice, so a report is offered too.
    question = (
        f"{summary} Shall I list them, fix them, or write a report?"
    )

    return _ask("proofread", question, _proofread_answer)


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


_RUN_TASK = re.compile(
    r"^(?:run|start|execute|do)\s+(?:the\s+|my\s+)?(.+?)"
    r"(?:\s+task)?$"
)


def _run_task_request(text):
    """The task a spoken phrase refers to, or None.

    Gated on the task actually existing: the phrase is only ever
    matched against names already written in tasks.txt, so a
    mishearing can pick the wrong task from your own list at worst,
    never invent a command.
    """
    match = _RUN_TASK.match(text)

    if not match:
        return None

    return tasks.find(match.group(1).strip())


# "forget the markets report pattern" -- gated on the word "pattern",
# the same way _READ_FILE_EXPLICIT is gated on "file", so this can only
# ever match a genuine attempt to name one, never something else that
# happens to start with "forget".
_FORGET_PATTERN = re.compile(
    r"^forget\s+(?:the\s+|about\s+the\s+)?(.+?)\s+pattern$"
)


def _forget_pattern_request(text):
    """The label someone's trying to forget, or None.

    Matches loosely against what's actually stored -- "forget the
    markets pattern" should work even if the stored label is "the
    markets report pattern", since asking for the exact stored wording
    defeats the point of a short spoken label. Returns the real stored
    label so the caller always removes by an exact match, never a
    fuzzy guess.
    """
    match = _FORGET_PATTERN.match(text)

    if not match:
        return None

    said = match.group(1).strip()

    if not said:
        return None

    for pattern in patterns._entries():
        label = pattern.get("label", "")
        # Compare against the label with "the"/"pattern" stripped, so
        # "forget the markets pattern" matches a stored "the markets
        # report pattern" without needing the exact wording.
        bare = label.replace("the ", "", 1).replace(" pattern", "").strip()

        if said in bare or bare in said:
            return label

    return None


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
    # calendar.txt and notes.txt genuinely exist in the JARVIS folder,
    # so without this "what's in my calendar" resolves to a real file
    # and gets read out raw -- Outlook GUIDs, tab separators and all --
    # instead of going to the skill that knows how to say it.
    "calendar", "my calendar", "diary", "my diary",
    "notes", "my notes",
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

# "Image"-worded phrasing used to live here too, but hide_image's own
# phrase list (checked earlier, via the main lookup) now catches "close
# image" / "hide the image" precisely and routes it through the
# picker-aware logic in _hide_image(). Leaving duplicate entries here
# would be unreachable dead code that misleadingly suggests this set
# still handles them — this set is reached only when nothing earlier
# matched, so from here down it only ever means the news headline photo
# or the camera, exactly matching the picture/photo-vs-image wording
# convention used throughout.
_HIDE_PICTURE = frozenset({
    "hide the picture", "close the picture",
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


# Ways of referring to what is on screen rather than to a file.
_SCREEN_WORDS = frozenset({
    "screen", "my screen", "the screen", "this", "what im writing",
    "what i am writing", "my writing", "this window", "the window",
    "here", "what im typing", "what i am typing",
})

_PROOFREAD_REQUEST = re.compile(
    r"^(?:proofread|proof read|spell check|spellcheck|check the spelling"
    r"(?: in| of)?|check|review)\s+(?:my\s+|the\s+)?(.+?)"
    r"(?:\s+(?:file|document|for spelling|for mistakes|for errors|"
    r"spelling|spellings))?$"
)

_ADD_EVENT = re.compile(
    rf"^(?:{_ADD_VERBS}|schedule|book|put|create)\s+(.+?)\s+"
    r"(?:to|in|on|into)\s+(?:my\s+|the\s+)?(?:calendar|diary|schedule)"
    r"(?:\s+(?:for|on|at)\s+(.+))?$"
)

_REMOVE_EVENT = re.compile(
    r"^(?:remove|delete|cancel|take off|drop)\s+(.+?)\s+"
    r"(?:from|off)\s+(?:my\s+|the\s+)?(?:calendar|diary|schedule)"
    r"(?:\s+(?:for|on)\s+(.+))?$"
)


def _event_request(text):
    """Return ("add"|"remove", title, spoken date) or None."""
    match = _ADD_EVENT.match(text)

    if match:
        return "add", match.group(1).strip(), (match.group(2) or "").strip()

    match = _REMOVE_EVENT.match(text)

    if match:
        return "remove", match.group(1).strip(), (match.group(2) or "").strip()

    return None


# "tell me when bitcoin moves 2 percent", and the ways people say it.
# "write me a report on bitcoin", and the ways people ask for one.
_MARKET_REPORT = re.compile(
    r"^(?:write|give|make|prepare|do)\s*(?:me)?\s*"
    r"(?:a|an|the)?\s*(?:market\s+)?report\s+"
    r"(?:on|for|about)\s+(?:the\s+)?(.+?)"
    r"(?:\s+for\s+.+)?$"
)


def _report_request(text):
    """The coin a report was asked about, or None."""
    match = _MARKET_REPORT.match(text)

    if not match:
        return None

    return markets.coin_for(match.group(1))


_SET_ALERT = re.compile(
    r"^(?:tell me|let me know|alert me|warn me|shout)\s+"
    r"(?:when|if)\s+(?:the\s+)?(.+?)\s+"
    r"(?:moves?|changes?|shifts?|goes|swings?)\s+"
    # Normalising strips the decimal point, so "0.5" arrives as "0 5" and
    # both halves have to be caught.
    r"(?:by\s+)?(\d+)(?:\s+(\d+))?\s*(?:percent|per cent|%)?$"
)


def _alert_request(text):
    """Return (coin, percent) for a threshold change, or None."""
    match = _SET_ALERT.match(text)

    if not match:
        return None

    coin = markets.coin_for(match.group(1))

    if not coin:
        return None

    whole = match.group(2)
    fraction = match.group(3)

    try:
        percent = float(f"{whole}.{fraction}" if fraction else whole)
    except ValueError:
        return None

    return coin, percent


_REMEMBER = re.compile(
    r"^(?:remember(?: that)?|keep in mind(?: that)?|"
    r"dont forget(?: that)?|bear in mind(?: that)?)\s+(.+)$"
)

_FORGET = re.compile(
    r"^(?:forget(?: about| that)?|stop remembering)\s+(.+)$"
)


_BARE_PREFERENCE = re.compile(
    r"^(?:i (?:prefer|like|want)|give me|keep)\s+"
    r"(?:short|brief|concise|medium|normal|long|detailed|full)\s+"
    r"(?:answers|replies|responses)$", re.I
)

_BARE_FACT = re.compile(
    r"^(?:my name is|call me|i live in|"
    r"i(?:m|'m| am)?\s*based in|i(?:m|'m| am) in|"
    r"i(?:m|'m| am) from|i come from|"
    r"my (?:default|main|current) project is|"
    r"my calendar is|my home is|my city is|"
    r"my location is|i work (?:at|for))\s+.+$",
    re.I,
)


def _memory_request(text):
    """Return ("remember"|"forget", value) or None."""
    # Stated plainly, without "remember" in front.
    if _BARE_PREFERENCE.match(text) or _BARE_FACT.match(text):
        return "remember", text.strip()

    match = _REMEMBER.match(text)

    if match:
        value = match.group(1).strip()

        # "remember to call mum in ten minutes" is a reminder, not a fact.
        if value and not _TIMER_HINT.search(value):
            return "remember", value

    match = _FORGET.match(text)

    if match:
        value = match.group(1).strip()

        if value and value not in _NOT_FILENAMES:
            return "forget", value

    return None


_TIMER_HINT = re.compile(
    r"\b(?:in|after)\s+\w+\s+(?:second|minute|hour|day)s?\b", re.I
)


_IGNORE_WORD = re.compile(
    r"^(?:ignore|add)\s+(.+?)\s*"
    r"(?:to (?:my |the )?(?:ignore list|dictionary|spelling list))?$"
)


def _proofread_request(text):
    """Resolve a proofread request.

    Returns the file name, or the string "ask" when it is clearly a request
    to proofread but the file is not named usefully. Returning "ask" matters:
    without it "proofread my file" fell through to fuzzy matching and landed
    on "list my files", which are only a word apart.
    """
    match = _PROOFREAD_REQUEST.match(text)

    if not match:
        return None

    name = match.group(1).strip()

    # "proof read my screen" arrives as two words and so misses the exact
    # phrase table; it is still plainly about the screen.
    if name in _SCREEN_WORDS:
        return "screen"

    if not name or name in _NOT_FILENAMES:
        return "ask"

    if _file_target(name):
        return name

    # Named something, but no such file. Still clearly a proofread request.
    return "ask"


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


# Costs a call to Unsplash to execute, same as "look" costs a vision
# call — but is still matched here rather than sent to the LLM, so
# understanding the command itself stays free even though fetching the
# photo is not. "photo"/"picture"/"image" are all accepted for search,
# unlike saving (see save_image's phrase list above), since there is no
# existing command claiming "show me a picture of X" the way "save the
# picture" already claims that wording for the camera.
_IMAGE_SEARCH_PATTERN = re.compile(
    r"^(?:show me |show |find |get me |get |bring up |fetch |"
    r"look up )?(?:an? |the )?(?:image|picture|photo) of (?:the )?(.+)$"
)


def _image_search_request(text):
    """What to search Unsplash for, or None."""
    match = _IMAGE_SEARCH_PATTERN.match(text)

    if not match:
        return None

    query = match.group(1).strip()

    # The pattern only strips a "the" directly after "of"; "of a golden
    # retriever" still has its own leading article to drop, so the panel
    # heading reads "Golden Retriever" rather than "A Golden Retriever".
    query = re.sub(r"^(?:a|an)\s+", "", query)

    return query or None


_ROTATE_PATTERN = re.compile(
    r"^(?:rotate|turn|spin)\s+(?:the\s+|this\s+)?(?:image|picture|photo)"
    r"\s*(.*)$"
)

# Only the counter-clockwise / negative case needs a check at all — the
# absence of one is what "clockwise" or "right" or a bare "rotate the
# image" (positive, the everyday default) fall through to below. Using
# \b word boundaries matters here: "counterclockwise" contains the
# substring "clockwise", the exact class of bug already found once in
# screen_control.py's word matching, so a naive "clockwise" check run
# first would wrongly fire on it too.
_COUNTERCLOCKWISE = re.compile(
    r"\b(?:counter\s*clockwise|anti\s*clockwise|left|widdershins)\b"
)
_NEGATIVE_WORD = re.compile(r"\b(?:minus|negative)\b")


def _rotate_request(text):
    """Degrees to turn the image, positive meaning clockwise, or None."""
    match = _ROTATE_PATTERN.match(text)

    if not match:
        return None

    tail = match.group(1).strip()

    if _COUNTERCLOCKWISE.search(tail) or _NEGATIVE_WORD.search(tail):
        sign = -1
    else:
        # Also true with no direction word at all: a bare "rotate the
        # image" means a plain clockwise quarter turn, the everyday
        # default in every photo app.
        sign = 1

    digits = re.search(r"\d+", tail)
    degrees = int(digits.group()) if digits else 90

    # Only orthogonal turns make sense for a rectangular photo.
    if degrees not in (90, 180, 270):
        degrees = 90

    return sign * degrees


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

    task = _run_task_request(text)

    if task:
        return _blank_result("run_task", text=task["name"])

    pattern_label = _forget_pattern_request(text)

    if pattern_label:
        return _blank_result("forget_pattern", text=pattern_label)

    target = _read_file_request(text)

    if target:
        return _blank_result("read_file", text=target)

    copy_request = _copy_file_request(text)

    if copy_request:
        source, destination = copy_request

        return _blank_result("copy_file", text=source, project=destination)

    event = _event_request(text)

    if event:
        action, title, spoken_date = event

        return _blank_result(
            "add_event" if action == "add" else "remove_event",
            text=_original_case(command, title),
            project=spoken_date or None,
        )

    removal = _remove_request(text)

    if removal:
        what, where = removal

        if where in _NOTE_TARGETS:
            return _blank_result("remove_note", text=what)

        if _file_target(where):
            return _blank_result("remove_line", text=what, project=where)

    reporting = _report_request(text)

    if reporting:
        return _blank_result("market_report", text=reporting)

    alert = _alert_request(text)

    if alert:
        coin, percent = alert

        return _blank_result("set_market_alert", text=coin, amount=percent)

    remembering = _memory_request(text)

    if remembering:
        action, value = remembering

        return _blank_result(action, text=_original_case(command, value))

    note = _note_request(text)

    if note:
        return _blank_result("make_note", text=_original_case(command, note))

    # Before clicking, because "go to" is both a click verb and a browsing
    # verb. _browse_request only answers when the target is plainly a
    # website, so "go to the file menu" still falls through to the click
    # below exactly as it always did.
    destination = _browse_request(text)

    if destination:
        return _blank_result("browse_to", website=destination)

    query = _search_request(text)

    if query:
        return _blank_result(
            "web_search", text=_original_case(command, query)
        )

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

    if text in _HIDE_PICTURE:
        # A headline picture belongs to the news panel and takes priority
        # if it's showing. Otherwise "picture" is exactly as ambiguous as
        # "image" — a fetched Unsplash photo is a picture too — so this
        # shares hide_image's own chain (picker, then a committed image,
        # falling back to the camera last) rather than assuming camera
        # outright.
        if _on_screen:
            return _blank_result("hide_picture")

        return _blank_result("hide_image")

    if _on_screen:
        picture = _picture_request(text)

        if picture:
            return _blank_result("show_picture", amount=picture)

        number = _expand_request(text)

        if number:
            return _blank_result("expand_story", amount=number)

    if text in _HIDE_CHART:
        return _blank_result("hide_chart")

    ignore_match = _IGNORE_WORD.match(text)

    if ignore_match and "ignore" in text:
        word = ignore_match.group(1).strip()

        if word and word not in _NOT_FILENAMES:
            return _blank_result("ignore_word", text=word)

    proof = _proofread_request(text)

    if proof == "screen":
        return _blank_result("proofread_screen")

    if proof == "ask":
        return _blank_result("proofread_which")

    if proof:
        return _blank_result("proofread", text=proof)

    plot = _plot_request(text)

    if plot:
        return _blank_result("plot_chart", text=plot)

    rotate_degrees = _rotate_request(text)

    if rotate_degrees is not None:
        return _blank_result("rotate_image", amount=rotate_degrees)

    image_query = _image_search_request(text)

    if image_query:
        return _blank_result(
            "show_image", text=_original_case(command, image_query)
        )

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
# Intents that change something, so they belong in the journal. Logged in
# one place rather than intent by intent, which is how most of them came to
# be missing.
_WRITE_INTENTS = frozenset({
    "create_file", "append_file", "copy_file", "remove_line",
    "file_to_clipboard", "make_note", "read_notes_removed",
    "remove_note", "clear_notes", "copy_to_clipboard", "clear_clipboard",
    "save_picture", "save_chart", "take_screenshot", "plot_chart",
    "click_thing", "type_text", "open_application", "close_application",
    "open_website", "open_project", "close_project", "set_reminder",
    "cancel_reminders", "set_volume", "mute", "unmute", "toggle_mute",
    "minimise_all", "restore_all", "stop_looking",
    # The read-only browser intents are deliberately absent: they log
    # themselves through journal.browser instead, so visiting a page
    # never becomes the answer to "what did you do?". Saving one is a
    # real artefact on disk, so that one is recorded properly.
    "page_to_file",
    # Same principle applies to the fetched-image feature: show_image
    # costs a call to Unsplash but is not logged as a "did", matching
    # "look" (also absent above) — a search or a view is not a change.
    # Rotating, enlarging, shrinking and restoring are ephemeral view
    # state too, gone the moment the panel closes, so none of those are
    # logged either. save_image is the one that writes a real file.
    "save_image",
    "show_brain", "hide_brain",
    "proofread_fix", "proofread_report", "proofread_copy", "ignore_word",
    "remember", "forget", "set_market_alert", "market_report",
    "add_event", "remove_event", "clear_calendar",
})


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

# How long a question waits for its answer before it's considered
# abandoned rather than still live. Long enough to survive a genuine
# pause — going quiet, the follow-up window expiring, saying "Jarvis"
# again later — since none of that touches _awaiting at all; only a
# real answer, a real interruption, or this timeout ever clears it.
# Short enough that a stray "one" in some unrelated sentence hours later
# can't resurrect a question nobody meant to still be answering.
_AWAITING_TIMEOUT = 300


def _ask(intent, question, handler, recognizes=None):
    """Ask something and hand the next utterance to the handler.

    `recognizes`, if given, is a function taking the next utterance and
    returning True when it clearly answers this specific question. It is
    checked before the usual "does this match an existing command
    instead" safety net below, so a genuine answer that happens to also
    resemble an unrelated command — "select image one" reads equally
    well as a reply to "which one, sir?" and as a click_thing command —
    isn't wrongly stolen by that check. Most callers don't need this;
    it defaults to treating nothing as an unambiguous answer, which
    preserves the exact previous behaviour of always deferring to a
    genuine command match.
    """
    global _awaiting, _pending

    _pending = None
    _awaiting = {
        "intent": intent,
        "handler": handler,
        "recognizes": recognizes,
        "asked_at": time.monotonic(),
    }

    return {
        "kind": "query",
        "intent": intent,
        "response": None,
        "action": lambda: question,
    }


def _resolve_awaiting(text):
    """Give the answer to whatever asked the question, or return None.

    A pending question must not swallow a real command: "list my files"
    while waiting on "shall I list them?" is a new instruction, not an
    answer. Anything the fast path recognises outright takes precedence
    and the question simply lapses — unless the question itself said it
    would clearly recognise this particular reply as its own answer
    (see _ask's `recognizes`), in which case that takes priority instead.
    """
    global _awaiting

    if not _awaiting:
        return None

    if time.monotonic() - _awaiting.get("asked_at", 0) > _AWAITING_TIMEOUT:
        # Abandoned rather than answered — see _AWAITING_TIMEOUT.
        _awaiting = None
        return None

    recognizes = _awaiting.get("recognizes")
    is_a_clear_answer = bool(recognizes and recognizes(text))

    if not is_a_clear_answer and _fast_path(text) is not None:
        _awaiting = None
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


def _start_task(name):
    """Run a defined task, asking first unless it said not to."""
    task = tasks.find(name)

    if not task:
        return _query(
            "run_task",
            lambda: f"I don't have a task called {name}, sir.",
        )

    def _go():
        tasks.run_in_background(task)

        return True

    if tasks.needs_confirmation(task):
        return _confirm(
            "run_task",
            f"Run {task['spoken']}, sir?",
            _go,
            yes_text=f"Running {task['spoken']}, sir.",
            no_text="Very good, sir.",
        )

    return _action(
        "run_task",
        f"Running {task['spoken']}, sir.",
        _go,
        detail=task["spoken"],
    )


def _propose_commit():
    """Read the staged diff, draft a message, and ask before committing.

    Returns a result dict either way -- a plain query when there's
    nothing to do, or a confirmation when there's a real message to
    approve. Nothing is committed until the user actually says yes.
    """
    if not git_tasks.available():
        return _query("propose_commit", git_tasks.no_repo_message)

    if not git_tasks.staged_files():
        return _query(
            "propose_commit",
            lambda: (
                "Nothing's staged, sir. Run git add, then ask me again."
            ),
        )

    diff = git_tasks.staged_diff()
    message = commit_message(diff)

    if not message:
        return _query(
            "propose_commit",
            lambda: (
                "I couldn't draft a message for that, sir. "
                "The console says why."
            ),
        )

    staged = git_tasks.staged_files()
    count = len(staged)
    word = "file" if count == 1 else "files"

    def _do_commit():
        ok, spoken = git_tasks.commit(message)

        journal.action("git_commit", message, ok)

        return ok

    return _confirm(
        "git_commit",
        f"{count} {word} staged, sir. I'd say: {message}. Shall I commit?",
        _do_commit,
        yes_text="Committing, sir.",
        no_text="Very good, sir. Nothing committed.",
    )


def offer_pattern(pattern):
    """Turn a detected pattern into a confirmation question.

    The monitor calls this outside the normal command loop. _confirm()
    stores the pending yes/no action, while the returned query lets the
    Assistant speak the suggestion through the normal speech path.
    """
    if not pattern:
        return None

    label = pattern.get("label")

    if not label:
        return None

    return _confirm(
        "confirm_pattern",
        patterns.spoken_suggestion(pattern),
        lambda: patterns.confirm(label),
        yes_text=f"Certainly, sir. I'll run {label} automatically from now on.",
        no_text=f"Very good, sir. I won't use {label}.",
    )


def run_pattern(pattern):
    """Execute one confirmed automatic pattern using its normal intent."""
    if not pattern:
        return None

    intent = pattern.get("intent")

    if intent == "get_weather":
        return _query(intent, describe_weather)

    if intent == "get_system_status":
        return _query(intent, describe_system)

    if intent == "show_news":
        return _query(
            intent,
            lambda: _show_news(news.DEFAULT_REGION),
        )

    if intent == "read_log":
        return _query(intent, journal.describe)

    if intent == "log_summary":
        return _query(intent, journal.summary)

    if intent == "read_notes":
        return _query(intent, notes.describe)

    if intent == "read_calendar":
        return _query(intent, diary.describe)

    # market_report needs a coin to produce a useful report, but the current
    # pattern record only stores the intent and time, not the original coin.
    # Do not guess one silently.
    if intent == "market_report":
        subject = pattern.get("subject")

        if not subject:
            return None

        def build_report():
            path, sections = market_report.write(subject)

            return market_report.describe(subject, path, sections)

        spoken = markets.COINS.get(subject, {}).get("spoken", subject)

        return _query(
            intent,
            build_report,
            detail=f"a report on {spoken}",
        )

    return None


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


# Commands arrive from two places now: the voice loop at the desk, and
# the phone server on its own thread. Everything below relies on module
# state -- _awaiting, _pending, the pronoun subject -- so two commands
# running at once could let one answer the other's pending question, or
# leave "it" pointing at the wrong thing. Serialising them is the whole
# fix; a command is short enough that waiting is no hardship, and one
# genuinely finishing before the next starts is what the state assumes.
_command_lock = threading.RLock()


def handle_command(command):
    with _command_lock:
        return _handle_command(command)


def _handle_command(command):
    command = _resolve_pronouns(command)

    answered = _resolve_awaiting(command)

    if answered is not None:
        return answered

    answered = _resolve_pending(command)

    if answered is not None:
        return answered

    result = _fast_path(command)
    took_free_path = result is not None

    if result is not None:
        print(f"[fast] {result['intent']} (no API call)")

    else:
        candidates = _application_manager.candidates(command)

        try:
            journal.write("model", f"asking about {command!r}")

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

    # Logged here rather than per-branch above, so a command's resolved
    # intent is recorded regardless of whether it took the free path or
    # needed the model -- previously only the free path was logged with
    # its intent at all, which would make any intent that regularly
    # falls through to the model invisible to anything reading history
    # (pattern detection, "what have you done today").
    journal.command(
        command,
        intent,
        took_free_path,
        detail=(
            (result.get("text") or "").strip()
            if intent == "market_report"
            else None
        ),
    )

    application = result.get("application")

    _set_subject(result)

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

    if intent == "make_note" and (verbatim_text or text):
        return _action(
            intent,
            phrases.pick("acknowledge"),
            lambda: notes.add(verbatim_text or text),
        )

    if intent == "remove_note" and text:
        return _query(
            intent,
            lambda: notes.describe_removal(text),
            detail=f"{text} from your notes",
        )

    if intent == "remove_line" and text and project:
        return _query(
            intent,
            lambda: files.describe_removal(project, text),
            detail=f"{text} from {project}",
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

    if intent == "append_file" and (verbatim_text or text) and project:
        return _action(
            intent,
            phrases.pick("added"),
            lambda: files.append(project, verbatim_text or text) is not None,
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

    if intent == "proofread_followup":
        if not _last_check.get("findings"):
            return _query(
                "proofread",
                lambda: "I haven't checked anything yet, sir.",
            )

        return _proofread_answer(command)

    if intent == "proofread_copy":
        return _query(intent, _copy_corrected, detail=_last_check.get("name"))

    if intent == "proofread_screen":
        return _start_screen_proofread()

    if intent == "proofread_which":
        return _ask(
            "proofread_which",
            "Which file shall I check, sir?",
            _proofread_named,
        )

    if intent == "proofread" and text:
        return _start_proofread(text)

    if intent == "ignore_word" and text:
        # What was heard may not be what was said: an unfamiliar name comes
        # back as a familiar word. The right word is almost certainly among
        # the mistakes just found, so it is matched against those.
        wanted = proofread.resolve_spoken(
            verbatim_text or text, _last_check.get("findings")
        )

        def ignore_word():
            if not proofread.ignore(wanted):
                return False

            # Drop it from the pending findings too, or "fix them" would
            # still offer to change the word just excused.
            findings = _last_check.get("findings")

            if findings:
                _last_check["findings"] = [
                    f for f in findings
                    if f["word"].casefold() != wanted.casefold()
                ]

            return True

        return _action(
            intent,
            f"I'll leave {wanted} alone, sir.",
            ignore_word,
            detail=wanted,
        )

    if intent == "plot_chart" and text:
        return _start_plot(text)

    if intent == "hide_chart":
        return _action(intent, "Closing the chart, sir.", _hide_chart)

    if intent == "read_log":
        return _query(intent, journal.describe)

    if intent == "log_summary":
        return _query(intent, journal.summary)

    if intent == "open_default_project":
        default = memory.default_project()

        if not default:
            return _query(
                intent,
                lambda: (
                    "You haven't told me which project is yours, sir. "
                    "Say: remember my default project is JARVIS."
                ),
            )

        return _action(
            "open_project",
            phrases.pick("opening", name=default),
            lambda: _project_manager.open(default),
            detail=default,
        )

    if intent == "add_event" and text:
        return _start_event(verbatim_text or text, project)

    if intent == "remove_event" and text:
        return _start_remove_event(verbatim_text or text, project)

    if intent == "read_calendar":
        return _query(intent, diary.describe)

    if intent == "clear_calendar":
        total = len(diary.events())

        if not total:
            return _query(intent, lambda: "Your calendar is already empty, sir.")

        return _confirm(
            "clear_calendar",
            f"That will remove all {phrases.number(total)} entries, sir. "
            "Are you sure?",
            lambda: diary.clear() >= 0,
            yes_text="Calendar cleared, sir.",
            no_text="Cancelled, sir.",
        )

    if intent == "market_report" and text:
        def build():
            path, sections = market_report.write(text)

            return market_report.describe(text, path, sections)

        spoken = markets.COINS.get(text, {}).get("spoken", text)

        return _query(intent, build, detail=f"a report on {spoken}")

    if intent == "set_market_alert" and text and amount is not None:
        spoken = markets.COINS.get(text, {}).get("spoken", text)

        try:
            percent = abs(float(amount))
        except (TypeError, ValueError):
            percent = None

        if percent is None or not (
            markets.MIN_THRESHOLD <= percent <= markets.MAX_THRESHOLD
        ):
            return _query(
                intent,
                lambda: (
                    f"I can watch for anything between "
                    f"{markets.MIN_THRESHOLD:g} and "
                    f"{markets.MAX_THRESHOLD:g} percent, sir."
                ),
            )

        return _action(
            intent,
            f"I'll tell you when {spoken} moves {percent:g} percent, sir.",
            lambda: markets.set_threshold(text, percent),
            detail=f"{spoken} at {percent:g} percent",
        )

    if intent == "market_summary":
        return _query(intent, markets.describe)

    if intent == "read_market_alerts":
        return _query(intent, markets.describe_thresholds)

    if intent == "recall_memory":
        return _query(intent, memory.describe)

    if intent == "remember" and text:
        def store():
            if memory.remember(verbatim_text or text):
                return True

            return False

        keyed = memory.classify(verbatim_text or text)

        if keyed:
            key, value = keyed
            spoken = f"Noted, sir. Your {key} is {value}."
        else:
            spoken = "I'll remember that, sir."

        return _action(intent, spoken, store, detail=text)

    if intent == "forget" and text:
        def drop():
            removed = memory.forget(text)

            return removed > 0

        return _action(
            intent,
            f"Forgetting {text}, sir.",
            drop,
            detail=text,
        )

    if intent == "look":
        question = (verbatim_text or text or "").strip() or None

        return _query(intent, lambda: _look(question))

    if intent == "stop_looking":
        return _action(intent, "Camera off, sir.", _stop_looking)

    if intent == "show_brain":
        return _action(intent, "Showing you my mind, sir.", _show_brain)

    if intent == "hide_brain":
        return _action(intent, "Hiding it, sir.", _hide_brain)

    if intent == "save_chart":
        def save_chart():
            path = charts.save_last()

            if not path:
                return "There's no chart to save, sir."

            return "Saved to your JARVIS folder, sir."

        return _query(intent, save_chart, detail="a chart")

    if intent in ("save_picture", "save_image"):
        # "Save the picture" and "save the image" now do the same thing:
        # save whichever is actually on screen, rather than requiring
        # the word to match the panel. The journal intent is picked
        # here, at the moment of saving — not from whichever word was
        # said — so "what did you do" always describes what actually
        # happened. If both were ever open at once, the fetched image
        # wins, on the reasoning that asking to save right after
        # rotating or resizing it is the more deliberate, in-progress
        # action of the two.
        if images.has_image():
            return _query("save_image", _save_image, detail="an image")

        return _query("save_picture", _save_picture)

    # The search itself costs a call to Unsplash, same as "look" costs a
    # vision call, so it is never in _WRITE_INTENTS — matching "look",
    # which is not either. Everything from here down is pure local pixel
    # manipulation with no network involved, and only save_image writes
    # anything to disk, which is the one that is logged.
    if intent == "show_image" and (verbatim_text or text):
        # Not wrapped in _query() here: _show_image returns the full
        # dispatch result itself now (either a plain answer, or an
        # _ask() that hands the next utterance to the picker) rather
        # than a bare string for an outer _query to speak.
        return _show_image(verbatim_text or text)

    if intent == "hide_image":
        return _action(intent, "Closing it, sir.", _hide_image)

    if intent == "rotate_image":
        # amount is already numeric-or-None by this point in
        # handle_command (see _to_number above); no need to convert again.
        degrees = int(amount) if amount is not None else 90

        return _query(intent, lambda: _rotate_image(degrees))

    if intent == "enlarge_image":
        return _query(intent, _enlarge_image)

    if intent == "shrink_image":
        return _query(intent, _shrink_image)

    if intent == "restore_image":
        return _query(intent, _restore_image)

    if intent == "click_thing" and text:
        return _click_thing(text)

    if intent == "type_text" and (verbatim_text or text):
        return _type_text(verbatim_text or text)

    if intent == "describe_screen":
        return _query(intent, screen_control.describe)

    # Browsing is read-only for now: go somewhere, read what's there.
    # Nothing below types, clicks, or submits. Each step logs itself
    # through journal.browser, which writes the audit trail without
    # claiming to be the last thing JARVIS "did" — a page visit is not
    # the answer anyone wants to "what did you do?".
    if intent == "browse_to" and website:
        return _query(intent, lambda: browser.navigate(website))

    if intent == "web_search" and (verbatim_text or text):
        wanted = verbatim_text or text

        return _query(intent, lambda: browser.search(wanted))

    if intent == "read_page":
        return _query(intent, browser.describe_page)

    if intent == "page_overview":
        return _query(intent, browser.describe_overview)

    if intent == "current_page":
        return _query(intent, browser.describe_current)

    if intent == "page_to_file":
        return _query(intent, _page_to_file)

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

    if intent == "clear_documents":
        return _query(intent, documents.clear)

    if intent == "list_documents":
        return _query(intent, documents.status)

    if intent == "list_patterns":
        return _query(intent, patterns.describe)

    if intent == "list_tasks":
        return _query(intent, tasks.describe)

    if intent == "run_task" and verbatim_text:
        return _start_task(verbatim_text)

    if intent == "git_status":
        return _query(intent, git_tasks.describe_status)

    if intent == "propose_commit":
        return _propose_commit()

    if intent == "forget_pattern" and verbatim_text:
        label = verbatim_text

        def _do_forget_pattern():
            return f"I've forgotten {label}, sir." if patterns.forget(label) \
                else f"I couldn't find {label}, sir."

        return _query(intent, _do_forget_pattern)

    if intent == "forget_all_patterns":
        return _confirm(
            intent,
            "Forget every pattern I've noticed, sir?",
            lambda: patterns.forget_all() >= 0,
            yes_text="Cleared, sir.",
        )

    if intent == "answer_question":
        if documents.active():
            return _query(
                intent,
                lambda: answer_with_documents(
                    command,
                    documents.context(),
                ),
            )

        return _query(intent, lambda: answer(command))

    return None
