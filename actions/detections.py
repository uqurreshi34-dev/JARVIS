"""What a camera recognises on its own board, and what JARVIS says about it.

The house cam, flashed as "s3detect", runs YOLO26n itself (FEMTO-ML's compiled C; see
tools/femto_build.py) every few seconds, beside its live view, and posts what it found to
POST /camera/seen: names, scores and where each thing is, never the picture. This keeps the
latest report from each camera, in memory only, and:

    says, unasked, what comes into view             "Someone's in view of your house cam, sir."
                                                    "Someone and a cup are in view of your house cam, sir."
                                                    "Three bottles are in view of your house cam, sir."
                                                    (to the phone at once, tapping it opens the camera;
                                                    said aloud at once, even between a question and
                                                    the next -- only not over you while you talk)
    answers, free, what a camera has detected       "what has the house cam detected"
                                                    "is anyone on the house cam"
                                                    (from a look it takes when asked: its last
                                                    report can be from before you pointed it)
    stops and starts saying so                      "mute the camera alerts" / "unmute ..."

Which things are said unasked is JARVIS_DETECT_ANNOUNCE: "all" (the default) or the model's names,
"person,cat"; JARVIS_DETECT_IGNORE names things never said unasked ("sink,dining table": what the
model takes the furniture for), though still answered when asked. A thing is said when it comes
into view, and again only once it has been out of view for AWAY_SECONDS -- so a cup the model
misses for a look or two is not news, but a cup put down and picked up a minute later is -- and
when more of it come into view than were said ("A bottle", then "Three bottles").
"""

import os
import re
import threading
import time

from actions import cameras, files, journal


# Things said unasked when they come into view, by the model's own names; "all" for everything it knows.
ANNOUNCE = tuple(name.strip().casefold() for name in
                 (os.getenv("JARVIS_DETECT_ANNOUNCE") or "all").split(",") if name.strip())

# Never said unasked, by the model's own names.
IGNORE = tuple(name.strip().casefold() for name in
               (os.getenv("JARVIS_DETECT_IGNORE") or "").split(",") if name.strip())

# How long a thing must have been out of view to be news again. The board reports every change
# and, while anything is in view, every 15 seconds, so a thing still there is never this stale.
AWAY_SECONDS = float(os.getenv("JARVIS_DETECT_AWAY") or 30)

# A report older than this is not what the camera sees now.
FRESH_SECONDS = 30.0

# Asked what a camera sees, JARVIS has it take a look and waits this long for it (a look is about
# three seconds). A board that does not answer -- one flashed before 20 October 2026 -- is answered
# from its last report, and not asked again for NO_LOOK_RETRY seconds.
LOOK_WAIT = float(os.getenv("JARVIS_DETECT_LOOK_WAIT") or 8)
NO_LOOK_RETRY = 600.0
# The cameras that have ever reported a detection, one name a line in JARVIS's folder: a board
# reports only when what it sees changes, so after a restart, with nothing new in view, "what has
# my house cam detected" was not known to be the house cam's and was left to the model, which
# said it had no access to the camera feed (20 October 2026).
DETECTING_FILE = "jarvis-detecting-cameras.txt"

# A report is refused past this many things, or with a name longer than this.
MAX_OBJECTS = 50
MAX_LABEL = 40

_lock = threading.Lock()
_looked = threading.Condition(_lock)   # a look taken on request has come in
_latest = {}          # camera -> {"at", "objects", "ms"}
_present = {}         # (camera, label) -> {"last": monotonic time last in view, "said": how many said}
_pending = {}         # sentence waiting to be said -> (camera, {label: count}, monotonic time)
_answered = set()     # sentences an answer has already covered: not said after it
_look_wanted = {}     # camera -> monotonic time JARVIS asked it to look now
_cannot_look = {}     # camera -> monotonic time it last failed to answer a request to look
_detecting = None     # the names in DETECTING_FILE, read on first use
_marks = {}           # camera -> {"at", "objects", "now"}: the newest boxes, from a report or a live view's look
_last_said = {}       # camera -> [labels] last said unasked: what "highlight it" means
_muted = False
_say = None
_phone = None
_on_marks = None      # called with a camera's name when its boxes change (actions/highlight.py)

# Boxes older than this are not where things are now.
MARKS_FRESH_SECONDS = 12.0


def _detecting_path():
    base = files.root()

    return os.path.join(base, DETECTING_FILE) if base else None


def _known_detecting():
    """The cameras known to detect. The caller holds _lock."""
    global _detecting

    if _detecting is None:
        _detecting = set()
        path = _detecting_path()

        try:
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as file:
                    _detecting = {line.strip() for line in file if line.strip()}
        except OSError as error:
            print(f"[JARVIS] could not read {path}: {error}")

    return _detecting


def _remember_detecting(camera):
    """[camera] has reported a detection: kept, so it is known after a restart. The caller holds _lock."""
    known = _known_detecting()

    if camera in known:
        return

    known.add(camera)
    path = _detecting_path()

    try:
        if path:
            with open(path, "w", encoding="utf-8") as file:
                file.write("".join(f"{name}\n" for name in sorted(known)))
    except OSError as error:
        print(f"[JARVIS] could not write {path}: {error}")


def set_listeners(on_say=None, on_phone=None):
    """on_say(text): says an unprompted announcement aloud, through the gate other alerts use -- held while
    a turn is open, said when it ends. on_phone(text, camera): to the phone at once, not held behind the
    desk's turn: tapping it opens [camera]. Without on_phone, on_say is expected to reach the phone too."""
    global _say, _phone
    _say, _phone = on_say, on_phone


def set_marks_listener(listener):
    """listener(camera): a camera's newest boxes have changed (marks(camera) has them)."""
    global _on_marks
    _on_marks = listener


def _keep_marks(camera, objects, now_seen, at):
    """The newest boxes for [camera]. The caller does not hold _lock; the listener is told after."""
    with _lock:
        _marks[camera] = {"at": at, "objects": list(objects),
                          "now": list(now_seen) if now_seen is not None else None}

    listener = _on_marks

    if listener is not None:
        try:
            listener(camera)
        except Exception as error:   # noqa: BLE001 - drawing boxes must never stop a report
            print(f"[JARVIS] could not pass on a camera's boxes: {error}")


def take_marks(name, payload):
    """A look's boxes sent down camera [name]'s live stream (cameras.MARKS_FLAG): {"objects": [what the
    board is sure of], "now": [that look]}. Kept for drawing only -- nothing is said, as a look's report
    to /camera/seen still says it. Returns None, or why refused."""
    camera = cameras.normal_name(name)
    objects = _clean(payload) if camera else None

    if objects is None:
        return "not a report"

    now_seen = _clean({"objects": payload.get("now")}) if payload.get("now") is not None else None
    _keep_marks(camera, objects, now_seen, time.monotonic())
    return None


def marks(name):
    """{"objects", "now", "ago"}: camera [name]'s newest boxes and their age in seconds, or None."""
    with _lock:
        kept = _marks.get(cameras.normal_name(name))

    if kept is None:
        return None

    return {"objects": list(kept["objects"]), "now": list(kept["now"]) if kept["now"] is not None else None,
            "ago": time.monotonic() - kept["at"]}


def detecting_cameras():
    """The cameras ({"name", ...}) that detect: reporting now, or known to from before."""
    with _lock:
        reporting = set(_latest) | (_known_detecting() - set(_cannot_look))

    return [camera for camera in cameras.cameras() if camera["name"] in reporting]


def last_said(name):
    """The things last said unasked of camera [name], newest sentence's, or []."""
    with _lock:
        return list(_last_said.get(cameras.normal_name(name), []))


def _worth_saying(label):
    return ("all" in ANNOUNCE or label in ANNOUNCE) and label not in IGNORE


def _clean(payload):
    """[{"label", "score", "box"}] from a camera's report, or None if it is not one."""
    objects = payload.get("objects") if isinstance(payload, dict) else None

    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        return None

    kept = []

    for item in objects:
        if not isinstance(item, dict):
            return None

        label = item.get("label")
        score = item.get("score")
        box = item.get("box")

        if (not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL
                or isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1):
            return None

        if (not isinstance(box, list) or len(box) != 4
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and -0.01 <= v <= 1.01
                           for v in box)):
            return None

        kept.append({"label": " ".join(label.casefold().split()), "score": float(score),
                     "box": [float(v) for v in box]})

    return kept


def record(name, payload):
    """A camera's report: kept as its latest, and anything worth saying said. Returns None, or why refused.

    "objects" is what the board is sure of (seen over its last few looks: what is announced);
    a look taken because JARVIS asked also carries "fresh": true and "now", what that one look
    saw -- the answer to the question."""
    camera = cameras.normal_name(name)

    if not camera:
        return "no camera named"

    objects = _clean(payload)

    if objects is None:
        return "not a report"

    fresh = isinstance(payload, dict) and payload.get("fresh") is True
    seen_now = _clean({"objects": payload.get("now")}) if fresh else None

    if fresh and seen_now is None:
        return "not a report"

    took = payload.get("ms")
    now = time.monotonic()

    with _lock:
        _remember_detecting(camera)
        before = _latest.get(camera, {})
        _latest[camera] = {"at": now, "objects": objects,
                           "ms": int(took) if isinstance(took, (int, float)) and not isinstance(took, bool) else None,
                           "now": seen_now if fresh else before.get("now"),
                           "looked": now if fresh else before.get("looked")}

        if fresh:
            _cannot_look.pop(camera, None)
            _look_wanted.pop(camera, None)
            _looked.notify_all()
        counts = {}

        for item in objects:
            counts[item["label"]] = counts.get(item["label"], 0) + 1

        due = {}

        for label, count in counts.items():
            seen = _present.get((camera, label))

            # Out of view long enough: news again. A miss of a look or two is not.
            if seen is None or now - seen["last"] > AWAY_SECONDS:
                seen = {"last": now, "said": 0}

            seen["last"] = now
            _present[(camera, label)] = seen

            # More of it than was said -- or, back in view, any -- is said.
            if count > seen["said"] and _worth_saying(label):
                seen["said"] = count
                due[label] = count

        quiet = _muted

    _keep_marks(camera, objects, seen_now, now)

    if not due:
        return None

    with _lock:
        _last_said[camera] = list(due)

    sentence = arrival_words(camera, due)
    journal.alert(sentence)

    with _lock:
        for old in [old for old, (_, _, at) in _pending.items() if now - at > 120]:
            del _pending[old]

        _pending[sentence] = (camera, dict(due), now)

    if not quiet:
        if _phone:
            try:
                _phone(sentence, camera)
            except Exception as error:   # noqa: BLE001 - the phone failing must not stop it being said
                print(f"[JARVIS] could not send a sighting to the phone: {error}")

        if _say:
            _say(sentence)

    return None


def _yours(camera):
    words = camera.split()

    if words and words[0] == "my":
        words[0] = "your"
    elif words and words[0] not in ("your", "our", "the"):
        words.insert(0, "your")

    return " ".join(words)


def arrival_words(camera, arrived):
    """What JARVIS says when the things in [arrived] ({label: how many}, or one label) come into view of
    [camera], someone first: "Someone's in view of your house cam, sir.", "A cup is in view of ...",
    "Someone and two cups are in view of ..."."""
    if isinstance(arrived, str):
        arrived = {arrived: 1}

    order = sorted(arrived, key=lambda label: (label != "person", label))
    parts = []

    for label in order:
        count = arrived[label]

        if label == "person":
            parts.append("someone" if count == 1 else f"{_NUMBERS.get(count, count)} people")
        else:
            parts.append(_counted(label, count))

    if parts == ["someone"]:
        return f"Someone's in view of {_yours(camera)}, sir."

    said = _listed(parts)
    verb = "is" if len(parts) == 1 and arrived[order[0]] == 1 else "are"
    return f"{said[:1].upper() + said[1:]} {verb} in view of {_yours(camera)}, sir."


def _article(word):
    return "an" if word[:1] in "aeiou" else "a"


_IRREGULAR = {"person": "people", "mouse": "mice", "knife": "knives", "sheep": "sheep", "skis": "skis",
              "scissors": "scissors", "bus": "buses", "wine glass": "wine glasses", "couch": "couches",
              "sandwich": "sandwiches", "toothbrush": "toothbrushes", "hair drier": "hair driers"}

_NUMBERS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


def _counted(label, count):
    if count == 1:
        return f"{_article(label)} {label}"

    plural = _IRREGULAR.get(label) or (label + "es" if label.endswith(("s", "x", "ch", "sh")) else label + "s")
    return f"{_NUMBERS.get(count, count)} {plural}"


def _listed(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def answered(sentence):
    """True, once, if [sentence] is a sighting an answer has already covered: not to be said."""
    with _lock:
        if sentence in _answered:
            _answered.discard(sentence)
            return True

        _pending.pop(sentence, None)
        return False


def latest(name):
    """{"objects", "ms", "ago"} -- camera [name]'s latest report and its age in seconds -- or None."""
    with _lock:
        report = _latest.get(cameras.normal_name(name))

    if report is None:
        return None

    return {"objects": list(report["objects"]), "ms": report["ms"], "ago": time.monotonic() - report["at"],
            "now": list(report["now"]) if report.get("now") is not None else None,
            "looked": report.get("looked")}


def look_asked_at(name):
    """When JARVIS asked [name] to take a look now (time.monotonic()), or None: what ends its live view,
    whose stream otherwise keeps the board from asking whether anything is wanted."""
    with _lock:
        return _look_wanted.get(cameras.normal_name(name))


def look_wanted(name):
    """Whether JARVIS has asked [name] to take a look now (told to it on its next /camera/wanted)."""
    with _lock:
        return cameras.normal_name(name) in _look_wanted


def look_now(camera):
    """What [camera] sees now, as a sentence: from a look it takes on being asked, waited for up to
    LOOK_WAIT -- "Nothing is in view" was said of a laptop already held up (19 October 2026),
    because the board reports only once it is sure of a thing. A board that cannot look on
    request, or one that does not answer in time, is answered from its last report.

    Every camera is asked, until one fails to answer: the first version waited only on cameras that had
    already answered a request -- and so never asked any (20 October 2026)."""
    name = camera["name"]
    asked = time.monotonic()

    with _looked:
        failed = _cannot_look.get(name)

        if failed is None or asked - failed > NO_LOOK_RETRY:
            _look_wanted[name] = asked
            answered = _looked.wait_for(lambda: (_latest.get(name, {}).get("looked") or 0) >= asked,
                                        timeout=LOOK_WAIT)
            _look_wanted.pop(name, None)

            # Only a board that has never answered a request is taken for one that cannot: one that has
            # answered and missed one -- its live view open, so it never heard the request -- is asked
            # again the next time, not answered from its steady report for ten minutes (20 October 2026).
            if not answered and not _latest.get(name, {}).get("looked"):
                _cannot_look[name] = time.monotonic()

    return summary(camera, since=asked)


def summary(camera, since=None):
    """What [camera] ({"name", ...}) has detected, as a sentence. With [since], what a look taken
    after then saw, if there is one, rather than only what the board is sure of."""
    report = latest(camera["name"])
    who = _yours(camera["name"])

    if report is None:
        return (f"{who[:1].upper() + who[1:]} hasn't reported anything it recognised, sir. Its detection runs when it"
                f" is flashed as s3detect.")

    if report["ago"] > FRESH_SECONDS:
        minutes = int(report["ago"] // 60)
        when = f"{minutes} minute{'s' if minutes != 1 else ''} ago" if minutes else f"{int(report['ago'])} seconds ago"
        return f"{who[:1].upper() + who[1:]} last reported {when}, sir; its detection may have stopped."

    counts = {}
    looked = since is not None and report.get("looked") is not None and report["looked"] >= since
    objects = report["now"] if looked else report["objects"]

    for item in sorted(objects, key=lambda item: -item["score"]):
        counts[item["label"]] = counts.get(item["label"], 0) + 1

    # A sighting still waiting to be said that this answer covers -- you asked as the cup came
    # into view, and were told -- is not said again straight after it (19 October 2026).
    with _lock:
        for sentence, (where, said, _) in list(_pending.items()):
            if where == camera["name"] and all(counts.get(label, 0) >= n for label, n in said.items()):
                _answered.add(sentence)
                del _pending[sentence]

    if not counts:
        return f"Nothing {who} recognises is in view, sir."

    seen = _listed([_counted(label, count) for label, count in counts.items()])
    return f"{who[:1].upper() + who[1:]} sees {seen}, sir."


_ASKING = re.compile(r"\b(?:detect|detected|detecting|detects|spotted|spot|spotting|recognise|recognised|recognize"
                     r"|recognized|anyone|anybody|someone|somebody)\b")


def asked(text):
    """The camera [text] asks what it has detected -- "what has the house cam detected", "is anyone on the
    house cam" -- or None."""
    words = " ".join(re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold().replace("'", "")).split())

    if not _ASKING.search(words):
        return None

    known = cameras.cameras()

    with _lock:
        reporting = set(_latest) | (_known_detecting() - set(_cannot_look))

    # Only a camera that detects -- one reporting now, or one that has reported before and still
    # answers a request to look: "is anyone on the street", asked of a camera that only takes
    # pictures, is left to be answered as before.
    for camera in sorted(known, key=lambda camera: -len(camera["name"])):
        if camera["name"] not in reporting:
            continue

        for target in sorted(cameras._targets(camera, len(known) == 1), key=len, reverse=True):
            if re.search(rf"\b{re.escape(target)}\b", words):
                return camera

    return None


_MUTE = re.compile(r"^(?:jarvis\s+)?(?:please\s+)?(?:mute|silence|stop|turn\s+off|switch\s+off|disable)\s+(?:the\s+)?"
                   r"(?:camera|cam|detection|person|people)\s+(?:alerts|alarms|announcements)(?:\s+off)?(?:\s+please)?$"
                   r"|^(?:the\s+)?camera\s+alerts\s+off$")
_UNMUTE = re.compile(r"^(?:jarvis\s+)?(?:please\s+)?(?:unmute|start|turn\s+on|switch\s+on|enable|resume)\s+(?:the\s+)?"
                     r"(?:camera|cam|detection|person|people)\s+(?:alerts|alarms|announcements)(?:\s+please)?$"
                     r"|^(?:the\s+)?camera\s+alerts\s+on$")


def muting_asked(text):
    """True for "mute the camera alerts", False for "unmute the camera alerts", None for anything else."""
    words = " ".join(re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold()).split())

    if _MUTE.match(words):
        return True

    if _UNMUTE.match(words):
        return False

    return None


def set_muted(muted):
    """Stop (True) or start (False) saying unasked what the cameras see. Returns what to say."""
    global _muted

    with _lock:
        _muted = bool(muted)

    return ("Camera alerts muted, sir. I'll still note what they see." if muted
            else "Camera alerts back on, sir.")


def forget():
    """Drop every report. For tests."""
    global _muted, _detecting

    with _lock:
        _latest.clear()
        _present.clear()
        _pending.clear()
        _answered.clear()
        _look_wanted.clear()
        _cannot_look.clear()
        _marks.clear()
        _last_said.clear()
        _muted = False
        _detecting = None


cameras.set_marks_listener(take_marks)
