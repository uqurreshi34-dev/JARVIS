"""Cameras on the wifi: ask for a picture, get one back.

An ESP32-CAM keeps nothing and sends nothing on its own. It reports
itself online like any board (sensors.py, so the house hologram draws
its cone), and every few seconds asks JARVIS whether a picture is wanted
(POST /camera/wanted). Only when one is does it take a picture and post
it back (POST /camera). So nothing is captured unless you ask, and the
street is not being recorded: the newest picture from each camera is
kept in memory, never written to disk, and replaced by the next.

Said aloud, and never a model call to show one:

    show me the street / show me the street camera / show me my room cam
    what's happening on the street / what's outside      (described: a model call)

What a camera looks at comes from the house plan: a window that "looks"
at the street names the camera that looks through it.
"""

import re
import threading
import time

from actions import house, sensors


# How long a request for a picture stands. A camera polls every three
# seconds, takes one, and sends it; longer than this and it is not coming.
WAIT_SECONDS = 12.0

# Larger than any JPEG the camera is set to send (about 40 to 120 KB at
# its default size), and small enough that a stray upload cannot fill memory.
MAX_BYTES = 600 * 1024

_JPEG_START = b"\xff\xd8\xff"

_lock = threading.Condition()
_wanted = {}        # name -> monotonic time it was asked for
_frames = {}        # name -> (monotonic time taken, jpeg bytes)


def _name(name):
    return " ".join(str(name or "").strip().casefold().split())


def want(name):
    """Ask camera [name] for a picture."""
    with _lock:
        _wanted[_name(name)] = time.monotonic()


def wanted(name):
    """Called by the camera's poll: is a picture wanted? Answers once per request."""
    name = _name(name)

    with _lock:
        asked = _wanted.get(name)

        if asked is None:
            return False

        if time.monotonic() - asked > WAIT_SECONDS:
            del _wanted[name]
            return False

        del _wanted[name]
        return True


def store(name, data):
    """A picture from camera [name]. Returns None when kept, or why it was refused."""
    if not data:
        return "empty"

    if len(data) > MAX_BYTES:
        return "too large"

    if not data.startswith(_JPEG_START):
        return "not a JPEG"

    with _lock:
        _frames[_name(name)] = (time.monotonic(), bytes(data))
        _lock.notify_all()

    return None


def latest(name, newer_than=None):
    """The newest picture from [name], or None; with [newer_than], only one taken after it."""
    with _lock:
        frame = _frames.get(_name(name))

    if frame is None or (newer_than is not None and frame[0] <= newer_than):
        return None

    return frame[1]


def fetch(name, timeout=WAIT_SECONDS):
    """Ask [name] for a picture and wait for it. The JPEG, or None."""
    name = _name(name)
    asked = time.monotonic()
    want(name)
    deadline = asked + timeout

    with _lock:
        while True:
            frame = _frames.get(name)

            if frame is not None and frame[0] > asked:
                return frame[1]

            left = deadline - time.monotonic()

            if left <= 0:
                _wanted.pop(name, None)
                return None

            _lock.wait(left)


def forget():
    """Drop every request and picture. For tests."""
    with _lock:
        _wanted.clear()
        _frames.clear()


# ---- which cameras there are, and what they are called ---------------------------------------

_CAMERA_WORDS = ("cam", "camera", "webcam")


def cameras():
    """[{"name", "looks", "room", "online"}] for every camera the house plan or the boards know."""
    found = {}
    heard = sensors.known()

    try:
        drawn = house.render()
    except Exception:
        drawn = None

    for room in (drawn or {}).get("rooms") or []:
        for camera in room.get("cameras") or []:
            board = heard.get(camera["name"])
            found[camera["name"]] = {"name": camera["name"], "looks": camera.get("looks") or "",
                                     "room": room["name"],
                                     "online": bool(board and board["seen_ago"] < house.GONE_SECONDS)}

    for name, board in heard.items():
        words = name.split()

        if name not in found and words and words[-1] in _CAMERA_WORDS:
            found[name] = {"name": name, "looks": "", "room": None, "online": board["seen_ago"] < house.GONE_SECONDS}

    return list(found.values())


def _spoken_words(text):
    return re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold().replace("'", "")).split()


def _targets(camera, only_one):
    """What a camera can be called: "the street", "my room cam", "my room camera"; "outside" if it is the only one."""
    names = {camera["name"]}
    stem = " ".join(word for word in camera["name"].split() if word not in _CAMERA_WORDS)

    if stem:
        names |= {f"{stem} {word}" for word in _CAMERA_WORDS}

    if camera["looks"]:
        looks = _spoken_words(camera["looks"])
        names.add(" ".join(looks))
        names |= {" ".join(looks + [word]) for word in _CAMERA_WORDS}

    if only_one:
        names |= {"camera outside", "outside", "outside camera", "outdoor camera"}

    return names


_SHOW = ("show me", "show", "let me see", "bring up", "pull up", "open", "display")
_DESCRIBE = ("whats happening on", "whats happening in", "what is happening on", "what is happening in",
             "whats going on on", "whats going on in", "what is going on on", "what is going on in",
             "whats on", "what is on", "whats in", "what can you see on", "what can you see in",
             "whats going on", "what is going on", "whats happening", "what is happening", "whats",
             "what is", "describe", "look at")
_POLITE = ("jarvis", "please", "hey", "ok", "okay", "can", "you", "could", "would")


def asked(text):
    """("show", camera) or ("describe", camera) for a known camera, or None.

    A camera is named by what it looks at or its own name, straight after
    the words that ask for it, and nothing after: "show me the street",
    "what's happening on the street", "what's outside". With no camera
    known, nothing is claimed, so the PC's own webcam commands are left be.
    """
    words = _spoken_words(text)

    while words and words[0] in _POLITE:
        words = words[1:]

    while words and words[-1] in ("please", "jarvis", "now", "right", "sir"):
        words = words[:-1]

    said = " ".join(words)
    known = cameras()

    if not said or not known:
        return None

    for verbs, kind in ((_SHOW, "show"), (_DESCRIBE, "describe")):
        for verb in sorted(verbs, key=len, reverse=True):
            if not (said == verb or said.startswith(verb + " ")):
                continue

            rest = said[len(verb):].strip()

            for lead in ("me ", "the ", "my ", "on the ", "in the ", "out the ", "out of the "):
                if rest.startswith(lead) and lead != "my ":
                    rest = rest[len(lead):]

            for camera in known:
                if rest in _targets(camera, len(known) == 1):
                    return kind, camera

            # "my room cam": "my" is part of the name.
            continue

    return None


def _called(camera):
    if camera["looks"]:
        return f"the {camera['looks']} camera"

    return camera["name"]


def picture(text):
    """For "show me the street": (what to say, the JPEG or None)."""
    request = asked(text)

    if request is None:
        return None, None

    kind, camera = request

    if not camera["online"]:
        return f"{_first_up(_called(camera))} isn't connected, sir.", None

    frame = fetch(camera["name"])

    if frame is None:
        return f"{_first_up(_called(camera))} didn't send a picture, sir.", None

    if kind == "show":
        looks = camera["looks"]
        return (f"The {looks}, sir." if looks else f"Here's {_called(camera)}, sir."), frame

    return None, frame


def _first_up(text):
    return text[:1].upper() + text[1:]
