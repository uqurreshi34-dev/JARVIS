"""Faces in a picture (actions/faces.py): where, how sure, whether they look at the camera, how sharp.

    python tools/test_faces.py

Checked without the model (a stand-in gives YuNet's outputs, so the reading of them is checked anywhere):

- no model, no faces, and nothing loaded trying;
- YuNet's outputs read as OpenCV reads them: a cell's score, box and five points, in the picture's own
  pixels whatever its size; below SURE_AT_LEAST nothing; one face found twice is one;
- facing: a nose midway between the eyes 1, at an eye 0, beyond it 0;
- sharpness: a picture made dark is as sharp as it was, a blurred one is not;
- worth: a face turned away, or blurred, is worth less.

With the real model (models/faces, or JARVIS_FACE_FOLDER) and a picture of a face named by
JARVIS_TEST_FACE_IMAGE: a face is found in it (SKIP otherwise).
"""

import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFilter  # noqa: E402

from actions import faces  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


real_dir = faces.MODEL_DIR
faces.MODEL_DIR = Path(__file__).resolve().parent / "no-face-model-here"
faces.forget()
picture = np.random.default_rng(2).integers(40, 200, (480, 640, 3)).astype(np.uint8)
check(not faces.present() and faces.detect(picture) == [] and faces._session is None,
      "without the model: no faces, and nothing loaded trying")

# ---- YuNet's outputs, read --------------------------------------------------------------------------------


class Output:
    def __init__(self, name):
        self.name = name


class StandIn:
    """YuNet's outputs: one face in the stride-16 cell at row 10, column 20, of a 640 x 640 input; and the
    same face found again, a little weaker, in the next cell; and a weak one elsewhere."""
    names = [f"{kind}_{stride}" for kind in ("cls", "obj", "bbox", "kps") for stride in faces.STRIDES]

    def __init__(self):
        self.fed = None

    def get_inputs(self):
        return [Output("input")]

    def get_outputs(self):
        return [Output(name) for name in self.names]

    def run(self, _, feeds):
        self.fed = feeds["input"]
        out = {}

        for stride in faces.STRIDES:
            cells = (faces.SIZE // stride) ** 2
            out[f"cls_{stride}"] = np.zeros((1, cells, 1), np.float32)
            out[f"obj_{stride}"] = np.zeros((1, cells, 1), np.float32)
            out[f"bbox_{stride}"] = np.zeros((1, cells, 4), np.float32)
            out[f"kps_{stride}"] = np.zeros((1, cells, 10), np.float32)

        columns = faces.SIZE // 16

        for index, (score, shift) in {10 * columns + 20: (0.9, 0.0), 10 * columns + 21: (0.8, -1.0),
                                      30 * columns + 5: (0.5, 0.0)}.items():
            out["cls_16"][0, index, 0] = score
            out["obj_16"][0, index, 0] = score
            # Middle half a cell in, 4 cells wide (e^ln4 * 16 = 64 pixels), 5 tall (80).
            out["bbox_16"][0, index] = (0.5 + shift, 0.5, np.log(4), np.log(5))
            # Eyes at (-1, -1) and (+1, -1) cells from the corner, nose at (0, 0), mouth at (+-0.7, +1).
            out["kps_16"][0, index] = (-1 + shift, -1, 1 + shift, -1, shift, 0, -0.7 + shift, 1, 0.7 + shift, 1)

        return [out[name] for name in self.names]


stand_in = StandIn()
faces.MODEL_DIR = real_dir
real_present = faces.present
faces.present = lambda: True
faces._session = stand_in

try:
    found = faces.detect(np.zeros((480, 640, 3), np.uint8))
    face = found[0] if found else None
    check(len(found) == 1 and abs(face["score"] - 0.9) < 1e-6,
          f"one face, though found in two cells; the weak one below SURE_AT_LEAST left ({len(found)} found)")
    check(face is not None and np.allclose(face["box"], (20.5 * 16 - 32, 10.5 * 16 - 40, 20.5 * 16 + 32,
                                                         10.5 * 16 + 40)),
          f"its box: the cell, its middle and size as YuNet gives them ({face and [round(v) for v in face['box']]})")
    check(face is not None and np.allclose(face["points"][0], (19 * 16, 9 * 16))
          and np.allclose(face["points"][2], (20 * 16, 10 * 16)), "and its five points")
    check(stand_in.fed.shape == (1, 3, 640, 640) and stand_in.fed[0, :, 480:, :].max() == 0,
          "fed a 640 x 480 picture padded below to the model's 640 x 640")

    small = faces.detect(np.zeros((240, 320, 3), np.uint8))
    check(small and np.allclose(small[0]["box"], [v / 2 for v in face["box"]]),
          "a picture half the size: the face in its own pixels (the picture scaled up to fit, and back)")

    blue_red = np.zeros((480, 640, 3), np.uint8)
    blue_red[..., 0] = 200                                       # red
    faces.detect(blue_red)
    check(stand_in.fed[0, 2, 0, 0] == 200 and stand_in.fed[0, 0, 0, 0] == 0,
          "fed blue-green-red, as YuNet was trained, values 0 to 255")
finally:
    faces.present = real_present
    faces.forget()

# ---- facing, sharpness, worth ------------------------------------------------------------------------------


def a_face(nose_x, box=(100, 100, 180, 200)):
    return {"score": 0.9, "box": box, "points": [(120, 130), (160, 130), (nose_x, 150), (125, 175), (155, 175)]}


check(abs(faces.facing(a_face(140)) - 1.0) < 1e-6, "a nose midway between the eyes: facing the camera (1)")
check(abs(faces.facing(a_face(130)) - 0.5) < 1e-6, "halfway to an eye: 0.5")
check(faces.facing(a_face(160)) == 0.0 and faces.facing(a_face(175)) == 0.0, "at an eye, or past it: 0")

rng = np.random.default_rng(4)
texture = (np.indices((200, 200)).sum(axis=0) % 8 < 4) * 120 + 60 + rng.integers(0, 20, (200, 200))
sharp = np.stack([texture] * 3, axis=2).astype(np.uint8)
dark = (sharp.astype(np.float32) * 0.2).astype(np.uint8)
blurred = np.asarray(Image.fromarray(sharp).filter(ImageFilter.BoxBlur(4)))
whole = (0, 0, 200, 200)
s_sharp, s_dark, s_blur = (faces.sharpness(x, whole) for x in (sharp, dark, blurred))
check(abs(s_dark - s_sharp) / s_sharp < 0.25 and s_blur < s_sharp / 2,
      f"sharpness: dark as sharp as bright ({s_dark:.2f} to {s_sharp:.2f}), blurred far less ({s_blur:.2f})")
check(faces.sharpness(sharp, (5, 5, 6, 6)) == 0.0, "a box of almost nothing has no sharpness")

box = (20, 20, 180, 180)
check(faces.worth(sharp, a_face(100, box) | {"points": [(60, 60), (140, 60), (100, 100), (70, 140), (130, 140)]})
      > faces.worth(blurred, a_face(100, box) | {"points": [(60, 60), (140, 60), (100, 100), (70, 140), (130, 140)]})
      > 0, "a blurred face is worth less")
check(faces.worth(sharp, a_face(158, box)) < faces.worth(sharp, a_face(140, box)),
      "and one turned away")

# ---- the real model ------------------------------------------------------------------------------------------

image = os.getenv("JARVIS_TEST_FACE_IMAGE")

if not faces.present():
    print(f"SKIP the real model (not in {faces.MODEL_DIR}; python tools/face_model.py)")
elif not image or not Path(image).is_file():
    print("SKIP the real model on a face (set JARVIS_TEST_FACE_IMAGE to a picture with a face in it)")
else:
    with Image.open(image) as photo:
        found = faces.detect(np.asarray(photo.convert("RGB")))

    check(found and found[0]["score"] >= faces.SURE_AT_LEAST, f"the real model finds the face ({len(found)} found)")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
