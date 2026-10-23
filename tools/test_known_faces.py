"""Faces JARVIS knows (actions/known_faces.py): learning yours from a sighting, and whether a sighting was you.

    python tools/test_known_faces.py

Checked without the models (stand-ins give faces and fingerprints, so the rules are checked anywhere):

- no model: no fingerprints, no one known, and "learn my face" says what to run;
- the cut: a face's five points land where the model was trained to see them, whatever its size and tilt;
- who: you only when at least SEEN_IN_AT_LEAST pictures are as alike as SAME_AT_LEAST; no one else named;
- learning: only pictures with one face, turned to the camera, and like the rest; too few and nothing is
  kept; learned again, it adds; "forget my face" deletes it;
- a sighting's record says it was you, or None; nothing of a face is kept in it but that;
- "learn my face", "forget my face": asked how people say it, and "show me the last sighting" says you;
  the sighting learned from is marked as you; sightings from before your face was known are checked for
  it, and those not found to be you checked again after learning more;
- greeted: "Someone's in view" said at once, never held; then "It's you, sir. Welcome back." when the
  first pictures are you; a stranger, or no pictures, and nothing more is said; the board's own picture
  of a look with a face in it (POST /camera/face) greets you on its own when surely you, and is kept.

With the real models (models/faces, or JARVIS_FACE_FOLDER) and JARVIS_TEST_FACE_IMAGE: a face's
fingerprint is 128 numbers, and the same face made smaller is still alike (SKIP otherwise).
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

home = sandbox.activate()

for _name in ("win32api", "win32con", "win32gui", "win32process", "win32event", "pythoncom", "sounddevice",
              "soundfile", "edge_tts", "pywebpush"):
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = MagicMock()

from PIL import Image  # noqa: E402

import io  # noqa: E402
import time  # noqa: E402

from actions import cameras, detections, faces, known_faces, outline, sensors, sightings  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


real_dir = faces.MODEL_DIR
outline.MODEL_DIR = Path(home) / "no-outline-model"
outline.forget()
faces.MODEL_DIR = Path(home) / "no-face-model"
faces.forget()
known_faces.forget()
picture = np.zeros((480, 640, 3), np.uint8)
one_face = {"score": 0.9, "box": (100, 100, 200, 220), "points": [(130, 140), (170, 140), (150, 165), (135, 190),
                                                                   (165, 190)]}
check(not known_faces.present() and known_faces.fingerprint(picture, one_face) is None
      and not known_faces.anyone_known() and known_faces._session is None,
      "without the model: no fingerprints, no one known, and nothing loaded trying")
check("tools/face_model.py" in known_faces.learn(home), "and 'learn my face' says what to run")

# ---- the cut ------------------------------------------------------------------------------------------------

turn = np.radians(20)
tilted = np.array([[np.cos(turn), -np.sin(turn)], [np.sin(turn), np.cos(turn)]])
points = (known_faces.WHERE - 56) @ tilted.T * 1.7 + (300, 200)
moved = known_faces._moved(points)
check(np.allclose(np.hstack([points, np.ones((5, 1))]) @ moved.T, known_faces.WHERE, atol=1e-6),
      "the five points of a face larger and tilted are moved exactly onto where the model expects them")
dotted = np.zeros((480, 640, 3), np.uint8)
nose_x, nose_y = (int(round(v)) for v in points[2])
dotted[nose_y - 2:nose_y + 3, nose_x - 2:nose_x + 3] = 255
cut = known_faces.cut(dotted, {"points": [tuple(p) for p in points]})
found_y, found_x = np.unravel_index(cut[..., 0].argmax(), cut.shape[:2])
check(cut.shape == (112, 112, 3) and abs(found_x - known_faces.WHERE[2][0]) <= 2
      and abs(found_y - known_faces.WHERE[2][1]) <= 2,
      f"and the picture with them: the nose lands at its place in the 112 x 112 cut ({found_x}, {found_y})")

# ---- who, learning, forgetting (stand-ins for the models) ---------------------------------------------------

rng = np.random.default_rng(7)


def unit(vector):
    return vector / np.linalg.norm(vector)


you = unit(rng.normal(size=128))


def like_you(how_alike):
    """A fingerprint [how_alike] (cosine) to you."""
    other = unit(rng.normal(size=128))
    other = unit(other - other.dot(you) * you)
    return unit(how_alike * you + np.sqrt(1 - how_alike ** 2) * other)


# Each picture says, in its top-left pixels, which fingerprint its face has (red / 4) and how many faces it
# has (green / 80), in steps wide enough for JPEG's rounding.
PRINTS = {}


def picture_of(number, faces_in_it=1, facing_camera=True):
    shown = np.full((120, 160, 3), 128, np.uint8)
    shown[:8, :8] = (number * 4, faces_in_it * 80, 255 if facing_camera else 0)
    return shown


def stand_in_detect(rgb):
    number, count, facing_camera = round(rgb[2, 2, 0] / 4), round(rgb[2, 2, 1] / 80), int(rgb[2, 2, 2])
    nose = 150 if facing_camera > 128 else 168
    return [dict(one_face, points=[(130, 140), (170, 140), (nose, 165), (135, 190), (165, 190)])
            for _ in range(count)] if number else []


real = {"faces.present": faces.present, "faces.detect": faces.detect, "known.present": known_faces.present,
        "fingerprint": known_faces.fingerprint}
faces.present = lambda: True
faces.detect = stand_in_detect
known_faces.present = lambda: True
known_faces.fingerprint = lambda rgb, face: PRINTS.get(round(rgb[2, 2, 0] / 4))
base = Path(home) / sightings.FOLDER


def sighting_folder(name, frames):
    path = base / name

    if path.exists():
        shutil.rmtree(path)

    path.mkdir(parents=True)

    for index, shown in enumerate(frames, start=1):
        Image.fromarray(shown).save(path / f"frame-{index:04d}.jpg", quality=100)

    return path


try:
    known_file = Path(home) / known_faces.FOLDER / known_faces.KNOWN_FILE

    for number in range(1, 30):
        PRINTS[number] = like_you(0.8)
    PRINTS[40] = like_you(0.0)                                         # someone else in a picture

    few = sighting_folder("house-cam-2026-10-20-090000", [picture_of(n) for n in (1, 2, 3)])
    check("hasn't enough" in known_faces.learn(str(few)) and not known_file.exists(),
          "too few clear pictures: nothing learned, nothing kept")

    frames = ([picture_of(n) for n in range(1, 7)] + [picture_of(7, faces_in_it=2), picture_of(8, facing_camera=False),
                                                      picture_of(40)])
    first = sighting_folder("house-cam-2026-10-20-100000", frames)
    said = known_faces.learn(str(first))
    kept = json.loads(known_file.read_text(encoding="utf-8"))
    check(said.startswith("Learned your face from 6 pictures") and len(kept["people"]["you"]["prints"]) == 6,
          f"learned from the 6 pictures with one face, facing, like the rest -- not two faces, turned or someone "
          f"else ({said!r})")
    check(np.dot(known_faces.known()["you"], you) > 0.95, "what you look like: the average of them")
    second = sighting_folder("house-cam-2026-10-21-230000", [picture_of(n) for n in range(10, 16)])
    check("2 sightings so far" in known_faces.learn(str(second))
          and len(json.loads(known_file.read_text(encoding="utf-8"))["people"]["you"]["learned_from"]) == 2,
          "learned again from another sighting, it adds")

    likeness = known_faces.known()["you"]
    sure = known_faces.SAME_AT_LEAST
    check(known_faces.who([like_you(sure + 0.05)] * 2 + [like_you(0.1)] * 50) is None,
          "two pictures alike is not enough: no one")
    found = known_faces.who([like_you(sure + 0.05)] * 3 + [like_you(0.1)] * 50)
    check(found and found["name"] == "you" and found["pictures"] == 3, f"three are: you ({found})")
    check(known_faces.who([like_you(sure - 0.05)] * 40) is None, "many a little less alike: no one")

    # A sighting's record.
    for number in range(50, 60):
        PRINTS[number] = like_you(0.7)
    for number in range(30, 40):
        PRINTS[number] = like_you(0.05)

    sightings.forget()
    mine = sighting_folder("house-cam-2026-10-22-080000", [picture_of(n) for n in range(50, 60)])
    stranger = sighting_folder("house-cam-2026-10-22-090000", [picture_of(n) for n in range(30, 40)])
    os.utime(mine, (0, 0))
    os.utime(stranger, (0, 0))
    sightings.finish_unfinished()
    mine_record = json.loads((mine / sightings.RECORD_FILE).read_text(encoding="utf-8"))
    stranger_record = json.loads((stranger / sightings.RECORD_FILE).read_text(encoding="utf-8"))
    check((mine_record.get("recognised") or {}).get("name") == "you", "a sighting of you: its record says so")
    check("recognised" in stranger_record and stranger_record["recognised"] is None,
          "a stranger: no one, and not named")
    check(not any("print" in key for key in stranger_record) and "prints" not in json.dumps(stranger_record),
          "nothing of their face kept in the record")
    check("; you)" in (base / sightings.LOG_FILE).read_text(encoding="utf-8"), "the log says it was you")

    shutil.rmtree(stranger)
    check(sightings.last_picture()[2].startswith("You were on your house cam"),
          "'show me the last sighting' says it was you")
    shutil.rmtree(mine)

    # ---- greeted as you arrive -------------------------------------------------------------------------------

    def as_jpeg(shown):
        out = io.BytesIO()
        Image.fromarray(shown).save(out, "JPEG", quality=100)
        return out.getvalue()

    def wait_until(condition, seconds=8.0):
        until = time.monotonic() + seconds

        while time.monotonic() < until and not condition():
            time.sleep(0.05)

        return condition()

    def arrive(numbers, gap=0.15):
        """Someone comes into view: the board's report, then the stream's pictures of face [numbers]."""
        sightings.forget()
        cameras.forget()
        detections.forget()
        sensors.reset()
        sensors.report({"name": "house cam", "event": "online"})
        said.clear()
        earlier[:] = [path for path in base.iterdir() if path.is_dir()]
        detections.record("house cam", {"name": "house cam", "ms": 300,
                                        "objects": [{"label": "person", "score": 0.8, "box": [0.3, 0.2, 0.6, 0.9]}]})
        held = list(said)

        for number in numbers:
            cameras.store("house cam", as_jpeg(picture_of(number)))
            time.sleep(gap)

        return held

    def gone():
        detections.take_marks("house cam", {"objects": [], "now": []})
        return wait_until(lambda: not sightings._open and all((path / sightings.RECORD_FILE).is_file()
                                                              for path in base.iterdir()
                                                              if path.is_dir() and path not in earlier), 10)

    said, earlier = [], []
    detections.set_listeners(on_say=said.append, on_phone=lambda text, camera: None)
    real_waits = (sightings.GREET_WAIT_SECONDS, sightings.GREET_WAIT_MOST, sightings.QUIET_SECONDS,
                  sightings.LEASE_SECONDS)
    # The newest picture is checked each tick (twice a second): three of you take a second and a half.
    sightings.GREET_WAIT_SECONDS, sightings.GREET_WAIT_MOST = 2.5, 4.0
    sightings.QUIET_SECONDS, sightings.LEASE_SECONDS = 1.0, 1.0

    try:
        someone = "Someone's in view of your house cam, sir."
        held = arrive(list(range(50, 60)) * 2)
        check(held == [someone], f"you come into view: 'Someone's in view' at once, as ever ({held})")
        check(wait_until(lambda: len(said) == 2, 5) and said == [someone, "It's you, sir. Welcome back."],
              f"and when the pictures are you: 'It's you, sir. Welcome back.' after it ({said})")
        check(gone(), "the sighting ends as before")
        newest_folder, newest_record = sightings.newest()
        check(newest_record.get("greeted") == sightings.GREETING and "greeted you" in
              (base / sightings.LOG_FILE).read_text(encoding="utf-8"), "its record and the log say you were greeted")
        shutil.rmtree(newest_folder)

        held = arrive(list(range(30, 40)) * 2)
        time.sleep(sightings.GREET_WAIT_SECONDS)
        check(held == [someone] and said == [someone], f"a stranger: 'Someone's in view' at once, and no more ({said})")
        gone()
        shutil.rmtree(sightings.newest()[0])

        held = arrive([])
        time.sleep(sightings.GREET_WAIT_MOST + 0.5)
        check(held == [someone] and said == [someone], f"no pictures at all: the same ({said})")
        gone()
        shutil.rmtree(sightings.newest()[0])

        # The board's own picture of the look, sent because its face finder saw a face in it: one
        # that is surely you greets you with no live view at all.
        arrive([])
        check(cameras.take_face("house cam", as_jpeg(picture_of(50)), 0.91) is None,
              "a look's picture with a face in it is taken (POST /camera/face)")
        check(wait_until(lambda: len(said) == 2, 3) and said[1] == sightings.GREETING,
              f"one picture surely you (alike {known_faces.CERTAIN_AT_LEAST} or more): greeted before live view "
              f"starts ({said})")
        check(gone(), "the sighting ends as before")
        folder, record = sightings.newest()
        check([look["file"] for look in record.get("look_pictures", [])] == ["look-01.jpg"]
              and record["look_pictures"][0]["face_finder"] == 0.91 and (Path(folder) / "look-01.jpg").is_file()
              and record["look_pictures"][0]["sha256"] == hashlib.sha256((Path(folder) / "look-01.jpg").read_bytes()).hexdigest(),
              "and kept as it came, look-01.jpg, with its SHA-256 and the board's face score")
        shutil.rmtree(folder)

        PRINTS[45] = like_you(0.45)                 # alike, but not surely: one is not enough
        arrive([])
        cameras.take_face("house cam", as_jpeg(picture_of(45)), 0.8)
        time.sleep(sightings.GREET_WAIT_SECONDS + 0.5)
        check(said == [someone], f"one picture only a little alike: not greeted on it alone ({said})")
        gone()
        shutil.rmtree(sightings.newest()[0])

        check(cameras.take_face("house cam", b"", 0.9) == "empty" and cameras.take_face("house cam", b"GIF89a", 0.9)
              == "not a JPEG", "a face picture that is empty or not a JPEG is refused")

        try:
            import phone
        except Exception as error:
            print(f"SKIP the phone server's /camera/face (could not import phone: {error})")
            phone = None

        if phone is not None:
            client = phone.PhoneServer(token="face-test")._app.test_client()
            headers = {"X-Jarvis-Token": "face-test", "X-Jarvis-Camera": "house cam", "X-Jarvis-Face": "0.88",
                       "Content-Type": "image/jpeg"}
            jpeg = as_jpeg(picture_of(50))
            check(client.post("/camera/face", data=jpeg, headers={"X-Jarvis-Camera": "house cam"}).status_code == 403
                  and client.post("/camera/face", data=b"not a jpeg", headers=headers).status_code == 400,
                  "/camera/face needs the token, and a JPEG")
            arrive([])
            check(client.post("/camera/face", data=jpeg, headers=headers).status_code == 200
                  and wait_until(lambda: len(said) == 2, 3) and said[1] == sightings.GREETING,
                  "and through it, the board's picture greets you")
            gone()
            shutil.rmtree(sightings.newest()[0])
    finally:
        (sightings.GREET_WAIT_SECONDS, sightings.GREET_WAIT_MOST, sightings.QUIET_SECONDS,
         sightings.LEASE_SECONDS) = real_waits
        detections.set_listeners(on_say=None, on_phone=None)
        sightings.forget()
        detections.forget()
        cameras.forget()

    # Sightings from before your face was known: checked for it, at start and after "learn my face".
    def recorded(name, numbers, recognised="never"):
        path = sighting_folder(name, [picture_of(n) for n in numbers])
        record = {"camera": "house cam", "began": "2026-10-21T12:00:00+01:00", "seconds": 5,
                  "frames": [{"file": f"frame-{index:04d}.jpg"} for index in range(1, len(numbers) + 1)]}

        if recognised != "never":
            record["recognised"] = recognised

        (path / sightings.RECORD_FILE).write_text(json.dumps(record), encoding="utf-8")
        return path

    def recognised_in(path):
        return json.loads((path / sightings.RECORD_FILE).read_text(encoding="utf-8")).get("recognised", "never")

    old_you = recorded("house-cam-2026-10-21-100000", list(range(50, 60)))
    old_stranger = recorded("house-cam-2026-10-21-110000", list(range(30, 40)))
    missed = recorded("house-cam-2026-10-21-120000", list(range(50, 60)), recognised=None)
    checked = sightings.check_unchecked()
    check(checked == 2 and (recognised_in(old_you) or {}).get("name") == "you"
          and recognised_in(old_stranger) is None and recognised_in(missed) is None,
          f"sightings never checked for your face: checked, you and not you ({checked} checked)")
    check("checked  house-cam-2026-10-21-100000  (you)" in (base / sightings.LOG_FILE).read_text(encoding="utf-8")
          and "recognised_later" in json.loads((old_you / sightings.RECORD_FILE).read_text(encoding="utf-8")),
          "the log and the record say when it was checked")
    check(sightings.check_unchecked() == 0, "each only once")
    check(sightings.check_unchecked(again=True) == 2 and (recognised_in(missed) or {}).get("name") == "you"
          and recognised_in(old_stranger) is None,
          "after learning more, those not found to be you are checked again: you now, a stranger still not")

    for path in (old_you, old_stranger, missed):
        shutil.rmtree(path)

    # Learned from: that sighting is you, though it ended before your face was known.
    learned_from = sighting_folder("house-cam-2099-01-01-000000", [picture_of(n) for n in range(50, 60)])
    (learned_from / sightings.RECORD_FILE).write_text(json.dumps({"camera": "house cam", "began": "2099-01-01T00:00:00+00:00",
                                                             "seconds": 5, "frames": [], "recognised": None}),
                                                 encoding="utf-8")
    check(sightings.learn_face().startswith("Learned")
          and json.loads((learned_from / sightings.RECORD_FILE).read_text(encoding="utf-8"))["recognised"]["name"] == "you",
          "the sighting learned from is marked as you")
    shutil.rmtree(learned_from)

    check(sightings.asked("learn my face") == "learn" and sightings.asked("Jarvis, learn my face please.") == "learn"
          and sightings.asked("learn my face from the last sighting") == "learn",
          "'learn my face', how people say it")
    check(sightings.asked("forget my face") == "unlearn" and sightings.asked("forget my facebook password") is None
          and sightings.asked("learn my faces of the moon") is None, "'forget my face', and nothing like it")
    check(sightings.unlearn_face() == "Forgotten your face, sir." and not known_faces.known()
          and known_faces.unlearn() == "I don't know your face, sir.", "forgotten, and then not known at all")
    check(not known_faces.anyone_known(), "with no one known, sightings compare no faces")
finally:
    faces.present = real["faces.present"]
    faces.detect = real["faces.detect"]
    known_faces.present = real["known.present"]
    known_faces.fingerprint = real["fingerprint"]
    sightings.forget()

# ---- the real models -----------------------------------------------------------------------------------------

faces.MODEL_DIR = real_dir
faces.forget()
known_faces.forget()
image = os.getenv("JARVIS_TEST_FACE_IMAGE")

if not (faces.present() and known_faces.present()):
    print(f"SKIP the real models (not in {faces.MODEL_DIR}; python tools/face_model.py)")
elif not image or not Path(image).is_file():
    print("SKIP the real models on a face (set JARVIS_TEST_FACE_IMAGE to a picture with a face in it)")
else:
    with Image.open(image) as photo:
        full = np.asarray(photo.convert("RGB"))
        half = np.asarray(photo.convert("RGB").resize((photo.width // 2, photo.height // 2), Image.BILINEAR))

    big, small = faces.detect(full), faces.detect(half)

    if not big or not small:
        check(False, "the real model finds the face, at full size and half")
    else:
        one, two = known_faces.fingerprint(full, big[0]), known_faces.fingerprint(half, small[0])
        check(one.shape == (128,) and abs(np.linalg.norm(one) - 1) < 1e-6
              and known_faces.alike(one, two) > known_faces.SAME_AT_LEAST,
              f"the real fingerprint: 128 numbers; the same face at half size alike ({known_faces.alike(one, two):.2f})")

faces.MODEL_DIR = real_dir
faces.forget()
known_faces.forget()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
