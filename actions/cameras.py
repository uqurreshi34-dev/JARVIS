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
    show me all the cameras / show my cameras            (every camera at once)
    what's happening on the street / what's outside      (described: a model call)

What a camera looks at comes from the house plan: a window that "looks"
at the street names the camera that looks through it.
"""

import json
import os
import re
import struct
import threading
import time

from actions import house, sensors


# How long a request for a picture stands. A camera polls every second,
# takes one, and sends it; longer than this and it is not coming.
WAIT_SECONDS = 12.0

# Larger than any JPEG the camera is set to send (about 40 to 120 KB at
# its default size), and small enough that a stray upload cannot fill memory.
MAX_BYTES = 600 * 1024

_JPEG_START = b"\xff\xd8\xff"

_lock = threading.Condition()
_wanted = {}        # name -> monotonic time it was asked for
_frames = {}        # name -> (monotonic time taken, jpeg bytes)
_history = {}       # name -> [(monotonic time taken, jpeg bytes)], oldest first: frame_at

# A camera's pictures of the last this many seconds are kept, so a detection look's boxes can be matched
# with the picture the look was made from (frame_at): the board sends its boxes when the look ends,
# a couple of seconds after it took that picture, and a camera held in the hand has moved since.
HISTORY_SECONDS = 6.0
_polled = {}        # name -> monotonic time it last asked whether a picture was wanted
_live = {}          # name -> monotonic time its live view stops
_leases = {}        # name -> monotonic time the last remote viewer's lease runs out
_live_listener = None

# Live view: while a camera's picture is on screen, the camera keeps one
# encrypted connection open (POST /camera/live, chunked) and sends frame
# after frame down it -- one TLS handshake for the whole view, not one a
# picture, which is what held it to about one a second. JARVIS's own
# authority and token, as everything else; nothing is broadcast on the
# wifi, and nothing is kept but the newest frame, in memory.
#
# A view stops when the picture is put away, and on its own after this
# long (JARVIS_CAMERA_LIVE_SECONDS), so a camera is never left streaming.
LIVE_SECONDS = float(os.getenv("JARVIS_CAMERA_LIVE_SECONDS") or 300)

# A stream that sends nothing for this long has gone; its thread is freed.
# Longer than a camera's own wait for a frame it fails to take (four
# seconds) twice over, since it carries on past two of those in a row.
LIVE_READ_TIMEOUT = 10.0

_FRAME_HEADER = struct.Struct(">I")

# Between two frames a detecting camera (the house cam as s3detect) sends each look's boxes, as
# JSON, its length with the top bit set: what it is sure of and what that look saw, for drawing
# over the picture ("highlight the bottle"). Handed to the marks listener (actions/detections.py),
# never taken for a frame.
MARKS_FLAG = 0x80000000
MAX_MARKS_BYTES = 8 * 1024
_marks_listener = None

# A streaming camera cannot also send its "online" every 15 seconds, so its
# frames say it for it, as often: it stays connected on the plan and the
# sensor panel for as long as it streams.
LIVE_HEARTBEAT_SECONDS = 15.0


def _name(name):
    return " ".join(str(name or "").strip().casefold().split())


def normal_name(name):
    """[name] as cameras are kept: lower case, single spaces."""
    return _name(name)


# A camera's ask (POST /camera/wanted) may wait this long at most for something to be wanted, and looks
# again at least this often while it does (a request is also made known at once: wait_for_change).
ASK_WAIT_MAX = 15.0
ASK_WAIT_STEP = 0.2


def want(name):
    """Ask camera [name] for a picture."""
    with _lock:
        _wanted[_name(name)] = time.monotonic()
        _lock.notify_all()


def wait_for_change(seconds):
    """Wait up to [seconds] for something to be asked of a camera (a picture, its live view, a look)."""
    if seconds > 0:
        with _lock:
            _lock.wait(seconds)


def changed():
    """Something was asked of a camera: a waiting ask hears it now, not at its next look."""
    with _lock:
        _lock.notify_all()


def wanted(name):
    """Called by the camera's poll: is a picture wanted? Answers once per request.

    A poll is the camera saying it is there: it counts as being seen, so a camera is connected the moment
    it asks, not only at its next "online" report -- within a second of JARVIS starting, not fifteen.
    """
    name = _name(name)

    with _lock:
        _polled[name] = time.monotonic()
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

    now = time.monotonic()

    with _lock:
        _frames[_name(name)] = (now, bytes(data))
        kept = _history.setdefault(_name(name), [])
        kept.append(_frames[_name(name)])

        while kept and now - kept[0][0] > HISTORY_SECONDS:
            kept.pop(0)

        _lock.notify_all()

    return None


def frame_at(name, when):
    """The picture from [name] taken nearest [when] (time.monotonic()) in the last HISTORY_SECONDS, or
    None."""
    with _lock:
        kept = list(_history.get(_name(name), ()))

    if not kept:
        return None

    return min(kept, key=lambda frame: abs(frame[0] - when))[1]


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


# ---- live view -------------------------------------------------------------------------------

def watch(name, seconds=None):
    """Ask camera [name] for live view, until stop_watching() or [seconds] (LIVE_SECONDS) pass."""
    with _lock:
        _live[_name(name)] = time.monotonic() + (LIVE_SECONDS if seconds is None else seconds)
        _lock.notify_all()


def stop_watching(name=None):
    """End live view for camera [name], or for every camera."""
    with _lock:
        if name is None:
            _live.clear()
        else:
            _live.pop(_name(name), None)


def live_wanted(name):
    """Whether camera [name] should be streaming now: shown on the desk, or watched from the phone.

    Called by its poll and by its stream.
    """
    name = _name(name)
    now = time.monotonic()

    with _lock:
        for views in (_live, _leases):
            until = views.get(name)

            if until is not None and now >= until:
                del views[name]

        return name in _live or name in _leases


# ---- watching from the phone -----------------------------------------------------------------

# A phone watching a camera holds a lease on its live view, renewed while
# it is being sent frames; a phone that goes away (locked, out of signal,
# the page closed) stops renewing, and the camera stops streaming this long
# after. Closing the picture on the desk does not end a phone's view.
REMOTE_LEASE_SECONDS = 10.0

# Frames sent to a phone at most this often (JARVIS_REMOTE_CAMERA_FPS): a
# camera streams 25 a second on the wifi, and a phone on mobile data does
# not need them all.
REMOTE_FPS = float(os.getenv("JARVIS_REMOTE_CAMERA_FPS") or 12)

# A camera that sends no frame for this long while watched has gone.
REMOTE_FRAME_WAIT = 15.0

_BOUNDARY = "jarvisframe"
REMOTE_MIMETYPE = f"multipart/x-mixed-replace; boundary={_BOUNDARY}"


def _renew(name, seconds):
    _leases[name] = max(_leases.get(name, 0.0), time.monotonic() + seconds)


def lease(name, seconds=REMOTE_LEASE_SECONDS):
    """Keep camera [name] live for [seconds] more, for a viewer elsewhere."""
    with _lock:
        _renew(_name(name), seconds)
        _lock.notify_all()


def remote_frames(name, fps=None, wait=REMOTE_FRAME_WAIT):
    """Camera [name]'s live view as a multipart JPEG stream (MJPEG), for a phone's <img>.

    Yields each new frame, no more than [fps] a second, renewing the lease
    while it waits and as it sends; ends when the camera sends nothing
    for [wait] seconds. The phone going away ends the generator, and with
    it the renewals.
    """
    name = _name(name)
    gap = 1.0 / max(0.5, fps or REMOTE_FPS)
    sent_at = None
    sent_taken = None

    while True:
        deadline = time.monotonic() + wait

        with _lock:
            while True:
                _renew(name, REMOTE_LEASE_SECONDS)
                frame = _frames.get(name)

                if frame is not None and (sent_taken is None or frame[0] > sent_taken):
                    break

                left = deadline - time.monotonic()

                if left <= 0:
                    return

                _lock.wait(min(left, REMOTE_LEASE_SECONDS / 2))

        taken, jpeg = frame
        sent_taken = taken

        if sent_at is not None:
            pause = gap - (time.monotonic() - sent_at)

            if pause > 0:
                time.sleep(pause)

        sent_at = time.monotonic()
        yield (f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
               .encode("ascii") + jpeg + b"\r\n")


def listed():
    """[{"name", "looks", "online"}] of the cameras, for the phone to offer."""
    return [{"name": camera["name"], "looks": camera["looks"], "online": camera["online"]} for camera in cameras()]


def beside(board):
    """{"name", "title"} of an online camera in the room board [board] is in on the house plan, or None.

    What an alarm from that board offers to show: movement in my room opens the room cam.
    """
    try:
        room = house.room_of(board)
    except Exception:
        return None

    if not room:
        return None

    for camera in cameras():
        if camera["room"] == room and camera["online"] and camera["name"] != normal_name(board):
            return {"name": camera["name"], "title": camera["name"].upper()}

    return None


def set_live_listener(listener):
    """Register a callable taking (name, jpeg) for each live frame: the panel's update."""
    global _live_listener
    _live_listener = listener


def set_marks_listener(listener):
    """Register a callable taking (name, payload dict) for each look's boxes sent down a live stream."""
    global _marks_listener
    _marks_listener = listener


def _read_exactly(stream, count):
    data = b""

    while len(data) < count:
        piece = stream.read(count - len(data))

        if not piece:
            return None

        data += piece

    return data


def receive_live(name, stream, look_asked_at=None):
    """Take frames from camera [name]'s live stream until the view ends, the stream does, or a frame is bad.

    Each frame is its length (4 bytes, big-endian) then the JPEG. Returns
    (frames kept, seconds streamed, why it ended).

    [look_asked_at], given a name, says when the camera was asked to take a detection look, or None. A look
    asked for after this stream began ends it: while it streams the board asks nothing, so it would never
    hear the request; it asks the moment the stream ends, and comes back to live view straight after.
    """
    started = time.monotonic()
    count = 0
    ended = "the view was closed"
    heard = None

    while live_wanted(name):
        asked = look_asked_at(name) if look_asked_at is not None else None

        if asked is not None and asked >= started:
            ended = "a detection look was asked for"
            break

        try:
            header = _read_exactly(stream, _FRAME_HEADER.size)
        except OSError:
            header = None

        if header is None:
            ended = "the camera stopped sending"
            break

        (length,) = _FRAME_HEADER.unpack(header)

        if length & MARKS_FLAG:
            # A look's boxes, not a frame (see MARKS_FLAG).
            size = length & ~MARKS_FLAG

            if not 0 < size <= MAX_MARKS_BYTES:
                ended = f"a look's boxes said they were {size} bytes"
                break

            try:
                marks = _read_exactly(stream, size)
            except OSError:
                marks = None

            if marks is None:
                ended = "the camera stopped part way through a look's boxes"
                break

            listener = _marks_listener

            if listener is not None:
                try:
                    payload = json.loads(marks.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    payload = None

                try:
                    if isinstance(payload, dict):
                        listener(_name(name), payload)
                except Exception as error:
                    print(f"[JARVIS] could not take a look's boxes: {error}")

            continue

        if not 0 < length <= MAX_BYTES:
            ended = f"a frame said it was {length} bytes"
            break

        try:
            frame = _read_exactly(stream, length)
        except OSError:
            frame = None

        if frame is None:
            ended = "the camera stopped part way through a frame"
            break

        refused = store(name, frame)

        if refused:
            ended = f"a frame was {refused}"
            break

        count += 1
        now = time.monotonic()

        with _lock:
            _polled[_name(name)] = now

        if heard is None or now - heard >= LIVE_HEARTBEAT_SECONDS:
            heard = now
            sensors.report({"name": name, "event": "online"})

        listener = _live_listener

        if listener is not None:
            try:
                listener(_name(name), frame)
            except Exception as error:
                print(f"[JARVIS] could not show a live frame: {error}")

    return count, time.monotonic() - started, ended


def is_camera(name):
    """Whether board [name] is a camera: it has asked this run whether a picture is wanted.

    Told by what the board does, not by its name, so the sensor panel can
    draw a camera as a camera within a second of it starting.
    """
    with _lock:
        return _name(name) in _polled


def picture_ago(name):
    """Seconds since camera [name] last sent a picture, or None if it has not this run."""
    with _lock:
        frame = _frames.get(_name(name))

    return None if frame is None else time.monotonic() - frame[0]


def forget():
    """Drop every request and picture. For tests."""
    with _lock:
        _wanted.clear()
        _frames.clear()
        _history.clear()
        _polled.clear()
        _live.clear()
        _leases.clear()


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

    with _lock:
        polled = {name: time.monotonic() - when for name, when in _polled.items()}

    def online(name):
        board = heard.get(name)
        return bool((board and board["seen_ago"] < house.GONE_SECONDS)
                    or polled.get(name, house.GONE_SECONDS) < house.GONE_SECONDS)

    for room in (drawn or {}).get("rooms") or []:
        for camera in room.get("cameras") or []:
            found[camera["name"]] = {"name": camera["name"], "looks": camera.get("looks") or "",
                                     "room": room["name"], "online": online(camera["name"])}

    for name in list(heard) + list(polled):
        words = name.split()

        if name not in found and words and words[-1] in _CAMERA_WORDS:
            found[name] = {"name": name, "looks": "", "room": None, "online": online(name)}

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

    # "my room cam" is also "the room cam" and "room cam": whose it is, or "the", is said either way, and
    # "show me the room cam" asked for this camera, not the PC's own webcam (12 October 2026). And the
    # other way: "house cam" is also "my house cam" -- said so, it opened the laptop's webcam instead
    # (19 October 2026).
    for name in list(names):
        for owner in _OWNERS:
            if name.startswith(owner + " "):
                names.add(name[len(owner) + 1:])

    for name in list(names):
        if not any(name.startswith(owner + " ") for owner in _OWNERS):
            names |= {f"{owner} {name}" for owner in _OWNERS}

    return names


# Words before a camera's name that say whose it is, not which it is.
_OWNERS = ("my", "our", "your", "the")


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


def by_view(text, camera):
    """Whether [text] asked for [camera] by what it looks at ("show me the street"), not by its own name
    ("show me my room cam") -- so the answer uses the words that were asked."""
    looks = _spoken_words(camera["looks"])
    return bool(looks) and set(looks) <= set(_spoken_words(text))


def title(text, camera):
    """The panel's title for [camera]'s picture: what was asked for ("STREET CAM", "MY ROOM CAM")."""
    return f"{camera['looks'].upper()} CAM" if by_view(text, camera) else camera["name"].upper()


_CLOSE = ("close", "hide", "dismiss", "shut", "put away", "turn off", "get rid of")


def dismissed(text):
    """The camera [text] asks to put away -- a closing word, then a camera by its name or what it looks at:
    "close my room cam", "hide the street" -- or None. Matched before any program's name, so "close my
    room cam" is never taken for an application to quit."""
    words = _spoken_words(text)

    while words and words[0] in _POLITE:
        words = words[1:]

    while words and words[-1] in ("please", "jarvis", "now", "sir"):
        words = words[:-1]

    said = " ".join(words)
    known = cameras()

    for verb in sorted(_CLOSE, key=len, reverse=True):
        if not said.startswith(verb + " "):
            continue

        rest = said[len(verb):].strip()

        for lead in ("the ", "down ", "off "):
            if rest.startswith(lead):
                rest = rest[len(lead):]

        for camera in known:
            if rest in _targets(camera, len(known) == 1):
                return camera

    return None


def closed_words(text, camera):
    """What JARVIS says having put [camera]'s picture away, in the words asked."""
    return f"Closed {_called(camera, by_view(text, camera))}, sir."


def _called(camera, view=True):
    if camera["looks"] and view:
        return f"the {camera['looks']} camera"

    # A name is in your words ("my room cam"); said back to you, it is yours: "your room cam". One named
    # without an owner ("house cam") is yours all the same: "your house cam".
    words = camera["name"].split()

    if not words:
        return camera["name"]

    if words[0] not in _OWNERS:
        return " ".join(["your"] + words)

    return " ".join(["your" if words[0] == "my" else words[0]] + words[1:])


def picture(text):
    """For "show me the street": (what to say, the JPEG or None)."""
    request = asked(text)

    if request is None:
        return None, None

    kind, camera = request
    view = by_view(text, camera)

    if not camera["online"]:
        return f"{_first_up(_called(camera, view))} isn't connected, sir.", None

    # Shown, it stays live until put away; one camera live at a time. Described, one picture is enough.
    if kind == "show":
        stop_watching()
        watch(camera["name"])

    frame = fetch(camera["name"])

    if frame is None:
        stop_watching(camera["name"])
        return f"{_first_up(_called(camera, view))} didn't send a picture, sir.", None

    if kind == "show":
        # In the words asked: "show me the street" -> "The street, sir."; "show me my room cam" -> "Here's my
        # room cam, sir." -- the camera may be on the desk, not at its window.
        return (f"The {camera['looks']}, sir." if view else f"Here's {_called(camera, view)}, sir."), frame

    return None, frame


def _first_up(text):
    return text[:1].upper() + text[1:]


def here_words(text, camera):
    """What JARVIS says showing [camera], in the words asked: "The street, sir." or "Here's your room cam, sir." """
    view = by_view(text, camera)
    return f"The {camera['looks']}, sir." if view else f"Here's {_called(camera, view)}, sir."


# ---- every camera at once ---------------------------------------------------------------------

# "Show me all the cameras", "show my cameras", "show every camera": always more than one -- plural, or
# "all" or "every" -- so "show me the camera" and "open the camera" stay the PC's own webcam.
_EVERY = (r"(?:all\s+(?:of\s+)?(?:the\s+|my\s+|our\s+)?(?:cameras|cams|camera\s+feeds|feeds)"
          r"|(?:the\s+|my\s+|our\s+)?(?:cameras|cams|camera\s+feeds)"
          r"|every\s+(?:camera|cam))")
_ALL_SHOWN = re.compile(r"^(?:show\s+me|show|let\s+me\s+see|bring\s+up|pull\s+up|open|display)\s+" + _EVERY + "$")
_ALL_CLOSED = re.compile(r"^(?:" + "|".join(verb.replace(" ", r"\s+") for verb in _CLOSE) + r")\s+" + _EVERY + "$")


def _plain(text):
    words = _spoken_words(text)

    while words and words[0] in _POLITE:
        words = words[1:]

    while words and words[-1] in ("please", "jarvis", "now", "right", "sir"):
        words = words[:-1]

    return " ".join(words)


def all_asked(text):
    """Whether [text] asks to see every camera at once: "show me all the cameras", "show my cameras"."""
    return bool(_ALL_SHOWN.match(_plain(text)))


def all_dismissed(text):
    """Whether [text] puts every camera away: "close all the cameras", "hide my cameras"."""
    return bool(_ALL_CLOSED.match(_plain(text)))


def online():
    """The cameras connected now, in the order cameras() gives them."""
    return [camera for camera in cameras() if camera["online"]]


def gone():
    """The cameras that have reported themselves this run and are not connected now: unplugged, or off the
    wifi. Not a camera the house plan only draws, which no board has yet claimed."""
    heard = sensors.known()
    return [camera for camera in cameras() if not camera["online"] and camera["name"] in heard]


def watch_all(names, seconds=None):
    """Live view for every camera in [names] at once -- the grid -- and for no other."""
    with _lock:
        _live.clear()

    for name in names:
        watch(name, seconds)
