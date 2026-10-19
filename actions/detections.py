"""What a camera recognises on its own board, and what JARVIS says about it.

The house cam, flashed as "s3detect", runs YOLO26n itself (FEMTO-ML's compiled C; see
tools/femto_build.py) every few seconds, beside its live view, and posts what it found to
POST /camera/seen: names, scores and where each thing is, never the picture. This keeps the
latest report from each camera, in memory only, and:

    says, unasked, when someone comes into view     "Someone's in view of your house cam, sir."
                                                    (to the room and the phone, as other alerts)
    answers, free, what a camera has detected       "what has the house cam detected"
                                                    "is anyone on the house cam"
    stops and starts saying so                      "mute the camera alerts" / "unmute ..."

Which things are worth saying unasked is JARVIS_DETECT_ANNOUNCE (default "person"); each is
said at most once every ANNOUNCE_GAP seconds for each camera, so someone standing in view is
not announced every five seconds.
"""

import os
import re
import threading
import time

from actions import cameras, journal


# Things said unasked when they come into view, by the model's own names.
ANNOUNCE = tuple(name.strip().casefold() for name in
                 (os.getenv("JARVIS_DETECT_ANNOUNCE") or "person").split(",") if name.strip())

# Each thing is said again for a camera only after this long.
ANNOUNCE_GAP = float(os.getenv("JARVIS_DETECT_ANNOUNCE_GAP") or 120)

# A report older than this is not what the camera sees now.
FRESH_SECONDS = 30.0

# A report is refused past this many things, or with a name longer than this.
MAX_OBJECTS = 50
MAX_LABEL = 40

_lock = threading.Lock()
_latest = {}          # camera -> {"at", "objects", "ms"}
_announced = {}       # (camera, label) -> monotonic time last said
_muted = False
_say = None


def set_listeners(on_say=None):
    """on_say(text): speaks an unprompted announcement (and sends it to the phone)."""
    global _say
    _say = on_say


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
    """A camera's report: kept as its latest, and anything worth saying said. Returns None, or why refused."""
    camera = cameras.normal_name(name)

    if not camera:
        return "no camera named"

    objects = _clean(payload)

    if objects is None:
        return "not a report"

    took = payload.get("ms")
    now = time.monotonic()

    with _lock:
        before = _latest.get(camera)
        was = {item["label"] for item in before["objects"]} if before else set()
        _latest[camera] = {"at": now, "objects": objects,
                           "ms": int(took) if isinstance(took, (int, float)) and not isinstance(took, bool) else None}
        arrived = [label for label in ANNOUNCE if label in {item["label"] for item in objects} and label not in was]
        due = []

        for label in arrived:
            said = _announced.get((camera, label))

            if said is None or now - said >= ANNOUNCE_GAP:
                _announced[(camera, label)] = now
                due.append(label)

        quiet = _muted

    for label in due:
        sentence = arrival_words(camera, label)
        journal.alert(sentence)

        if _say and not quiet:
            _say(sentence)

    return None


def _yours(camera):
    words = camera.split()

    if words and words[0] == "my":
        words[0] = "your"
    elif words and words[0] not in ("your", "our", "the"):
        words.insert(0, "your")

    return " ".join(words)


def arrival_words(camera, label):
    """What JARVIS says when [label] comes into view of [camera]."""
    if label == "person":
        return f"Someone's in view of {_yours(camera)}, sir."

    return f"{_article(label).capitalize()} {label} is in view of {_yours(camera)}, sir."


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


def latest(name):
    """{"objects", "ms", "ago"} -- camera [name]'s latest report and its age in seconds -- or None."""
    with _lock:
        report = _latest.get(cameras.normal_name(name))

    if report is None:
        return None

    return {"objects": list(report["objects"]), "ms": report["ms"], "ago": time.monotonic() - report["at"]}


def summary(camera):
    """What [camera] ({"name", ...}) has detected, as a sentence."""
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

    for item in sorted(report["objects"], key=lambda item: -item["score"]):
        counts[item["label"]] = counts.get(item["label"], 0) + 1

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
        reporting = set(_latest)

    # Only a camera that detects: "is anyone on the street", asked of a camera that only takes
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
    global _muted

    with _lock:
        _latest.clear()
        _announced.clear()
        _muted = False
