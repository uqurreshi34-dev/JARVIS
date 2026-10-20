"""A highlight's exact shape, and "save the highlight" (actions/outline.py, actions/highlight.py).

    python tools/test_outline.py

No camera and, unless it is set up, no outline model: a picture is made here with a "bottle" in it,
and a stand-in model cuts out exactly that bottle. Checked:

- the outline tracer: shapes (round, square, L, U, a dot, a bottle) traced on their edge, all the way
  round, in at most MAX_POINTS points; a cut-out is the thing alone, clear around it;
- without the model's files: no outlines, nothing loaded, highlights stay boxes; saved pictures are
  what is in each box;
- with it: each highlighted thing's outline made after a look, off the look's thread, and given with
  its box; kept while the box hardly moves; dropped when the highlighting stops;
- "save the highlight": asked how people say it; through commands, the camera that is highlighting;
  the images folder made only if it is not there; one PNG a thing, cut out with no background;
  nothing highlighted, nothing saved;
- the panel draws the outline; the phone's page does;
- the real model (models/mobile_sam, or JARVIS_SAM_FOLDER), when it is set up: a bottle on a busy
  background cut out as it is (SKIP when it is not).
"""

import io
import os
import sys
import threading
import time
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

from actions import cameras, detections, highlight, outline, sensors  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def edge(region):
    """The pixels of boolean [region] with a neighbour outside it."""
    padded = np.pad(region, 1)
    inner = padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:]
    return region & ~inner


def traced_well(region, points):
    """Every point on [region]'s edge (within the tracer's step), and how far the edge gets from them."""
    height, width = region.shape
    border = edge(region)
    ys, xs = np.nonzero(border)
    at = np.array([[x * width - 0.5, y * height - 0.5] for x, y in points])

    on = all(np.min(np.hypot(xs - px, ys - py)) <= 3.5 for px, py in at)
    gap = max(np.min(np.hypot(at[:, 0] - x, at[:, 1] - y)) for y, x in zip(ys, xs))
    return on, gap


# ---- the tracer ---------------------------------------------------------------------------------------------

yy, xx = np.mgrid[0:120, 0:160]
shapes = {
    "a round thing": (xx - 80) ** 2 + (yy - 60) ** 2 < 40 ** 2,
    "a square": (abs(xx - 80) < 30) & (abs(yy - 60) < 30),
    "an L": ((xx > 30) & (xx < 50) & (yy > 10) & (yy < 110)) | ((xx > 30) & (xx < 120) & (yy > 90) & (yy < 110)),
    "a U": ((xx > 30) & (xx < 130) & (yy > 10) & (yy < 110)) & ~((xx > 50) & (xx < 110) & (yy < 90)),
}

for name, region in shapes.items():
    points = outline.outline(region)
    on, gap = traced_well(region, points)
    check(3 <= len(points) <= outline.MAX_POINTS and on and gap < 4,
          f"{name}: traced on its edge, all the way round ({len(points)} points, widest gap {gap:.1f} px)")

dot = np.zeros((50, 50), dtype=bool)
dot[20, 30] = True
check(len(outline.outline(dot)) == 1, "a single pixel is one point, not a hang")
check(outline.outline(None) == [] and outline.outline(np.zeros((9, 9), dtype=bool)) == [], "nothing, no outline")

two = shapes["a square"] | ((xx < 12) & (yy < 12))
check(traced_well(shapes["a square"], outline.outline(two))[0], "of two parts, the larger is traced")

# The picture the tests use: a blue bottle on a grey, speckled table, 640 x 480 as the house cam sends.
rng = np.random.default_rng(7)
yy, xx = np.mgrid[0:480, 0:640]
BOTTLE = (((abs(xx - 320) < 60) & (yy > 200) & (yy < 440)) | ((abs(xx - 320) < 20) & (yy > 110) & (yy <= 200))
          | (((xx - 320) ** 2 / 60 ** 2 + (yy - 200) ** 2 / 50 ** 2 < 1) & (yy <= 200)))
RGB = rng.integers(60, 140, (480, 640, 3)).astype(np.uint8)
RGB[BOTTLE] = (40, 120, 220)
# The board's box for it: as a detector draws one, a few pixels outside the bottle all round.
BOX = [254 / 640, 104 / 480, 387 / 640, 447 / 480]
buffer = io.BytesIO()
Image.fromarray(RGB).save(buffer, "JPEG", quality=95)
JPEG = buffer.getvalue()

points = outline.outline(BOTTLE)
on, gap = traced_well(BOTTLE, points)
check(on and gap < 8, f"a bottle's outline hugs it, neck and all ({len(points)} points, widest gap {gap:.1f} px)")

ys, xs = np.nonzero(BOTTLE)
SIZE = (xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)
cut = outline.cutout(RGB, BOTTLE)
alpha = np.asarray(cut)[..., 3]
check(cut.mode == "RGBA" and cut.size == SIZE and alpha.min() == 0 and alpha.max() == 255
      and (alpha == 255).sum() == BOTTLE.sum(), "a cut-out is the bottle alone, cropped, clear around it")

# ---- what is good enough to draw ----------------------------------------------------------------------------

PIXEL_BOX = (BOX[0] * 640, BOX[1] * 480, BOX[2] * 640, BOX[3] * 480)
sure = np.where(BOTTLE, 6.0, -6.0)
found, steady, fits = outline.judge(sure, PIXEL_BOX)
check(found is not None and (found ^ BOTTLE).sum() <= BOTTLE.sum() // 1000 and steady > 0.99 and fits > 0.8,
      f"a clear shape in its box is kept, as it is (steady {steady:.2f}, fits {fits:.2f})")

x1, y1, x2, y2 = PIXEL_BOX
loose = (x1 - 0.125 * (x2 - x1), y1 - 0.125 * (y2 - y1), x2 + 0.125 * (x2 - x1), y2 + 0.125 * (y2 - y1))
found, steady, fits = outline.judge(sure, loose)
check(found is None and fits < outline.FITS_BOX_AT_LEAST,
      f"a shape that doesn't fit its box (the box a look old, the camera moved) keeps the box (fits {fits:.2f})")

unsure = np.where(BOTTLE, 0.5, -0.5) + np.random.default_rng(3).normal(0, 0.8, BOTTLE.shape)
found, steady, fits = outline.judge(unsure, PIXEL_BOX)
check(found is None and steady < outline.STEADY_AT_LEAST,
      f"a shape SAM is unsure of keeps the box (steady {steady:.2f})")

messy = BOTTLE.copy()
messy[300:320, 300:330] = False                  # a hole in the bottle
messy[120:124, 250:254] = True                   # specks beside it, inside the box
messy[430:433, 392:395] = True
tidied = outline.tidy(messy)
check(tidied is not None and tidied[300:320, 300:330].all() and not tidied[120:124, 250:254].any()
      and not tidied[430:433, 392:395].any() and (tidied & ~BOTTLE).sum() <= BOTTLE.sum() // 1000
      and (BOTTLE & ~tidied).sum() <= BOTTLE.sum() // 1000,
      "tidied: holes filled, specks gone, the edge where it was")
check(outline.tidy(None) is None and outline.tidy(np.zeros((9, 9), dtype=bool)) is None, "nothing tidies to nothing")

# ---- without the model --------------------------------------------------------------------------------------

real_dir = outline.MODEL_DIR
outline.MODEL_DIR = Path(home) / "no-model-here"
outline.forget()
check(not outline.present() and "sam_export" in (outline.why_not() or ""),
      "without the model's files, outlines can't be made, and JARVIS says how to set them up")
check(outline._sessions is None, "and nothing was loaded trying")


def item(label, score=0.8, box=BOX):
    return {"label": label, "score": score, "box": list(box)}


def look(*objects):
    return {"objects": list(objects), "now": list(objects)}


sensors.reset()
cameras.forget()
detections.forget()
highlight.forget()
sensors.report({"name": "house cam", "event": "online"})
detections.set_listeners(on_say=lambda text: None, on_phone=lambda text, camera: None)
detections.record("house cam", {"name": "house cam", "ms": 2500, "objects": [item("bottle")]})
house = next(camera for camera in cameras.cameras() if camera["name"] == "house cam")
cameras.store("house cam", JPEG)
told = []
highlight.set_listener(told.append)
highlight.start(house, ("bottle",))
detections.take_marks("house cam", look(item("bottle")))
time.sleep(0.3)
shown = highlight.boxes("house cam")
check(shown and "outline" not in shown[0] and not highlight._jobs and highlight._worker is None,
      "without it, a highlight is a box, and nothing is queued")

images = Path(home) / highlight.IMAGES_FOLDER
check(not images.exists(), "no images folder until something is saved")
said = highlight.save(house)
saved = sorted(images.glob("*.png")) if images.exists() else []
check(said == "Saved a bottle to your images folder, in their boxes, sir." and len(saved) == 1,
      f"saved without it: what is in the box ({said!r})")

if saved:
    with Image.open(saved[0]) as picture:
        check(picture.size == (int(BOX[2] * 640) - int(BOX[0] * 640), int(BOX[3] * 480) - int(BOX[1] * 480)) and saved[0].name.startswith("house-cam-bottle-"),
              f"named for the camera and the thing ({saved[0].name}, {picture.size})")

    saved[0].unlink()

highlight.stop()

# ---- with a model (a stand-in, cutting out exactly the bottle) ----------------------------------------------

outline.MODEL_DIR = real_dir
real = {name: getattr(outline, name) for name in ("present", "embed", "mask", "scored")}
called = {"embed": 0, "threads": set(), "means": []}


def fake_embed(rgb):
    called["embed"] += 1
    called["means"].append(float(rgb.mean()))
    called["threads"].add(threading.current_thread().name)
    return {"size": rgb.shape[:2]}


def fake_mask(view, box):
    return BOTTLE.copy() if abs(box[0] - BOX[0]) < 0.1 else None


def fake_scored(view, box):
    found = fake_mask(view, box)
    return found, (0.95 if found is not None else 0.4), (0.9 if found is not None else 0.3)


outline.present, outline.embed, outline.mask, outline.scored = (lambda: True), fake_embed, fake_mask, fake_scored


def settle(seconds=3.0):
    until = time.monotonic() + seconds

    while time.monotonic() < until:
        with highlight._work:
            busy = bool(highlight._jobs)

        if not busy and called["embed"]:
            time.sleep(0.1)
            return

        time.sleep(0.05)


try:
    told.clear()
    highlight.start(house, ("bottle",))
    detections.take_marks("house cam", look(item("bottle")))
    settle()
    shown = highlight.boxes("house cam")
    line = shown[0].get("outline") if shown else None
    check(line and traced_well(BOTTLE, line)[0], "after a look, the bottle's outline comes with its box")
    check(called["threads"] == {"jarvis-outline"}, "made off the thread that took the look")
    check(told.count("house cam") >= 2, "and the panel and phone are told to draw it")

    detections.take_marks("house cam", look(item("bottle", 0.8, [b + 0.01 for b in BOX])))
    shown = highlight.boxes("house cam")
    check(shown and shown[0].get("outline") == line, "a box hardly moved keeps its outline until the next is made")
    detections.take_marks("house cam", look(item("bottle", 0.8, (0.0, 0.0, 0.2, 0.2))))
    shown = highlight.boxes("house cam")
    check(shown and "outline" not in shown[0], "one somewhere else is a box until its own is made")
    detections.take_marks("house cam", look(item("bottle")))
    settle()

    # The look's own picture, though a newer one (the camera moved on to a plain wall) has come since.
    buffer = io.BytesIO()
    Image.fromarray(np.full((480, 640, 3), 200, dtype=np.uint8)).save(buffer, "JPEG")
    WALL = buffer.getvalue()
    detections.record("house cam", {"name": "house cam", "ms": 800, "objects": [item("bottle")]})
    cameras.store("house cam", JPEG)
    time.sleep(0.8)
    cameras.store("house cam", WALL)
    before = len(called["means"])
    detections.take_marks("house cam", look(item("bottle")))
    until = time.monotonic() + 3

    while len(called["means"]) == before and time.monotonic() < until:
        time.sleep(0.05)

    used = called["means"][before:] or [None]
    check(used[0] is not None and abs(used[0] - RGB.mean()) < 5,
          "the outline is cut from the picture the look was made from, not the newest")
    check(cameras.frame_at("house cam", time.monotonic()) == WALL
          and cameras.frame_at("house cam", time.monotonic() - 0.8) == JPEG
          and cameras.frame_at("no such cam", time.monotonic()) is None,
          "a camera's recent pictures are kept, each found by when it came")
    real_history = cameras.HISTORY_SECONDS
    cameras.HISTORY_SECONDS = 0.5
    time.sleep(0.6)
    cameras.store("house cam", WALL)
    cameras.HISTORY_SECONDS = real_history
    check(len(cameras._history["house cam"]) == 1, "and only the last few seconds of them")
    settle()

    # "save the highlight"
    for text in ("save the highlight", "Jarvis, save the highlights", "save the highlighted bottle",
                 "keep them", "save it please", "store the cut-outs", "grab that"):
        asked = highlight.asked(text) or {}
        check(asked.get("save") is True and asked.get("on") is True, f"{text!r} saves the highlight")

    for text in ("save the file", "save my notes", "keep going"):
        check(not (highlight.asked(text) or {}).get("save"), f"{text!r} is not about highlights")

    images.mkdir(exist_ok=True)
    (images / "mine.png").write_bytes(b"not touched")
    said = highlight.save(house)
    saved = sorted(path for path in images.glob("house-cam-*.png"))
    check(said == "Saved a bottle to your images folder, cut out, sir." and len(saved) == 1,
          f"saved: one PNG a thing ({said!r})")
    check((images / "mine.png").read_bytes() == b"not touched", "an images folder already there is used as it is")

    if saved:
        with Image.open(saved[0]) as picture:
            kept = np.asarray(picture)

        check(picture.mode == "RGBA" and kept.shape[:2] == SIZE[::-1] and (kept[..., 3] == 255).sum() == BOTTLE.sum()
              and (kept[..., 3] == 0).sum() == kept[..., 3].size - BOTTLE.sum(),
              "cut out with no background: the bottle solid, everything else clear")
        inside = kept[..., :3][kept[..., 3] == 255].astype(int)
        check(abs(inside.mean(axis=0) - (40, 120, 220)).max() < 12, "and it is the bottle's own colours")

    try:
        import commands
    except Exception as error:
        print(f"SKIP commands (could not import commands: {error})")
        commands = None

    if commands is not None:
        check(commands._highlight_camera(highlight.asked("save the highlight")) == house,
              "through commands, the camera that is highlighting saves")
        check(commands._highlight_camera(highlight.asked("save it")) == house, "'save it' too, while it is")
        before = len(list(images.glob("house-cam-*.png")))
        time.sleep(1.1)
        answer = commands._camera_highlight("save the highlight")
        check(answer.startswith("Saved a bottle") and len(list(images.glob("house-cam-*.png"))) == before + 1,
              f"and saves ({answer!r})")

    highlight.stop()
    check(not highlight._shapes, "stopping drops the outlines")
    check(highlight.save(house) == "Nothing's highlighted to save, sir.", "nothing highlighted, nothing saved")

    if commands is not None:
        check(commands._highlight_camera(highlight.asked("save it")) is None,
              "and with nothing highlighted, 'save it' is left to the rest of JARVIS")
finally:
    for name, function in real.items():
        setattr(outline, name, function)

    highlight.stop()
    highlight.set_listener(None)

# ---- the panel and the phone draw it ------------------------------------------------------------------------

try:
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from camera_panel import CameraPanel
except Exception as error:
    print(f"SKIP the panel (could not load Qt: {error})")
    CameraPanel = None

if CameraPanel is not None:
    panel = CameraPanel()
    picture = QImage(320, 240, QImage.Format.Format_RGB32)
    picture.fill(QColor(20, 20, 20))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, "JPG")
    panel._on_show(bytes(buffer.data()), "HOUSE CAM")
    panel.resize(640, 480)
    image_before = panel.grab().toImage()
    shape = dict(item("bottle"), outline=outline.outline(BOTTLE))
    panel._on_marks({"": [shape]})
    drawn = panel.grab().toImage()

    def tinted(image, x, y):
        colour = image.pixelColor(x, y)
        return colour.red() > 40 and colour.red() > colour.blue() + 15

    # The middle of the bottle is filled; a corner of its box, outside the bottle, is not.
    middle = (drawn.width() // 2, int(drawn.height() * 0.7))
    check(tinted(drawn, *middle) and not tinted(image_before, *middle), "the panel fills the bottle's shape")
    corner_x, corner_y = int(drawn.width() * (BOX[0] + 0.01)), int(drawn.height() * (BOX[1] + 0.01))
    check(not tinted(drawn, corner_x, corner_y), "and not its box's corners, outside the bottle")
    panel._on_hide()

page = (ROOT / "phone.py").read_text(encoding="utf-8")
check("item.outline" in page and "closePath()" in page[page.index("item.outline"):],
      "the phone's page draws an outline when there is one")

# ---- the real model ------------------------------------------------------------------------------------------

outline.forget()

if not outline.present():
    print(f"SKIP the real model (not set up in {outline.MODEL_DIR}; python tools/sam_export.py)")
else:
    try:
        import onnxruntime  # noqa: F401
    except Exception as error:
        print(f"SKIP the real model (no onnxruntime: {error})")
    else:
        view = outline.embed(RGB)
        found, steady, fits = outline.scored(view, BOX)
        print(f"  the real model on the bottle: steady {steady:.2f}, fits its box {fits:.2f}")
        overlap = (found & BOTTLE).sum() / (found | BOTTLE).sum() if found is not None else 0.0
        check(overlap > 0.9, f"the real model cuts out the bottle as it is ({overlap:.1%} the same)")
        check(traced_well(BOTTLE, outline.outline(found))[1] < 8 if found is not None else False,
              "and its outline hugs the bottle")

detections.forget()
cameras.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
