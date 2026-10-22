"""Faces JARVIS knows: yours, learned when you ask, and whether a sighting was you. Nobody else is named.

"Learn my face" takes the newest sighting (actions/sightings.py) as being of you: every picture in it
with one face, turned to the camera, gives that face's fingerprint, and the average of them is what
you look like to JARVIS. Asked again after another sighting (by day, by night), it learns from that one
too, so it knows you in more light. "Forget my face" deletes it all.

When a sighting ends, each face in its pictures is compared with yours: if at least SEEN_IN_AT_LEAST
pictures are as alike as SAME_AT_LEAST, its record says it was you. A face that is not yours is
compared and forgotten: nothing about a stranger's face is kept, only that they were not you.

The fingerprint: SFace (Apache 2.0, OpenCV's model zoo; MobileFaceNet, 38.7 MB), run by onnxruntime on
the PC, on the face cut to 112 x 112 as OpenCV's FaceRecognizerSF cuts it (the five points YuNet finds,
moved onto where ArcFace's training put them). 128 numbers; alike is their cosine. The model file is
models/faces/face_recognition_sface_2021dec.onnx, made by tools/face_model.py. What JARVIS knows of
you is <JARVIS folder>/faces/known.json, on this PC only.

Measured on the house cam (22 October 2026): learned from a day sighting, you by night came out 0.38
to 0.50 in your best six pictures; learned by night, by day 0.40 to 0.47. Seven strangers' faces, some
made small, dark and blurred like the house cam's, came out 0.25 at most.

    learn my face            the newest sighting is you: learn from it
    forget my face           JARVIS knows no one
"""

import json
import os
import re
import threading
from datetime import datetime

import numpy as np

from actions import faces, files

MODEL = "face_recognition_sface_2021dec.onnx"
SIZE = 112

# Where OpenCV's FaceRecognizerSF puts the five points (eyes, nose, mouth corners) in its 112 x 112 cut.
WHERE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                  [41.5493, 92.3655], [70.7299, 92.2041]])

# As alike as this (cosine): the same person. OpenCV's own for SFace is 0.363.
SAME_AT_LEAST = 0.363

# In at least this many of a sighting's pictures: one alike picture could be chance.
SEEN_IN_AT_LEAST = 3

# Faces turned further than this from the camera (faces.facing) say little of who it is.
FACING_AT_LEAST = 0.3

# Learned from pictures turned at least this far to the camera, and from at least this many of them.
LEARN_FACING_AT_LEAST = 0.5
LEARN_FROM_AT_LEAST = 5

# A picture whose face is less like the rest than this is not learned from (someone else, or a bad find).
LEARN_LIKE_THE_REST = 0.3

# At most this many fingerprints kept; the oldest go first.
KEEP_AT_MOST = 300

FOLDER = "faces"
KNOWN_FILE = "known.json"
YOU = "you"

_lock = threading.Lock()
_session = None


def present():
    """Whether the model's file is there (without loading it)."""
    return (faces.MODEL_DIR / MODEL).is_file()


def _load():
    global _session

    with _lock:
        if _session is None:
            try:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.log_severity_level = 3
                _session = ort.InferenceSession(str(faces.MODEL_DIR / MODEL), options,
                                                providers=["CPUExecutionProvider"])
            except Exception as error:   # noqa: BLE001 - no one is recognised, and all else goes on
                print(f"[JARVIS] the face fingerprint model could not be loaded: {error}")
                _session = False

        return _session


def _moved(points):
    """The 2 x 3 matrix moving [points] onto WHERE: turned, scaled and shifted only (Umeyama)."""
    source = np.asarray(points, dtype=np.float64)
    source_middle, target_middle = source.mean(axis=0), WHERE.mean(axis=0)
    source_off, target_off = source - source_middle, WHERE - target_middle
    u, sizes, v = np.linalg.svd(target_off.T @ source_off / len(source))
    flip = np.eye(2)

    if np.linalg.det(u) * np.linalg.det(v) < 0:
        flip[1, 1] = -1

    turn = u @ flip @ v
    scale = np.trace(np.diag(sizes) @ flip) / source_off.var(axis=0).sum()
    matrix = scale * turn
    return np.hstack([matrix, (target_middle - matrix @ source_middle)[:, None]])


def cut(rgb, face):
    """[face] in picture [rgb], cut to SIZE x SIZE as the model was trained."""
    from PIL import Image

    back = np.linalg.inv(np.vstack([_moved(face["points"]), [0, 0, 1]]))[:2]
    shown = Image.fromarray(np.ascontiguousarray(rgb)).transform((SIZE, SIZE), Image.AFFINE,
                                                                 tuple(back.ravel()), Image.BILINEAR)
    return np.asarray(shown)


def fingerprint(rgb, face):
    """[face]'s 128 numbers, of length 1; None without the model."""
    session = _load() if present() else None

    if not session:
        return None

    values = cut(rgb, face).astype(np.float32).transpose(2, 0, 1)[None]   # red-green-blue, 0 to 255
    out = session.run(None, {session.get_inputs()[0].name: values})[0][0].astype(np.float64)
    length = float(np.linalg.norm(out))
    return out / length if length > 0 else None


# ---- what is known -----------------------------------------------------------------------------------------

def _path():
    base = files.root()
    return os.path.join(base, FOLDER, KNOWN_FILE) if base else None


def _read():
    path = _path()

    try:
        with open(path, encoding="utf-8") as kept:
            known = json.load(kept)
    except (OSError, TypeError, ValueError):
        return {"people": {}}

    return known if isinstance(known.get("people"), dict) else {"people": {}}


def _write(known):
    path = _path()

    if not path:
        return False

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w", encoding="utf-8") as out:
            json.dump(known, out)

        return True
    except OSError as error:
        print(f"[JARVIS] could not keep a known face: {error}")
        return False


def _likeness(person):
    """The average of a person's fingerprints, of length 1."""
    prints = np.asarray(person.get("prints") or [], dtype=np.float64)

    if not len(prints):
        return None

    middle = prints.mean(axis=0)
    length = float(np.linalg.norm(middle))
    return middle / length if length > 0 else None


def known():
    """{name: their likeness} of everyone known (only you, for now)."""
    found = {}

    for name, person in _read()["people"].items():
        likeness = _likeness(person)

        if likeness is not None:
            found[name] = likeness

    return found


def anyone_known():
    return present() and bool(known())


def alike(print_, likeness):
    return float(np.dot(print_, likeness))


def who(prints):
    """{"name", "pictures", "alike"} of who a sighting was, from its faces' fingerprints [prints], or None:
    someone known only if at least SEEN_IN_AT_LEAST of them are as alike as SAME_AT_LEAST."""
    best = None

    for name, likeness in known().items():
        scores = sorted((alike(one, likeness) for one in prints), reverse=True)
        close = [score for score in scores if score >= SAME_AT_LEAST]

        if len(close) >= SEEN_IN_AT_LEAST and (best is None or len(close) > best["pictures"]):
            best = {"name": name, "pictures": len(close), "alike": round(scores[0], 3)}

    return best


def learn(folder, name=YOU):
    """Learn [name]'s face from the sighting in [folder]. Returns what to say."""
    if not present() or not faces.present():
        return "I need the face models first, sir: run python tools/face_model.py, then restart me."

    from PIL import Image

    prints = []

    for file in sorted(os.listdir(folder)):
        if not re.match(r"^(?:frame-\d{4}|still-\d{2})\.jpg$", file):
            continue

        try:
            with Image.open(os.path.join(folder, file)) as picture:
                rgb = np.asarray(picture.convert("RGB"))
        except OSError:
            continue

        found = faces.detect(rgb)

        # One face only: with two in a picture, which is you cannot be told.
        if len(found) == 1 and faces.facing(found[0]) >= LEARN_FACING_AT_LEAST:
            one = fingerprint(rgb, found[0])

            if one is not None:
                prints.append(one)

    if prints:
        middle = np.mean(prints, axis=0)
        middle /= np.linalg.norm(middle)
        prints = [one for one in prints if alike(one, middle) >= LEARN_LIKE_THE_REST]

    if len(prints) < LEARN_FROM_AT_LEAST:
        return (f"That sighting hasn't enough clear pictures of one face to learn from, sir: "
                f"{len(prints)}, and I need {LEARN_FROM_AT_LEAST}. Face the camera for a few seconds and ask again.")

    known_now = _read()
    person = known_now["people"].setdefault(name, {"prints": [], "learned_from": []})
    person["prints"] = (person["prints"] + [[round(float(v), 6) for v in one] for one in prints])[-KEEP_AT_MOST:]
    person["learned_from"].append({"sighting": os.path.basename(folder), "pictures": len(prints),
                                   "at": datetime.now().astimezone().isoformat(timespec="seconds")})

    if not _write(known_now):
        return "I couldn't keep your face, sir."

    times = len(person["learned_from"])
    return (f"Learned your face from {len(prints)} pictures, sir" +
            (f", {times} sightings so far." if times > 1 else ". Ask again after a sighting at night, so I know you in any light."))


def unlearn(name=YOU):
    """Forget [name]'s face. Returns what to say."""
    known_now = _read()

    if name not in known_now["people"]:
        return "I don't know your face, sir."

    del known_now["people"][name]
    return "Forgotten your face, sir." if _write(known_now) else "I couldn't forget your face, sir."


def forget():
    """Load the model afresh next time. For tests."""
    global _session

    with _lock:
        _session = None
