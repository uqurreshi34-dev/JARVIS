"""Boxes over a camera's live view: "highlight the bottle" (actions/highlight.py, the panel, the phone, the sketch).

    python tools/test_highlight.py

No camera: looks are made here as the house cam sends them -- down its live stream between frames, the
length's top bit set, and to /camera/seen. Checked:

- what is asked: "highlight the bottle", plurals, several things, "what you see", "it", someone, a
  camera named, "stop highlighting"; and what is not ("highlight the second story" is the news);
- a look's boxes in the live stream are kept for drawing, frames still counted, nothing said; bad
  ones end the stream as a bad frame does;
- only what is asked is boxed, and nothing once the boxes are stale or the highlighting stops;
- what JARVIS says: what is boxed, what is waited for;
- through commands: the view is opened if it is not up, the panel is given the boxes as each look
  comes, closing the view ends it, and other "highlight" commands are left alone;
- the panel draws them; the phone's /camera/marks gives them, with the token, and its page draws them;
- the sketch sends each look's boxes only in the s3detect build, never as a frame.
"""

import io
import json
import struct
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

# Packages only Windows (or a desk with audio) has: the real ones where they import, stand-ins elsewhere.
for _name in ("win32api", "win32con", "win32gui", "win32process", "win32event", "pythoncom", "sounddevice",
              "soundfile", "edge_tts", "pywebpush"):
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = MagicMock()

from actions import cameras, detections, highlight, sensors  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 200


def item(label, score=0.7, box=(0.1, 0.2, 0.4, 0.9)):
    return {"label": label, "score": score, "box": list(box)}


def look(*objects, now=None):
    return {"objects": list(objects), "now": list(now if now is not None else objects)}


def stream(*parts):
    """A live stream as the board sends it: frames, and a look's boxes with the length's top bit set."""
    data = b""

    for part in parts:
        if isinstance(part, dict):
            body = json.dumps(part).encode()
            data += struct.pack(">I", len(body) | cameras.MARKS_FLAG) + body
        else:
            data += struct.pack(">I", len(part)) + part

    return data


sensors.reset()
cameras.forget()
detections.forget()
highlight.forget()
sensors.report({"name": "house cam", "event": "online"})
sensors.report({"name": "my room cam", "event": "online"})
said = []
detections.set_listeners(on_say=said.append, on_phone=lambda text, camera: None)

# ---- what is asked ------------------------------------------------------------------------------------------

detections.record("house cam", {"name": "house cam", "ms": 2500,
                                "objects": [item("bottle"), item("cell phone"), item("laptop")]})
said.clear()
house = next(camera for camera in cameras.cameras() if camera["name"] == "house cam")

for text, labels in (("highlight the bottle", ("bottle",)), ("Jarvis, highlight bottles", ("bottle",)),
                     ("highlight the two bottles please", ("bottle",)),
                     ("outline the laptop and the bottle", ("laptop", "bottle")),
                     ("highlight the cell phones", ("cell phone",)),
                     ("highlight someone", ("person",)), ("box the people", ("person",))):
    asked = highlight.asked(text) or {}
    check(asked.get("on") is True and sorted(asked.get("what") or ()) == sorted(labels) and asked.get("known"),
          f"{text!r} highlights {labels}")

for text in ("highlight what you see", "highlight everything", "highlight what the camera sees", "box them all",
             "highlight all of them", "highlight what's there"):
    asked = highlight.asked(text) or {}
    check(asked.get("on") is True and asked.get("what") is None, f"{text!r} highlights everything in view")

check((highlight.asked("highlight it") or {}).get("what") == "last", "'highlight it' is what was last said")
named = highlight.asked("highlight the bottle on the house cam") or {}
check(named.get("camera") == house and named.get("what") == ("bottle",), "a camera can be named")
check((highlight.asked("highlight the cup") or {}).get("known") is False
      and (highlight.asked("highlight the cup") or {}).get("what") == ("cup",),
      "a thing not reported yet is still a thing to highlight, though not one any camera has named")

for text in ("stop highlighting", "clear the highlights", "highlights off", "unhighlight the bottle",
             "Jarvis, remove the boxes", "no more highlighting"):
    check((highlight.asked(text) or {}).get("on") is False, f"{text!r} stops it")

for text in ("what has the house cam detected", "show me the house cam", "highlight", "read the news",
             "mark it as done"):
    check(highlight.asked(text) is None, f"{text!r} is not a highlight")

# ---- a look's boxes in the live stream ----------------------------------------------------------------------

cameras.watch("house cam")
frames_seen = []
cameras.set_live_listener(lambda name, frame: frames_seen.append(name))
kept, _, ended = cameras.receive_live("house cam", io.BytesIO(stream(JPEG, look(item("bottle", 0.8)), JPEG)))
marks = detections.marks("house cam")
check(kept == 2 and len(frames_seen) == 2, "frames on either side of a look's boxes are kept as frames")
check(marks and [m["label"] for m in marks["objects"]] == ["bottle"] and marks["ago"] < 5,
      "the look's boxes are kept for drawing")
check(not said, "and nothing is said for them: announcing stays /camera/seen's")

too_big = struct.pack(">I", (cameras.MAX_MARKS_BYTES + 1) | cameras.MARKS_FLAG) + b"x"
check("boxes said" in cameras.receive_live("house cam", io.BytesIO(too_big))[2], "boxes claiming to be too large end it")
cut = stream(look(item("cup")))[:-4]
check("part way through a look" in cameras.receive_live("house cam", io.BytesIO(cut))[2], "and boxes cut short")
cameras.receive_live("house cam", io.BytesIO(stream(b"\xff\xd8\xff" + b"x" * 10, {"objects": "nope"}, JPEG)))
check([m["label"] for m in detections.marks("house cam")["objects"]] == ["bottle"],
      "boxes that are not a report are passed over, and the stream goes on")
cameras.stop_watching()

# ---- what is drawn -----------------------------------------------------------------------------------------

drawn = []
highlight.set_listener(drawn.append)
detections.take_marks("house cam", look(item("bottle"), item("bottle", 0.5, (0.5, 0.1, 0.7, 0.6)), item("laptop")))
check(highlight.boxes("house cam") == [], "nothing is drawn until asked")
words = highlight.start(house, ("bottle",))
check([b["label"] for b in highlight.boxes("house cam")] == ["bottle", "bottle"] and drawn[-1] == "house cam",
      f"only the bottles are boxed, and the panel told ({words!r})")
check(words == "Highlighting two bottles, sir.", "and that is said")
detections.take_marks("house cam", look(item("bottle", 0.9, (0.2, 0.2, 0.3, 0.5))))
check(len(highlight.boxes("house cam")) == 1 and drawn.count("house cam") >= 2,
      "each look moves the boxes, and the panel is told")
detections._marks["house cam"]["at"] -= detections.MARKS_FRESH_SECONDS + 1
check(highlight.boxes("house cam") == [], "stale boxes are not drawn")
detections.take_marks("house cam", look(item("laptop")))
check(highlight.start(house, ("bottle", "laptop")) == "Highlighting a laptop, sir. I can't see a bottle on your house"
      " cam right now. I'll box it as soon as it shows up.", "what is boxed, and what is waited for, is said")
check(highlight.start(house, ("cup",)) == "I can't see a cup on your house cam right now, sir. I'll box it as soon"
      " as it shows up.", "and when nothing asked for is in view")
check(highlight.start(house, None).startswith("Highlighting what your house cam sees, sir: a laptop"),
      "everything in view")
detections.record("house cam", {"name": "house cam", "ms": 2500, "objects": [item("dog"), item("laptop")]})
check(highlight.start(house, "last") == "Highlighting a dog, sir." and detections.last_said("house cam") == ["dog"],
      "'highlight it' boxes what was last said to come into view")
check(highlight.stop() == "Highlights off, sir." and highlight.boxes("house cam") == []
      and highlight.stop() == "Nothing's highlighted, sir.", "stopping clears them")
said.clear()

# ---- through commands ---------------------------------------------------------------------------------------

try:
    import commands
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP commands (could not import commands: {error})")
    commands = None

if commands is not None:
    told, shown = [], []
    commands.set_camera_grid_listener(lambda event, payload: told.append((event, payload)))
    commands.set_camera_listener(lambda image, title: shown.append((image, title)))
    opened = []
    real_picture = commands._camera_picture

    def fake_picture(command):
        opened.append(command)
        commands._live_view.update(name="house cam", title="HOUSE CAM")
        return "Here's your house cam, sir."

    commands._camera_picture = fake_picture

    try:
        detections.take_marks("house cam", look(item("bottle")))
        result = commands.handle_command("highlight the bottle")
        answer = result["action"]()
        check(result["intent"] == "camera_highlight" and answer == "Highlighting a bottle, sir."
              and opened == ["show me house cam"], f"commands: the view is opened and the bottle boxed ({answer!r})")
        check(told and told[-1][0] == "marks" and [b["label"] for b in told[-1][1][""]] == ["bottle"],
              "the panel is given the boxes for its own view")
        detections.take_marks("house cam", look(item("bottle", 0.9, (0.3, 0.3, 0.6, 0.6))))
        check(told[-1][1][""][0]["box"] == [0.3, 0.3, 0.6, 0.6], "and new ones as each look comes")
        result = commands.handle_command("highlight what you see")
        answer = result["action"]()
        check(result["intent"] == "camera_highlight" and answer.startswith("Highlighting what your house cam sees")
              and len(opened) == 1, f"a view already up is not opened again ({answer!r})")
        check(commands.handle_command("highlight the second story")["intent"] == "camera_highlight",
              "with a camera on screen, any name is a thing to box")
        stopped = commands.handle_command("stop highlighting")
        check(stopped["intent"] == "camera_highlight" and stopped["action"]() == "Highlights off, sir."
              and told[-1] == ("marks", {"": []}), "'stop highlighting' clears the panel")
        commands.handle_command("highlight the bottle")["action"]()
        commands._end_live()
        check(not highlight.active_any(), "closing the view ends the highlighting")
        again = commands.handle_command("stop highlighting")
        check(again["intent"] == "camera_highlight" and again["action"]() == "Nothing's highlighted, sir.",
              "and asked to stop with nothing highlighted, JARVIS says so")
        check(commands._highlight_camera(highlight.asked("highlight the second story")) is None,
              "with no camera on screen, a name no camera has reported is left to the rest of JARVIS")
    finally:
        commands._camera_picture = real_picture
        commands._end_live()

# ---- the panel ---------------------------------------------------------------------------------------------

try:
    from PyQt6.QtGui import QImage, QColor
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
    data = io.BytesIO()
    from PyQt6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, "JPG")
    panel._on_show(bytes(buffer.data()), "HOUSE CAM")

    def amber(widget):
        image = widget.grab().toImage()
        return sum(1 for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)
                   if (lambda c: c.red() > 200 and 140 < c.green() < 220 and c.blue() < 120)(image.pixelColor(x, y)))

    before = amber(panel)
    panel._on_marks({"": [item("bottle", 0.8, (0.2, 0.2, 0.6, 0.8))]})
    boxed = amber(panel)
    check(before == 0 and boxed > 20, f"the panel draws a box over its view ({boxed} amber points)")
    panel._on_marks({"": []})
    check(amber(panel) == 0, "and clears it")
    panel._on_marks({"": [item("bottle")]})
    panel._marks[""] = (panel._marks[""][0] - 60, panel._marks[""][1])
    check(amber(panel) == 0, "a box not renewed for a while is not drawn")
    panel._on_hide()

# ---- the phone ---------------------------------------------------------------------------------------------

try:
    import phone
except Exception as error:
    print(f"SKIP the phone (could not import phone: {error})")
    phone = None

if phone is not None:
    server = phone.PhoneServer(token="marks-test")
    client = server._app.test_client()
    detections.take_marks("house cam", look(item("bottle")))
    highlight.start(house, ("bottle",))
    check(client.get("/camera/marks?name=house%20cam").status_code == 403, "/camera/marks needs the token")
    got = client.get("/camera/marks?name=house%20cam", headers={"X-Jarvis-Token": "marks-test"}).get_json() or {}
    check([b["label"] for b in got.get("boxes", [])] == ["bottle"], "and gives what is highlighted")
    highlight.stop()
    page = (ROOT / "phone.py").read_text(encoding="utf-8")
    check('id="feed-marks"' in page and "/camera/marks?t=" in page and "startMarks()" in page,
          "the phone's page draws them over the feed while it plays")

# ---- the sketch --------------------------------------------------------------------------------------------

sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="utf-8")
live = sketch[sketch.index("void streamLive()"):sketch.index("void sendPicture()")]
detect = sketch[sketch.index("void detectTask"):sketch.index("void startDetection")]
check("LIVE_MARKS_FLAG = 0x80000000UL" in sketch and cameras.MARKS_FLAG == 0x80000000,
      "the board and JARVIS mark a look's boxes with the same bit")
check(live.count("#ifdef JARVIS_DETECT") >= 2 and "flagged = (uint32_t)marksLength | LIVE_MARKS_FLAG" in live,
      "the live stream sends them only in the detection build")
check("liveMarksSeq++" in detect and "addObjects(marksJson, seen, count)" in detect
      and "addObjects(marksJson, looked, lookedCount)" in detect,
      "each look leaves what the board is sure of, and what the look saw")
check("steadyOnes(seen, lookedCount)" in detect, "the steady logic is untouched")

highlight.forget()
detections.forget()
cameras.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
