"""A picture from the phone's camera: asked about what is in it, and kept under a name that says so.

    python tools/test_phone_look.py

No phone and no vision model: the picture is made here and the model's
answer is set by hand. Checked:

- a picture sent with no question asks what is in it, not what you are
  holding -- even after "What am I holding?" was asked before (a board
  lying on a bed was answered "nothing in your hand", 12 October 2026);
- a question that is asked is asked as it was;
- the saved picture is named after what was seen, without "sir", and
  with the extension its bytes call for: the phone's JPEG as .jpg.
"""

import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from PIL import Image  # noqa: E402

import providers  # noqa: E402
from actions import camera  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def picture(kind):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 170, 160)).save(buffer, format=kind)
    return buffer.getvalue()


JPEG, PNG = picture("JPEG"), picture("PNG")
asked = []


def vision(prompt, image, **_):
    asked.append(prompt.rsplit("Question: ", 1)[-1])
    return answers.pop(0)


answers = ["You are holding a mug, sir.", "An ESP32 board with a camera, lying on a bed, sir.",
           "A smoke detector on the ceiling, sir."]

with patch.object(providers, "vision", side_effect=vision):
    camera.describe_image(JPEG, "What am I holding?")
    said = camera.describe_image(JPEG)
    check(asked[-1] == camera.DEFAULT_QUESTION == "What am I looking at?",
          f"a picture with no question asks what is in it, not the last question ({asked})")
    check(said == "An ESP32 board with a camera, lying on a bed, sir.", "and the answer is passed on as given")

    saved = camera.save_last()
    name = os.path.basename(saved or "")
    check(name.startswith("esp32-board-with-a-camera-lying ") and name.endswith(".jpg"),
          f"kept under what was seen, without 'sir', as the JPEG it is ({name})")

    camera.describe_image(PNG, "what's on the ceiling")
    check(asked[-1] == "what's on the ceiling", "a question asked is asked as it was")
    name = os.path.basename(camera.save_last() or "")
    check(name.startswith("smoke-detector-on-the-ceiling ") and name.endswith(".png"),
          f"a PNG stays .png ({name})")

check(all(Path(path).parent == Path(folder) / "images" for path in [saved]), "in the images folder")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
