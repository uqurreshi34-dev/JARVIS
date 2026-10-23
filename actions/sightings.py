"""Sightings: when someone comes into view of a camera that detects, what it saw is kept as evidence.

The house cam tells JARVIS when it sees a person (actions/detections.py). From that moment until they
have been gone QUIET_SECONDS (a look saw nobody), JARVIS has the camera stream (a lease, as a phone
watching it has: the desk's panel is not opened) and keeps, in its own folder under JARVIS's sightings
folder:

    frame-0001.jpg ...   the camera's own pictures, as it sent them: never re-encoded, cropped or drawn on,
                         at most FPS a second -- the "native format" UK police ask for
    still-01.jpg ...     larger pictures (the house cam's 1600 x 1200) asked for as it begins and every
                         STILL_EVERY_SECONDS, as the camera sent them
    best.jpg             the best picture of the person: a copy of one of those frames or stills
    best-cutout.png      the person alone, cut out (actions/outline.py), when the model is sure of the
                         shape; else best-crop.jpg, what is in their box. Made from best.jpg, for you to
                         look at: the frames are the evidence
    sighting.json        when it began and ended (the PC's clock, with its offset from UTC), the camera,
                         what each look saw, and every file's SHA-256, so any later change to one shows
    index.html           the frames played in order, in any browser, with nothing to install

Each sighting begun, ended, kept, viewed or deleted is a line in the folder's sightings-log.txt. One
JARVIS was closed during is finished when it next starts (finish_unfinished), from its pictures. A
sighting is deleted after KEEP_DAYS unless kept ("keep the last sighting"), and the oldest not kept go first when all of them
pass MAX_GB. Sightings are made whether or not the camera
alerts are muted: muting stops JARVIS saying so, not keeping the evidence.

Which things begin one is JARVIS_SIGHTING_LABELS, the model's own names ("person", the default).
Once you have asked "learn my face" (actions/known_faces.py), a sighting's record says whether it was
you; no one else is named, and nothing of a stranger's face is kept.

    keep the last sighting / keep that sighting      not deleted after KEEP_DAYS
    show me the last sighting                         its best picture, on the panel, and when it was
    learn my face / forget my face                    the newest sighting is you / JARVIS knows no one
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from io import BytesIO

from actions import cameras, detections, faces, files, known_faces, outline

FOLDER = "sightings"
LOG_FILE = "sightings-log.txt"
RECORD_FILE = "sighting.json"

# What begins a sighting, by the model's own names.
LABELS = tuple(name.strip().casefold() for name in
               (os.getenv("JARVIS_SIGHTING_LABELS") or "person").split(",") if name.strip())

# Gone this long, the sighting ends. The board keeps a person in what it is sure of while seen in this
# look or the last, so a look's miss does not end one. Gone means a look since saw nobody: no looks at
# all is not gone (JARVIS just started, the wifi dropped -- 22 October 2026 a sighting ended 10 s in,
# while the board could not reach JARVIS for as long, and a second began when it could).
QUIET_SECONDS = float(os.getenv("JARVIS_SIGHTING_QUIET") or 10)

# No looks at all for QUIET_SECONDS and this much more (the board off, or out of reach): it ends.
SILENT_MORE_SECONDS = 30.0

# However long someone stays, a sighting ends after this; if they are still there, a new one begins.
MAX_SECONDS = 120.0

# Kept this many days unless kept for good. 31: what UK police guidance takes as a usual retention.
KEEP_DAYS = float(os.getenv("JARVIS_SIGHTING_DAYS") or 31)

# All sightings together kept under this many gigabytes: past it, the oldest not kept go first. A camera
# facing a street sees many people pass, at a few megabytes each.
MAX_GB = float(os.getenv("JARVIS_SIGHTING_MAX_GB") or 5)

# Pictures kept a second at most: the camera streams up to 25, and five tell what happened.
FPS = 5.0

# The camera is kept streaming this long at a time, renewed while the sighting lasts: one that ends,
# or a JARVIS that stops, lets the camera go back to detection alone within this.
LEASE_SECONDS = 6.0

# How often the sighting's pictures are collected and its end looked for.
TICK_SECONDS = 0.5

# The best picture of the person: the most of them in view (their box's area), the model surest it is
# them, and sharp. Sharpness is the spread of the picture's edges (the variance of its Laplacian);
# measured 21 October 2026 on people cropped from a photo at the house cam's size: 2,500 to 3,400
# sharp, 300 to 430 dark, 15 to 45 blurred by moving 6 to 12 pixels. Weighed as s / (s + SHARP_HALF):
# a sharp picture keeps nearly all its worth, a blurred one under a third.
SHARP_HALF = 100.0

# A still -- a picture larger than live view's, from a board that can take one (the house cam: 1600 x
# 1200, against live view's 640 x 480) -- is asked for when a sighting begins and every this many
# seconds while it lasts. Each costs a second or two of live view (the board stops streaming to take it),
# and a face in one is two and a half times as many pixels across: what makes someone known again.
STILL_EVERY_SECONDS = 10.0

# Stills are off unless JARVIS_SIGHTING_STILLS is "on". Taking one means changing the camera's size while
# it runs, and on the house cam (ESP32-S3, OV3660, esp32-camera) that broke it: half-drawn stills, live
# view that stopped, and twice in a minute a crash inside the camera driver itself (esp_camera_fb_get ->
# cam_verify_jpeg_eoi, reading a scrambled pointer) on the next ordinary frame, and a restart (22 October
# 2026). The frames, chosen by face, are the sighting's pictures; the board takes a still only if asked.
STILLS = (os.getenv("JARVIS_SIGHTING_STILLS") or "off").strip().casefold() == "on"

# A still mostly blank is not kept: the camera, changing to the larger size, sometimes sends one with
# only part of it drawn (green and black, the rest blank: 5 of 8 on 22 October 2026, which showed
# something in 14% to 36% of their columns; the good ones, 92% to 100% -- a plain wall in view shows
# little). A column shows something when its values vary by more than STILL_COLUMN_SPREAD; a still must
# have STILL_COLUMNS_AT_LEAST of them.
STILL_COLUMN_SPREAD = 1.0
STILL_COLUMNS_AT_LEAST = 0.6

# A person's box for cutting them out, made from their face in the best picture itself (the board's
# boxes are from looks seconds apart, and someone moving is elsewhere by then): this many face widths
# either side of the face, from just above it to eight faces below, tightest first. On the house cam's
# dark pictures (21 October 2026) a wider box took in the dark door behind him and SAM was as sure of
# that; 1.3 cut off his arms. The first SAM is sure of is kept.
BODY_FACE_WIDTHS = (1.5, 1.8, 2.2)

# A look's boxes are matched with a kept picture taken within this of the look.
FRAME_MATCH_SECONDS = 1.0

# Once you have taught JARVIS your face: "Someone's in view" is said at once, as ever, and the sighting's
# first pictures are checked for you; when SEEN_IN_AT_LEAST of them are, GREETING follows. Checked for
# GREET_WAIT_SECONDS after the first picture comes, and at most GREET_WAIT_MOST after the look (the
# stream takes a few seconds to start); after that, nothing more is said. Never held: an alert about a
# stranger must not wait on a check for you (it did, for one evening: 22 October 2026).
GREET_WAIT_SECONDS = 8.0
GREET_WAIT_MOST = 20.0
GREETING = "It's you, sir. Welcome back."

_lock = threading.Lock()
_open = {}          # camera -> the sighting being made
_last = None        # folder of the newest sighting ended this run, for "the last sighting"
_worker = None
_STARTED = time.time()   # folders older than this, with no record, were left by a JARVIS since closed
_finishing = threading.Lock()   # one finish_unfinished at a time, so no folder is finished twice


def _now_text(monotonic=None):
    """The PC's local time, with its offset from UTC, of [monotonic] (time.monotonic()), or now."""
    wall = time.time() - (time.monotonic() - monotonic if monotonic is not None else 0.0)
    return _wall_text(wall)


def _wall_text(wall):
    """The PC's local time, with its offset from UTC, of [wall] (time.time())."""
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec="milliseconds")


def folder(make=True):
    """JARVIS's sightings folder, made if missing (with [make]); or None."""
    base = files.root()

    if not base:
        return None

    path = os.path.join(base, FOLDER)

    if not make:
        return path if os.path.isdir(path) else None

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not make the sightings folder: {error}")
        return None

    return path


def _log(text):
    path = folder()

    if not path:
        return

    try:
        with open(os.path.join(path, LOG_FILE), "a", encoding="utf-8") as log:
            log.write(f"{_now_text()}  {text}\n")
    except OSError as error:
        print(f"[JARVIS] could not write the sightings log: {error}")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---- beginning, keeping, ending -------------------------------------------------------------------------

def _seen(camera):
    """A camera's boxes changed (detections): begin or continue a sighting, and offer its best picture."""
    kept = detections.marks(camera)

    if kept is None:
        return

    sure = [item for item in kept["objects"] if item["label"] in LABELS]
    this_look = kept["now"] if kept["now"] is not None else kept["objects"]
    looked = [item for item in this_look if item["label"] in LABELS]
    now = time.monotonic()
    at = now - kept["ago"]

    with _lock:
        sighting = _open.get(camera)

        if sighting is None and not sure:
            return

        if sighting is None:
            sighting = _begin(camera, now)

            if sighting is None:
                return

        sighting["last_look"] = now

        if sure or looked:
            sighting["last_seen"] = now
            sighting["nobody_seen"] = False
        else:
            sighting["nobody_seen"] = True

        sighting["looks"].append({"at": _now_text(at), "seen": [dict(item) for item in this_look]})

        if looked:
            report = detections.latest(camera)
            took = (report or {}).get("ms")
            when = at - (took / 1000.0 if took else 0.0)

            # Only a look's own boxes say where someone was when its picture was taken. A report of
            # what the board is sure of can carry a box from an earlier look -- paired with the
            # picture of its own time, it chose a picture of the door after he had walked off
            # (21 October 2026) -- so those are kept for a sighting no look of its own saw.
            if kept["now"] is not None:
                sighting["candidates"].append((when, looked))
            else:
                sighting["reported"].append((when, looked))

    _start_worker()


def _begin(camera, now):
    """A new sighting on [camera] (the caller holds _lock), or None if it cannot be kept."""
    base = folder()

    if not base:
        return None

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", camera.casefold()).strip("-") or "camera"
    path = os.path.join(base, f"{slug}-{stamp}")
    number = 2

    while os.path.exists(path):
        path = os.path.join(base, f"{slug}-{stamp}-{number}")
        number += 1

    try:
        os.makedirs(path)
    except OSError as error:
        print(f"[JARVIS] could not begin a sighting: {error}")
        return None

    sighting = {"camera": camera, "folder": path, "began": now, "began_text": _now_text(now), "last_seen": now,
                "last_look": now, "nobody_seen": False,
                "frames": [], "frame_times": [], "after": now - cameras.HISTORY_SECONDS, "looks": [],
                "candidates": [], "unmatched": [], "reported": [], "best": None, "stills": [],
                "faces": [], "face_queue": []}
    _open[camera] = sighting
    cameras.lease(camera, LEASE_SECONDS)
    if STILLS:
        cameras.want_still(camera)

    sighting["still_asked"] = now
    _log(f"began  {os.path.basename(path)}  ({camera})")
    print(f"[JARVIS] sighting on {camera}: keeping what it sees ({os.path.basename(path)})", flush=True)
    return sighting


def still_shown(rgb):
    """How much of still [rgb] is drawn: the share of its columns whose values vary (0 to 1)."""
    if rgb is None:
        return 0.0

    import numpy as np

    return float((rgb.astype(np.float32).std(axis=(0, 2)) > STILL_COLUMN_SPREAD).mean())


def _still(camera, jpeg):
    """A still from [camera] (cameras.take_still): kept beside the sighting's frames, as it came."""
    with _lock:
        sighting = _open.get(camera)

    if sighting is None:
        return

    rgb = outline.picture(jpeg)
    shown = still_shown(rgb)

    if shown < STILL_COLUMNS_AT_LEAST:
        sighting["stills_broken"] = sighting.get("stills_broken", 0) + 1
        print(f"[JARVIS] sighting on {camera}: a still came broken ({shown:.0%} of it drawn); not kept",
              flush=True)
        return

    name = f"still-{len(sighting['stills']) + 1:02d}.jpg"

    try:
        with open(os.path.join(sighting["folder"], name), "wb") as out:
            out.write(jpeg)
    except OSError as error:
        print(f"[JARVIS] sighting on {camera}: could not keep a still: {error}")
        return

    height, width = rgb.shape[:2] if rgb is not None else (0, 0)
    sighting["stills"].append({"file": name, "at": _now_text(), "sha256": _sha256(jpeg),
                               "width": int(width), "height": int(height)})
    print(f"[JARVIS] sighting on {camera}: a still, {width} x {height}", flush=True)


def _face_look(camera, jpeg, score=None):
    """A look's own picture with a face in it (cameras.take_face): kept beside the frames as it came
    (look-01.jpg ...), and checked at the next tick for who it is -- seconds before live view's
    first frames (the board sends it straight after the look that saw them)."""
    with _lock:
        sighting = _open.get(camera)

    if sighting is None:
        return

    name = f"look-{len(sighting['faces']) + 1:02d}.jpg"

    try:
        with open(os.path.join(sighting["folder"], name), "wb") as out:
            out.write(jpeg)
    except OSError as error:
        print(f"[JARVIS] sighting on {camera}: could not keep a look's picture: {error}")
        return

    entry = {"file": name, "at": _now_text(), "sha256": _sha256(jpeg)}

    if score is not None:
        entry["face_finder"] = round(float(score), 2)   # the board's own face finder's score

    with _lock:
        sighting["faces"].append(entry)
        sighting["face_queue"].append(jpeg)

    _start_worker()


def _start_worker():
    global _worker

    with _lock:
        if _worker is not None and _worker.is_alive():
            return

        _worker = threading.Thread(target=_work, name="jarvis-sightings", daemon=True)
        _worker.start()


def _work():
    while True:
        with _lock:
            cameras_open = list(_open)

        if not cameras_open:
            return

        for camera in cameras_open:
            try:
                _tick(camera)
            except Exception as error:   # noqa: BLE001 - one bad picture must not end the evidence
                print(f"[JARVIS] sighting on {camera}: {error}")

        time.sleep(TICK_SECONDS)


def _tick(camera):
    now = time.monotonic()

    with _lock:
        sighting = _open.get(camera)

    if sighting is None:
        return

    gone = sighting["nobody_seen"] and now - sighting["last_seen"] > QUIET_SECONDS
    silent = now - sighting["last_look"] > QUIET_SECONDS + SILENT_MORE_SECONDS
    ending = gone or silent or now - sighting["began"] > MAX_SECONDS

    if not ending:
        cameras.lease(camera, LEASE_SECONDS)

        if STILLS and now - sighting.get("still_asked", now) >= STILL_EVERY_SECONDS:
            cameras.want_still(camera)
            sighting["still_asked"] = now

    fresh = []

    # The camera's pictures since the last kept, at most FPS a second; the first ones from just before.
    for taken, jpeg in cameras.frames_since(camera, sighting["after"]):
        sighting["after"] = taken

        if sighting["frame_times"] and taken - sighting["frame_times"][-1] < 1.0 / FPS:
            continue

        name = f"frame-{len(sighting['frames']) + 1:04d}.jpg"

        try:
            with open(os.path.join(sighting["folder"], name), "wb") as out:
                out.write(jpeg)
        except OSError as error:
            print(f"[JARVIS] sighting on {camera}: could not keep a picture: {error}")
            continue

        sighting["frames"].append({"file": name, "at": _now_text(taken), "sha256": _sha256(jpeg)})
        sighting["frame_times"].append(taken)
        fresh.append(jpeg)

    _greet(sighting, fresh, now)

    with _lock:
        candidates, sighting["candidates"] = sighting["candidates"], []

    for when, looked in candidates:
        if not _consider(sighting, when, looked):
            sighting["unmatched"].append((when, looked))

    if ending:
        with _lock:
            _open.pop(camera, None)

        _end(sighting)


def sharpness(rgb):
    """The variance of the Laplacian of picture [rgb] (height, width, 3): high when sharp."""
    import numpy as np

    grey = rgb.astype(np.float32).mean(axis=2)

    if grey.shape[0] < 3 or grey.shape[1] < 3:
        return 0.0

    edges = (4 * grey[1:-1, 1:-1] - grey[:-2, 1:-1] - grey[2:, 1:-1] - grey[1:-1, :-2] - grey[1:-1, 2:])
    return float(edges.var())


def best_shot(score, box, rgb):
    """How good a picture of someone is: (worth, sharpness). [box] is 0 to 1 of picture [rgb]."""
    height, width = rgb.shape[:2]
    x1, y1, x2, y2 = (int(max(0.0, min(1.0, v)) * size) for v, size in zip(box, (width, height, width, height)))

    if x2 - x1 < 3 or y2 - y1 < 3:
        return 0.0, 0.0

    sharp = sharpness(rgb[y1:y2, x1:x2])
    return score * (x2 - x1) * (y2 - y1) * sharp / (sharp + SHARP_HALF), sharp


def _consider(sighting, when, looked, strict=True):
    """Each person this look saw, in the kept picture taken when it looked: the best so far kept. Returns
    whether a picture was near enough in time ([strict]: within FRAME_MATCH_SECONDS; else the nearest)."""
    if not sighting["frame_times"]:
        return False

    nearest = min(range(len(sighting["frame_times"])), key=lambda i: abs(sighting["frame_times"][i] - when))
    apart = abs(sighting["frame_times"][nearest] - when)

    if strict and apart > FRAME_MATCH_SECONDS:
        return False

    frame = sighting["frames"][nearest]

    try:
        with open(os.path.join(sighting["folder"], frame["file"]), "rb") as kept:
            jpeg = kept.read()
    except OSError:
        return False

    rgb = outline.picture(jpeg)

    if rgb is None:
        return False

    for item in looked:
        worth, sharp = best_shot(item["score"], item["box"], rgb)

        if sighting["best"] is None or worth > sighting["best"]["worth"]:
            sighting["best"] = {"file": frame["file"], "at": frame["at"], "label": item["label"],
                                "score": item["score"], "box": list(item["box"]), "worth": worth,
                                "sharpness": round(sharp, 1), "seconds_from_look": round(apart, 2)}

    return True


def hold_arrival(camera, arrived, sentence, deliver):
    """detections' arrival hook: when JARVIS knows your face and one person has come, the sighting's
    first pictures are checked for you, and GREETING said through deliver(text) if they are. Always
    False: [sentence] ("Someone's in view") is said at once, by detections, never held."""
    likeness = known_faces.known().get(known_faces.YOU) if known_faces.present() else None

    if arrived != {"person": 1} or likeness is None:
        return False

    with _lock:
        sighting = _open.get(camera)

        if sighting is None or (sighting.get("arrival") and not sighting["arrival"]["done"]):
            return False

        sighting["arrival"] = {"sentence": sentence, "deliver": deliver, "looked": time.monotonic(),
                               "first_picture": None, "you": 0, "done": False,
                               "likeness": likeness}

    _start_worker()
    return False


def _say_arrival(arrival, text):
    arrival["done"] = True

    try:
        arrival["deliver"](text)
    except Exception as error:   # noqa: BLE001 - a greeting must never stop the evidence
        print(f"[JARVIS] could not say an arrival: {error}")


def _greet(sighting, fresh, now):
    """Check an arrival's new pictures [fresh] for your face: GREETING once enough are you; nothing
    more once the wait is over."""
    arrival = sighting.get("arrival")

    with _lock:
        looked, sighting["face_queue"] = sighting.get("face_queue", []), []

    if not arrival or arrival["done"]:
        return

    if fresh and arrival["first_picture"] is None:
        arrival["first_picture"] = now

    # The board's own pictures with a face in them first (each one), then the newest picture only, each
    # tick (two a second): a face takes about 50 ms to check, and a group in view must not leave it
    # behind the camera.
    if looked and arrival["first_picture"] is None:
        arrival["first_picture"] = now

    for jpeg in looked + fresh[-1:]:
        rgb = outline.picture(jpeg)

        if rgb is None:
            continue

        for face in faces.detect(rgb):
            if faces.facing(face) < known_faces.FACING_AT_LEAST:
                continue

            one = known_faces.fingerprint(rgb, face)

            how_alike = known_faces.alike(one, arrival["likeness"]) if one is not None else 0.0

            # One picture this alike is enough (known_faces.CERTAIN_AT_LEAST): the board's first
            # picture of you greets you before live view has started.
            if how_alike >= known_faces.CERTAIN_AT_LEAST:
                arrival["you"] = max(arrival["you"] + 1, known_faces.SEEN_IN_AT_LEAST)
            elif how_alike >= known_faces.SAME_AT_LEAST:
                arrival["you"] += 1

        if arrival["you"] >= known_faces.SEEN_IN_AT_LEAST:
            sighting["greeted"] = True
            _log(f"greeted you  {os.path.basename(sighting['folder'])}")
            _say_arrival(arrival, GREETING)
            return

    waited_from_picture = arrival["first_picture"] is not None and now - arrival["first_picture"] > GREET_WAIT_SECONDS

    if waited_from_picture or now - arrival["looked"] > GREET_WAIT_MOST:
        arrival["done"] = True


def _end(sighting):
    """Write what is kept beside the pictures: the best one, the person cut out, the record, the player."""
    global _last
    path = sighting["folder"]
    made = []

    # The picture showing their face best, from every one kept, when the face model is there.
    if faces.present():
        by_face = _best_face(sighting)

        if by_face is not None:
            sighting["best"] = by_face

        if sighting.get("prints"):
            sighting["who"] = known_faces.who(sighting["prints"])

        sighting.pop("prints", None)     # a stranger's face is compared and forgotten

    # No look of its own matched a kept picture -- the one look that saw them came before the camera
    # began streaming (21 October 2026: a 14 s sighting with no best picture) -- so a picture near a look
    # or a report of what the board was sure of, then the nearest; its distance in time in the record
    # ("seconds_from_look").
    for strict in (True, False):
        if sighting["best"] is None:
            for when, looked in sighting["unmatched"] + sighting["reported"]:
                _consider(sighting, when, looked, strict=strict)

    best = sighting["best"]

    if best is not None:
        try:
            with open(os.path.join(path, best["file"]), "rb") as kept:
                jpeg = kept.read()

            with open(os.path.join(path, "best.jpg"), "wb") as out:
                out.write(jpeg)

            made.append(("best.jpg", jpeg))
            made.extend(_cut_out(path, jpeg, best))
        except OSError as error:
            print(f"[JARVIS] sighting: could not keep its best picture: {error}")

    ended = time.monotonic()
    record = {
        "camera": sighting["camera"],
        "began": sighting["began_text"],
        "ended": sighting.get("ended_text") or _now_text(ended),
        "seconds": sighting.get("seconds", round(ended - sighting["began"], 1)),
        "kept": False,
        "clock": "this PC's own clock, local time with its offset from UTC",
        "frames": sighting["frames"],
        "stills": sighting["stills"],
        "look_pictures": sighting.get("faces", []),
        "stills_broken": sighting.get("stills_broken", 0),
        "best": {key: value for key, value in best.items() if key != "worth"} if best else None,
        "made_from_best": [{"file": name, "sha256": _sha256(data)} for name, data in made],
        "looks": sighting["looks"],
    }

    if sighting.get("finished_late"):
        record["finished_late"] = sighting["finished_late"]

    # Who it was, when JARVIS knows anyone: {"name", "pictures" alike, "alike" at best}, or None for no
    # one it knows. Nothing else about anyone's face is kept.
    if known_faces.anyone_known():
        record["recognised"] = sighting.get("who")

    if sighting.get("greeted"):
        record["greeted"] = GREETING

    who = (record.get("recognised") or {}).get("name")
    said_who = f"; {who}" if who else ""

    try:
        with open(os.path.join(path, RECORD_FILE), "w", encoding="utf-8") as out:
            json.dump(record, out, indent=1)

        with open(os.path.join(path, "index.html"), "w", encoding="utf-8") as out:
            out.write(_player(record))
    except OSError as error:
        print(f"[JARVIS] sighting: could not write its record: {error}")

    if sighting.get("finished_late"):
        _log(f"finished  {os.path.basename(path)}  ({len(sighting['frames'])} pictures, {record['seconds']} s;"
             f" JARVIS was closed before it ended{said_who})")
        print(f"[JARVIS] sighting on {sighting['camera']} finished: {os.path.basename(path)}, left when JARVIS"
              f" was closed", flush=True)
    else:
        _last = path
        _log(f"ended  {os.path.basename(path)}  ({len(sighting['frames'])} pictures, {record['seconds']} s{said_who})")
        print(f"[JARVIS] sighting on {sighting['camera']} ended: {len(sighting['frames'])} pictures kept",
              flush=True)

    sweep()


_KEPT_PICTURE = re.compile(r"^(?:frame-\d{4}|still-\d{2}|look-\d{2})\.jpg$")


def finish_unfinished():
    """Finish every sighting JARVIS was closed (or stopped) during: its pictures are there, but not its
    best picture, record or player (22 October 2026: two closed mid-sighting, never finished -- and,
    with no record, never deleted either). Each one's times are when its pictures were saved, as the
    record says. Returns how many."""
    base = folder(make=False)

    if not base:
        return 0

    with _finishing:
        return _finish_unfinished(base)


def _finish_unfinished(base):
    with _lock:
        making = {os.path.abspath(sighting["folder"]) for sighting in _open.values()}

    done = 0

    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)

        try:
            if (not os.path.isdir(path) or os.path.abspath(path) in making
                    or os.path.exists(os.path.join(path, RECORD_FILE)) or os.path.getmtime(path) >= _STARTED):
                continue

            pictures = sorted(file for file in os.listdir(path) if _KEPT_PICTURE.match(file))
            began = ended = os.path.getmtime(path)
        except OSError:
            continue

        frames, stills, looks, saved = [], [], [], []

        for file in pictures:
            try:
                with open(os.path.join(path, file), "rb") as kept:
                    data = kept.read()

                at = os.path.getmtime(os.path.join(path, file))
            except OSError:
                continue

            saved.append(at)
            kind = frames if file.startswith("frame-") else (looks if file.startswith("look-") else stills)
            kind.append({"file": file, "at": _wall_text(at), "sha256": _sha256(data)})

        if saved:
            began, ended = min(saved), max(saved)

        # The folder's name is the camera's, made safe for a file name (_begin): as near as it can be had.
        camera = re.sub(r"-\d{4}-\d{2}-\d{2}-\d{6}(?:-\d+)?$", "", name).replace("-", " ")
        sighting = {"camera": camera, "folder": path, "began": time.monotonic(), "began_text": _wall_text(began),
                    "ended_text": _wall_text(ended), "seconds": round(ended - began, 1),
                    "finished_late": "JARVIS was closed before this sighting ended; finished when it next"
                                     " started. Its times are when each picture was saved; no looks are"
                                     " recorded, so its best picture is chosen by face only.",
                    "frames": frames, "stills": stills, "faces": looks, "looks": [], "candidates": [], "unmatched": [],
                    "reported": [], "best": None}

        try:
            _end(sighting)
            done += 1
        except Exception as error:   # noqa: BLE001 - one folder must not stop the rest
            print(f"[JARVIS] could not finish the sighting {name}: {error}")

    return done


_checking = threading.Lock()   # one check_unchecked at a time

# Sightings made before your face was known are checked for it every this many pictures: half of them
# is plenty to find you, and the check takes half as long.
CHECK_EVERY = 2


def check_unchecked(again=False):
    """Check each sighting made before JARVIS knew your face for it, and say in its record whether it
    was you ("recognised", and "recognised_later" with when): asked "any strangers this week", a sighting
    never checked is not a stranger. With [again] (after "learn my face": it knows you better now), those
    found not to be you are checked again too. A stranger's fingerprints are compared and dropped, as at
    the end of a sighting. Returns how many were checked."""
    if not known_faces.anyone_known():
        return 0

    with _checking:
        with _lock:
            making = {os.path.abspath(sighting["folder"]) for sighting in _open.values()}

        checked = 0

        for path, record in _records():
            unchecked = "recognised" not in record or (again and record["recognised"] is None)

            if not unchecked or os.path.abspath(path) in making:
                continue

            prints = []
            pictures = (record.get("frames", [])[::CHECK_EVERY] + record.get("stills", [])
                        + record.get("look_pictures", []))

            for picture in pictures:
                try:
                    with open(os.path.join(path, picture["file"]), "rb") as kept:
                        rgb = outline.picture(kept.read())
                except (OSError, KeyError, TypeError):
                    continue

                if rgb is None:
                    continue

                for face in faces.detect(rgb):
                    if faces.facing(face) >= known_faces.FACING_AT_LEAST:
                        one = known_faces.fingerprint(rgb, face)

                        if one is not None:
                            prints.append(one)

            record["recognised"] = known_faces.who(prints)
            record["recognised_later"] = _now_text()

            try:
                with open(os.path.join(path, RECORD_FILE), "w", encoding="utf-8") as out:
                    json.dump(record, out, indent=1)
            except OSError as error:
                print(f"[JARVIS] could not check an old sighting for you: {error}")
                continue

            checked += 1
            _log(f"checked  {os.path.basename(path)}  ({'you' if record['recognised'] else 'not you'})")

        return checked


def _on_start():
    """Once a start, off the thread that imports this: unfinished sightings finished, old ones checked
    for your face, old ones deleted."""
    try:
        finish_unfinished()
        check_unchecked()
    except OSError as error:
        print(f"[JARVIS] could not finish old sightings: {error}")

    sweep()


def _best_face(sighting):
    """The best picture of someone's face among a sighting's kept pictures (faces.worth), as
    sighting["best"] is kept, or None if no face was found in any."""
    best = None
    knowing = known_faces.anyone_known()
    sighting["prints"] = []

    for frame in sighting["frames"] + sighting["stills"] + sighting.get("faces", []):
        try:
            with open(os.path.join(sighting["folder"], frame["file"]), "rb") as kept:
                rgb = outline.picture(kept.read())
        except OSError:
            continue

        if rgb is None:
            continue

        height, width = rgb.shape[:2]

        for face in faces.detect(rgb):
            worth = faces.worth(rgb, face)

            # Who it was (actions/known_faces.py): each face's fingerprint, kept only until it has been
            # compared, and only if someone is known at all.
            if knowing and faces.facing(face) >= known_faces.FACING_AT_LEAST:
                one = known_faces.fingerprint(rgb, face)

                if one is not None:
                    sighting["prints"].append(one)

            if worth > 0 and (best is None or worth > best["worth"]):
                x1, y1, x2, y2 = face["box"]
                best = {"file": frame["file"], "at": frame["at"], "label": "person", "chosen_by": "face",
                        "score": round(face["score"], 3), "worth": worth,
                        "box": [round(x1 / width, 4), round(y1 / height, 4), round(x2 / width, 4),
                                round(y2 / height, 4)],
                        "face": {"facing": round(faces.facing(face), 2),
                                 "sharpness": round(faces.sharpness(rgb, face), 3)}}

    return best


def _bodies(face_box):
    """Boxes (0 to 1) for the person whose face is [face_box] (0 to 1), tightest first (BODY_FACE_WIDTHS)."""
    x1, y1, x2, y2 = face_box
    across, down, middle = x2 - x1, y2 - y1, (x1 + x2) / 2
    return [(max(0.0, middle - k * across), max(0.0, y1 - 0.3 * down), min(1.0, middle + k * across),
             min(1.0, y1 + 8 * down)) for k in BODY_FACE_WIDTHS]


def _cut_out(path, jpeg, best):
    """The person alone from [jpeg]: [(file, bytes)] written to [path]."""
    rgb = outline.picture(jpeg)

    if rgb is None:
        return []

    found = None

    if outline.present():
        view = outline.embed(rgb)

        if view is not None and best.get("chosen_by") == "face":
            # Their face in this very picture: a body box made from it, tightest first, the face
            # itself given to SAM as a point on them -- it was 0.23 steady from the box alone, 0.90 with.
            x1, y1, x2, y2 = best["box"]
            face_middle = ((x1 + x2) / 2, (y1 + y2) / 2)

            for body in _bodies(best["box"]):
                found = outline.mask(view, body, inside=face_middle)

                if found is not None:
                    best["body"] = [round(v, 4) for v in body]
                    break
        elif view is not None:
            found = outline.mask(view, best["box"])

    out = BytesIO()

    if found is not None:
        outline.cutout(rgb, found).save(out, "PNG")
        name = "best-cutout.png"
    else:
        from PIL import Image

        height, width = rgb.shape[:2]
        x1, y1, x2, y2 = _bodies(best["box"])[1] if best.get("chosen_by") == "face" else best["box"]
        box = (int(x1 * width), int(y1 * height), max(int(x2 * width), int(x1 * width) + 1),
               max(int(y2 * height), int(y1 * height) + 1))
        Image.fromarray(rgb).crop(box).save(out, "JPEG", quality=95)
        name = "best-crop.jpg"

    data = out.getvalue()

    with open(os.path.join(path, name), "wb") as written:
        written.write(data)

    return [(name, data)]


def _player(record):
    """A page that plays a sighting's frames in order, with their times: nothing to install."""
    frames = json.dumps([{"file": frame["file"], "at": frame["at"]} for frame in record["frames"]])
    title = f"{record['camera']} -- {record['began']}"
    return f"""<!doctype html>
<meta charset="utf-8"><title>{title}</title>
<style>body{{background:#111;color:#ddd;font:14px sans-serif;text-align:center}}img{{max-width:100%}}</style>
<h3>{title} ({record['seconds']} s)</h3>
<img id="p" alt=""><p id="t"></p>
<button onclick="go(-1)">&lt;</button> <button onclick="play=!play">play / pause</button>
<button onclick="go(1)">&gt;</button>
<p>{"".join(f'<a href="{still["file"]}"><img src="{still["file"]}" style="width:200px;margin:4px" alt=""></a>' for still in record.get("stills", []))}</p>
<script>
const frames = {frames};
let i = 0, play = true;
function show() {{ if (!frames.length) return; p.src = frames[i].file; t.textContent = frames[i].at + "  (" + (i + 1) + " of " + frames.length + ")"; }}
function go(step) {{ i = (i + step + frames.length) % frames.length; show(); }}
setInterval(() => {{ if (play) go(1); }}, 200);
show();
</script>
"""


# ---- keeping and deleting -----------------------------------------------------------------------------

def _records():
    """[(folder, record)] of every sighting kept, newest first."""
    base = folder(make=False)

    if not base:
        return []

    found = []

    for name in os.listdir(base):
        path = os.path.join(base, name)

        try:
            with open(os.path.join(path, RECORD_FILE), encoding="utf-8") as kept:
                found.append((path, json.load(kept)))
        except (OSError, ValueError):
            continue

    found.sort(key=lambda pair: pair[1].get("began", ""), reverse=True)
    return found


def _size(path):
    total = 0

    for parent, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(parent, name))
            except OSError:
                pass

    return total


def sweep():
    """Delete sightings not kept: older than KEEP_DAYS, then the oldest while all are over MAX_GB.
    Returns how many."""
    cutoff = datetime.now().astimezone() - timedelta(days=KEEP_DAYS)
    base = folder(make=False)
    gone = 0

    if not base:
        return 0

    with _lock:
        making = {os.path.abspath(sighting["folder"]) for sighting in _open.values()}

    def delete(path, why):
        nonlocal gone

        try:
            shutil.rmtree(path)
            gone += 1
            _log(f"deleted  {os.path.basename(path)}  ({why})")
            return True
        except OSError as error:
            print(f"[JARVIS] could not delete an old sighting: {error}")
            return False

    left = []

    for path, record in _records():
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(base) or os.path.abspath(path) in making:
            continue

        try:
            began = datetime.fromisoformat(record["began"])
        except (KeyError, TypeError, ValueError):
            continue

        if not record.get("kept") and began < cutoff:
            delete(path, f"older than {KEEP_DAYS:g} days, not kept")
        else:
            left.append((path, record, _size(path)))

    total = sum(size for _, _, size in left)

    for path, record, size in reversed(left):        # oldest first
        if total <= MAX_GB * 1024 ** 3:
            break

        if not record.get("kept") and delete(path, f"all sightings over {MAX_GB:g} GB, the oldest not kept"):
            total -= size

    return gone


def newest():
    """(folder, record) of the newest sighting kept, or None."""
    found = _records()
    return found[0] if found else None


def keep_last():
    """Keep the newest sighting for good. Returns what to say."""
    found = newest()

    if found is None:
        return "There are no sightings to keep, sir."

    path, record = found

    if record.get("kept"):
        return "That sighting is already kept, sir."

    record["kept"] = True

    try:
        with open(os.path.join(path, RECORD_FILE), "w", encoding="utf-8") as out:
            json.dump(record, out, indent=1)
    except OSError:
        return "I couldn't mark it kept, sir."

    _log(f"kept  {os.path.basename(path)}")
    return f"Kept the sighting from {_spoken_time(record['began'])}, sir. It won't be deleted."


def last_picture_file():
    """(the file of the newest sighting's best picture, its record), or (None, record or None)."""
    found = newest()

    if found is None:
        return None, None

    path, record = found
    name = "best.jpg" if os.path.exists(os.path.join(path, "best.jpg")) else (
        record["frames"][0]["file"] if record.get("frames") else None)
    return (os.path.join(path, name) if name else None), record


def last_picture(where=""):
    """(jpeg, title, what to say) of the newest sighting's best picture, or (None, None, what to say).
    [where] it is viewed, for the log ("on the phone")."""
    picture, record = last_picture_file()

    if record is None:
        return None, None, "There are no sightings yet, sir."

    if picture is None:
        return None, None, "That sighting has no pictures, sir."

    try:
        with open(picture, "rb") as kept:
            jpeg = kept.read()
    except OSError:
        return None, None, "I couldn't open that sighting, sir."

    path = os.path.dirname(picture)
    _log(f"viewed  {os.path.basename(path)}{('  (' + where + ')') if where else ''}")
    seconds = record.get("seconds") or 0
    who = "You were" if (record.get("recognised") or {}).get("name") == known_faces.YOU else "Someone"

    # One still being kept is not the last: it has no best picture yet (19:04, 22 October 2026: asked
    # while in view, and shown the one before without a word).
    with _lock:
        going_on = bool(_open)

    still = " Another is being kept now; ask again when it ends." if going_on else ""
    return (jpeg, f"SIGHTING {record['camera'].upper()}",
            f"{who} on your {record['camera']} at {_spoken_time(record['began'])}, for "
            f"{max(1, round(seconds))} seconds, sir.{still}")


def _spoken_time(text):
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return "an unknown time"

    today = datetime.now().astimezone().date()
    day = "" if when.date() == today else (" yesterday" if when.date() == today - timedelta(days=1)
                                           else f" on {when.strftime('%d %B').lstrip('0')}")
    return f"{when.strftime('%H:%M')}{day}"


_KEEP = re.compile(r"^(?:(?:jarvis|please|now)\s+)*(?:keep|save)\s+(?:the\s+)?(?:last|latest|that|this)\s+"
                   r"(?:sighting|intruder|event)(?:\s+for\s+good)?(?:\s+please)?$")
_SHOW = re.compile(r"^(?:(?:jarvis|please|now|can\s+you|could\s+you)\s+)*(?:show(?:\s+me)?|open|let\s+me\s+see)\s+"
                   r"(?:the\s+)?(?:last|latest)\s+(?:sighting|intruder|event)(?:\s+please)?$")


_LEARN = re.compile(r"^(?:(?:jarvis|please|now|can\s+you|could\s+you)\s+)*learn\s+my\s+face"
                    r"(?:\s+from\s+(?:the\s+)?(?:last|latest)\s+sighting)?(?:\s+please)?$")
_UNLEARN = re.compile(r"^(?:(?:jarvis|please|now)\s+)*forget\s+my\s+face(?:\s+please)?$")


def asked(text):
    """"keep", "show", "learn" (my face), "unlearn", or "search" (a question about the sightings kept:
    actions/sighting_search.py) for a request about sightings, else None."""
    words = " ".join(re.sub(r"[^\w\s']", " ", str(text or "").casefold()).split())

    if _KEEP.match(words):
        return "keep"

    if _SHOW.match(words):
        return "show"

    if _LEARN.match(words):
        return "learn"

    if _UNLEARN.match(words):
        return "unlearn"

    from actions import sighting_search   # it reads the records here, so it is imported when asked

    if sighting_search.asked(text):
        return "search"

    return None


def learn_face():
    """"Learn my face": from the newest sighting. Returns what to say."""
    found = newest()

    if found is None:
        return "There are no sightings to learn your face from yet, sir. Stand in front of the house cam first."

    path, record = found
    said = known_faces.learn(path)

    if not said.startswith("Learned"):
        _log(f"could not learn a face from  {os.path.basename(path)}")
        return said

    _log(f"learned a face from  {os.path.basename(path)}")

    # The sighting learned from is you, by your own word (19:03, 22 October 2026: learned from, and
    # still "Someone" when shown, as it ended before your face was known).
    record["recognised"] = {"name": known_faces.YOU, "learned_from": True}

    try:
        with open(os.path.join(path, RECORD_FILE), "w", encoding="utf-8") as out:
            json.dump(record, out, indent=1)
    except OSError as error:
        print(f"[JARVIS] could not mark the sighting as you: {error}")

    # Every sighting made before your face was known, or not found to be you, checked now, off this thread:
    # learned by lamplight, last night's sightings may be you after all.
    threading.Thread(target=check_unchecked, kwargs={"again": True}, name="jarvis-sightings-check",
                     daemon=True).start()
    return said


def unlearn_face():
    said = known_faces.unlearn()

    if said.startswith("Forgotten"):
        _log("forgot the face it knew")

    return said


def forget():
    """Drop every sighting being made. For tests."""
    global _last

    with _lock:
        _open.clear()

    _last = None


detections.add_marks_listener(_seen)
detections.set_arrival_hold(hold_arrival)
cameras.set_face_listener(_face_look)
cameras.set_still_listener(_still)

# Unfinished sightings finished, old ones deleted, once a start, off the thread that imports this.
threading.Thread(target=_on_start, name="jarvis-sightings-sweep", daemon=True).start()
