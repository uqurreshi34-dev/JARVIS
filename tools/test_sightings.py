"""Sightings: someone comes into view of the house cam, and what it saw is kept as evidence (actions/sightings.py).

    python tools/test_sightings.py

No camera: reports and looks are made here as the house cam sends them, and its pictures are stored as
its live stream stores them. Checked:

- what begins one: a person (the model's own name), not a bottle; whether or not the alerts are muted;
- the camera is kept streaming while it lasts (a lease: the desk's panel is not opened), and let go after;
- its pictures kept as the camera sent them, byte for byte, at most FPS a second, the moments just before
  included; each one's SHA-256 and time in sighting.json;
- the best picture: the sharp one over a blurred one, matched to the look that saw the person; the person
  cut out of it when the outline model is sure, else what is in their box;
- it ends when the person has been gone QUIET_SECONDS, or after MAX_SECONDS whatever;
- sightings-log.txt says each begun, ended, kept, viewed and deleted; old sightings are deleted, kept ones not;
- "keep the last sighting", "show me the last sighting": asked how people say it, and through commands.
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta
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

from PIL import Image, ImageFilter  # noqa: E402

from actions import cameras, detections, outline, sensors, sightings  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


# Pictures as the house cam sends them: a figure on a speckled wall, sharp, and blurred as by moving.
rng = np.random.default_rng(5)
WALL = rng.integers(70, 120, (480, 640, 3)).astype(np.uint8)
yy, xx = np.mgrid[0:480, 0:640]
FIGURE = ((abs(xx - 320) < 45) & (yy > 150) & (yy < 460)) | ((xx - 320) ** 2 + (yy - 110) ** 2 < 38 ** 2)
BOX = [270 / 640, 70 / 480, 370 / 640, 462 / 480]


def jpeg(blur=0, stripes=True, shade=0):
    rgb = WALL.copy()
    rgb[FIGURE] = (180, 60, 40)

    if stripes:
        rgb[FIGURE & ((yy // 6) % 2 == 0)] = (240, 220, 200)   # a striped jumper: something to be sharp

    picture = Image.fromarray(np.clip(rgb.astype(int) + shade, 0, 255).astype(np.uint8))

    if blur:
        picture = picture.filter(ImageFilter.BoxBlur(blur))

    out = io.BytesIO()
    picture.save(out, "JPEG", quality=85)
    return out.getvalue()


SHARP = jpeg()
BLURRED = jpeg(blur=6)
EMPTY = jpeg(stripes=False, shade=-200)   # no one: the wall alone, in the dark


def person(score=0.7, box=BOX):
    return {"label": "person", "score": score, "box": list(box)}


def wait_until(condition, seconds=6.0):
    until = time.monotonic() + seconds

    while time.monotonic() < until:
        if condition():
            return True

        time.sleep(0.05)

    return condition()


def written():
    """Every sighting begun has its record written (it leaves _open just before)."""
    return not sightings._open and all((path / sightings.RECORD_FILE).is_file()
                                       for path in base.iterdir() if path.is_dir() and path.name.startswith("house-cam-2"))


def reset():
    sightings.forget()
    cameras.forget()
    detections.forget()
    sensors.reset()
    sensors.report({"name": "house cam", "event": "online"})
    detections.set_listeners(on_say=lambda text: None, on_phone=lambda text, camera: None)


base = Path(home) / sightings.FOLDER

# The outline model as if not set up, wherever it is (models/mobile_sam on a PC that has made it): the
# no-model path is what is checked, and the model itself is stood in for below.
real_model_dir = outline.MODEL_DIR
outline.MODEL_DIR = Path(home) / "no-outline-model"
outline.forget()

# ---- the best picture ---------------------------------------------------------------------------------------

sharp_rgb, blurred_rgb = outline.picture(SHARP), outline.picture(BLURRED)
sharp_worth, sharp = sightings.best_shot(0.7, BOX, sharp_rgb)
blurred_worth, blurred = sightings.best_shot(0.7, BOX, blurred_rgb)
check(sharp > 10 * blurred and sharp_worth > 2 * blurred_worth,
      f"a sharp picture of someone is worth more than a blurred one (sharpness {sharp:.0f} to {blurred:.0f})")
small_worth, _ = sightings.best_shot(0.7, [0.45, 0.4, 0.5, 0.6], sharp_rgb)
check(small_worth < sharp_worth, "and more of them in view is worth more")
check(sightings.best_shot(0.7, [0.5, 0.5, 0.5, 0.5], sharp_rgb)[0] == 0.0, "a box of nothing is worth nothing")

# ---- what is asked ------------------------------------------------------------------------------------------

for text, wanted in (("keep the last sighting", "keep"), ("Jarvis, keep that sighting please", "keep"),
                     ("save the last intruder", "keep"), ("keep the last sighting for good", "keep"),
                     ("show me the last sighting", "show"), ("show the latest intruder", "show"),
                     ("let me see the last sighting", "show"), ("keep going", None),
                     ("show me the house cam", None), ("save the highlight", None)):
    check(sightings.asked(text) == wanted, f"{text!r} is {wanted}")

# ---- a bottle is not a sighting -----------------------------------------------------------------------------

reset()
detections.record("house cam", {"name": "house cam", "objects": [
    {"label": "bottle", "score": 0.8, "box": [0.1, 0.1, 0.2, 0.4]}]})
time.sleep(0.2)
check(not sightings._open and not cameras.live_wanted("house cam"), "a bottle begins nothing")

# ---- someone comes into view --------------------------------------------------------------------------------

reset()
real = {name: getattr(sightings, name) for name in ("QUIET_SECONDS", "LEASE_SECONDS", "MAX_SECONDS")}
sightings.QUIET_SECONDS = 1.0
sightings.LEASE_SECONDS = 1.0
detections.set_muted(True)
cameras.store("house cam", EMPTY)            # the moment before, already streaming
time.sleep(0.25)
detections.record("house cam", {"name": "house cam", "ms": 300, "objects": [person()]})
check(len(sightings._open) == 1 and cameras.live_wanted("house cam"),
      "someone in view begins a sighting, alerts muted or not, and the camera is kept streaming")
folders = [path for path in base.iterdir() if path.is_dir()]
check(len(folders) == 1 and folders[0].name.startswith("house-cam-"), f"in its own folder ({folders[0].name})")
stored = []

for picture in (BLURRED, BLURRED, SHARP, SHARP, BLURRED, SHARP, BLURRED, BLURRED):
    cameras.store("house cam", picture)
    stored.append(picture)
    time.sleep(0.11)

    # A look ends 0.3 s after its picture (ms 300): the sharp picture's look saw the person sharp.
    detections.take_marks("house cam", {"objects": [person()], "now": [person()]})

time.sleep(0.6)
detections.take_marks("house cam", {"objects": [], "now": []})   # gone
check(wait_until(written, 6), "gone QUIET_SECONDS, it ends")
check(wait_until(lambda: not cameras.live_wanted("house cam"), 3), "and the camera is let go")
detections.set_muted(False)

path = folders[0]
record = json.loads((path / sightings.RECORD_FILE).read_text(encoding="utf-8"))
kept_files = [frame["file"] for frame in record["frames"]]
check(kept_files and all((path / name).is_file() for name in kept_files), f"its pictures are kept ({len(kept_files)})")
check(all(sightings._sha256((path / frame["file"]).read_bytes()) == frame["sha256"] for frame in record["frames"]),
      "each with its SHA-256")
check(all((path / name).read_bytes() in stored + [EMPTY] for name in kept_files),
      "as the camera sent them, byte for byte")
check((path / kept_files[0]).read_bytes() == EMPTY, "the moment before they came is kept too")
times = [datetime.fromisoformat(frame["at"]) for frame in record["frames"]]
gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
check(len(kept_files) < len(stored) + 1 and min(gaps) >= 1.0 / sightings.FPS - 0.02,
      f"at most {sightings.FPS:g} a second ({len(kept_files)} of {len(stored) + 1})")
check(all(time_.utcoffset() is not None for time_ in times) and record["began"] < record["ended"]
      and record["camera"] == "house cam" and record["kept"] is False,
      "with local times that carry their offset from UTC, when it began and ended")
check((path / "best.jpg").read_bytes() == SHARP and record["best"]["label"] == "person",
      f"the best picture is a sharp one (sharpness {record['best']['sharpness']})")
check((path / "best-crop.jpg").is_file() and not (path / "best-cutout.png").exists()
      and record["made_from_best"][0]["file"] == "best.jpg"
      and record["made_from_best"][1]["file"] == "best-crop.jpg",
      "without the outline model, what is in their box; both with their SHA-256")
page = (path / "index.html").read_text(encoding="utf-8")
check(kept_files[0] in page and "<script>" in page and "http" not in page,
      "a page that plays them, needing nothing from the internet")
log = (base / sightings.LOG_FILE).read_text(encoding="utf-8")
check(f"began  {path.name}" in log and f"ended  {path.name}" in log, "the log says it began and ended")
check(any(look["seen"] and look["seen"][0]["label"] == "person" for look in record["looks"]),
      "and the record what each look saw")

# ---- however long they stay ----------------------------------------------------------------------------------

reset()
sightings.MAX_SECONDS = 1.5
detections.record("house cam", {"name": "house cam", "objects": [person()]})
started = time.monotonic()
first = Path(sightings._open["house cam"]["folder"]) / sightings.RECORD_FILE

# Seen all along, for up to 15 s: a slow machine (GitHub's) takes its time over each picture, so the
# sighting is given long enough to end, and checked by how long it ran, not by the clock here.
while time.monotonic() - started < 15 and not first.is_file():
    cameras.store("house cam", SHARP)
    detections.take_marks("house cam", {"objects": [person()], "now": [person()]})
    time.sleep(0.2)

sightings.MAX_SECONDS = real["MAX_SECONDS"]
detections.take_marks("house cam", {"objects": [], "now": []})
wait_until(written, 10)
long_seconds = json.loads(first.read_text(encoding="utf-8"))["seconds"] if first.is_file() else None
check(long_seconds is not None and 1.5 <= long_seconds < 15,
      f"still there after MAX_SECONDS, it ends anyway ({long_seconds} s)")

# ---- a report of what the board is sure of carries an older look's box (21 October 2026) --------------------

reset()
detections.record("house cam", {"name": "house cam", "objects": [person()]})
cameras.store("house cam", SHARP)                    # a look sees them, sharp
time.sleep(0.15)
detections.take_marks("house cam", {"objects": [person()], "now": [person()]})
time.sleep(0.6)
cameras.store("house cam", EMPTY)                    # they walk off ...
time.sleep(0.15)
# ... and the board's report of what it is still sure of (them: seen this look or the last) comes now.
detections.record("house cam", {"name": "house cam", "objects": [person(score=0.9)]})
time.sleep(0.3)
detections.take_marks("house cam", {"objects": [], "now": []})
wait_until(written, 6)
door_path, door_record = sightings.newest()
check((Path(door_path) / "best.jpg").read_bytes() == SHARP,
      "the best picture is one a look saw them in, not one a report of an older look's box was paired with")

# ---- the only look that saw them came before the camera streamed (21 October 2026) -------------------------

reset()
real_quiet = sightings.QUIET_SECONDS
sightings.QUIET_SECONDS = 1.0
detections.record("house cam", {"name": "house cam", "ms": 2500, "objects": [person()]})

for _ in range(4):
    cameras.store("house cam", SHARP)
    time.sleep(0.25)

detections.take_marks("house cam", {"objects": [], "now": []})
wait_until(written, 6)
sightings.QUIET_SECONDS = real_quiet
_, only_record = sightings.newest()
check(only_record["best"] is not None and only_record["best"]["seconds_from_look"] > sightings.FRAME_MATCH_SECONDS,
      "with no picture near the one look that saw them, the nearest, and how far from it in time "
      f"({(only_record['best'] or {}).get('seconds_from_look')} s)")

# ---- with the outline model sure of them (a stand-in) ------------------------------------------------------

reset()
stand_in = {name: getattr(outline, name) for name in ("present", "embed", "mask")}
outline.present, outline.embed = (lambda: True), (lambda rgb: {"size": rgb.shape[:2]})
outline.mask = lambda view, box: FIGURE.copy()

try:
    detections.record("house cam", {"name": "house cam", "objects": [person()]})
    cameras.store("house cam", SHARP)
    time.sleep(0.15)
    detections.take_marks("house cam", {"objects": [person()], "now": [person()]})
    time.sleep(0.3)
    detections.take_marks("house cam", {"objects": [], "now": []})
    wait_until(written, 8)
finally:
    for name, function in stand_in.items():
        setattr(outline, name, function)

newest_path, newest_record = sightings.newest()
cut = Path(newest_path) / "best-cutout.png"

if cut.is_file():
    with Image.open(cut) as picture:
        alpha = np.asarray(picture)[..., 3]

    check(picture.mode == "RGBA" and (alpha == 255).sum() == FIGURE.sum() and (alpha == 0).any(),
          "with the model sure of them, the person cut out, nothing behind them")
else:
    check(False, "with the model sure of them, the person cut out, nothing behind them")

for name, value in real.items():
    setattr(sightings, name, value)

# ---- keeping, showing, deleting ------------------------------------------------------------------------------

said = sightings.keep_last()
check(said.startswith("Kept the sighting from ") and json.loads(
      (Path(newest_path) / sightings.RECORD_FILE).read_text(encoding="utf-8"))["kept"] is True,
      f"'keep the last sighting' keeps it ({said!r})")
check(sightings.keep_last() == "That sighting is already kept, sir.", "kept once is enough")
picture, title, said = sightings.last_picture()
check(picture == (Path(newest_path) / "best.jpg").read_bytes() and title == "SIGHTING HOUSE CAM"
      and said.startswith("Someone on your house cam at ") and said.endswith(" seconds, sir."),
      f"'show me the last sighting' gives its best picture ({said!r})")

old = datetime.now().astimezone() - timedelta(days=sightings.KEEP_DAYS + 2)
for name, kept in (("house-cam-old", False), ("house-cam-old-kept", True)):
    (base / name).mkdir()
    (base / name / sightings.RECORD_FILE).write_text(json.dumps(
        {"camera": "house cam", "began": old.isoformat(), "kept": kept, "frames": []}), encoding="utf-8")

(base / "not-a-sighting").mkdir()
gone = sightings.sweep()
check(gone == 1 and not (base / "house-cam-old").exists() and (base / "house-cam-old-kept").exists()
      and (base / "not-a-sighting").exists() and Path(newest_path).exists(),
      "older than KEEP_DAYS and not kept: deleted; kept, recent, or not a sighting: left")
real_gb = sightings.MAX_GB
for name, kept, days in (("house-cam-2000-01-01-000000", False, 2), ("house-cam-2000-01-02-000000", True, 3)):
    (base / name).mkdir()
    (base / name / sightings.RECORD_FILE).write_text(json.dumps(
        {"camera": "house cam", "began": (datetime.now().astimezone() - timedelta(days=days)).isoformat(),
         "kept": kept, "frames": []}), encoding="utf-8")
    (base / name / "frame-0001.jpg").write_bytes(b"x" * 300_000)

# Just under all of them together: under it again once the oldest not kept (300 KB) goes.
sightings.MAX_GB = (sightings._size(base) - 150_000) / 1024 ** 3
gone = sightings.sweep()
sightings.MAX_GB = real_gb
check(gone == 1 and not (base / "house-cam-2000-01-01-000000").exists()
      and (base / "house-cam-2000-01-02-000000").exists() and Path(newest_path).exists(),
      "over MAX_GB: the oldest not kept deleted until under it; kept ones never")
log = (base / sightings.LOG_FILE).read_text(encoding="utf-8")
check("deleted  house-cam-old  " in log and "kept  " in log and "viewed  " in log,
      "the log says what was kept, viewed and deleted")

try:
    import commands
except Exception as error:
    print(f"SKIP commands (could not import commands: {error})")
    commands = None

if commands is not None:
    shown = []
    commands.set_camera_listener(lambda image, title: shown.append((image, title)))
    result = commands.handle_command("show me the last sighting")
    answer = result["action"]()
    check(result["intent"] == "camera_sighting" and shown and shown[-1][1] == "SIGHTING HOUSE CAM"
          and answer.startswith("Someone on your house cam"), f"through commands: shown on the panel ({answer!r})")
    result = commands.handle_command("keep the last sighting")
    check(result["intent"] == "camera_sighting" and result["action"]() == "That sighting is already kept, sir.",
          "and kept")

outline.MODEL_DIR = real_model_dir
outline.forget()
sightings.forget()
detections.forget()
cameras.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
