"""What the house cam recognises on its own board (actions/detections.py, POST /camera/seen, the sketch).

    python tools/test_detections.py

Without a camera: reports are made here as the sketch sends them. Checked:

- a report is kept, and a bad one refused: not a list, a score out of range, a box that is not four
  numbers from 0 to 1, too many things;
- what comes into view is said once, in one sentence, someone first -- "Someone and two cups are in
  view of your house cam, sir." -- to the phone at once with the camera to open, aloud through the
  gate, and journalled; not again while it stays, nor within its gap (ANNOUNCE_GAP for someone,
  OTHER_GAP for anything else); JARVIS_DETECT_ANNOUNCE can narrow it to some things;
- "mute the camera alerts" stops the saying (it is still journalled), "unmute" starts it;
- "what has the house cam detected" and "is anyone on the house cam" are answered free, in words:
  "Your house cam sees a person and two cups, sir."; a stale report says so; a camera that only takes
  pictures is not claimed;
- the phone server's /camera/seen: the token, the size, a report;
- the sketch: detection only in the s3detect build, beside live view, and labels from the model;
- tools/femto_build.py: the labels header, and the model it expects.
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import cameras, detections, sensors  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def report(*objects, ms=4200):
    return {"name": "house cam", "ms": ms,
            "objects": [{"label": label, "score": score, "box": [0.1, 0.2, 0.4, 0.9]} for label, score in objects]}


sensors.reset()
cameras.forget()
detections.forget()
sensors.report({"name": "house cam", "event": "online"})
sensors.report({"name": "my room cam", "event": "online"})

said, written, phoned = [], [], []
detections.set_listeners(on_say=said.append, on_phone=lambda text, camera: phoned.append((text, camera)))

with patch.object(detections.journal, "alert", written.append):
    # ---- reports ------------------------------------------------------------------------------------
    check(detections.record("house cam", report(("cup", 0.81))) is None
          and detections.latest("house cam")["objects"][0]["label"] == "cup", "a report is kept")
    check(said == ["A cup is in view of your house cam, sir."] and written == said
          and phoned == [("A cup is in view of your house cam, sir.", "house cam")],
          f"a cup coming into view is said, sent to the phone with the camera, and journalled ({said})")
    check(detections.record("house cam", {"objects": "cup"}) == "not a report"
          and detections.record("house cam", report(("cup", 1.5))) == "not a report"
          and detections.record("house cam", {"objects": [{"label": "cup", "score": 0.5, "box": [0, 0, 2, 1]}]})
          == "not a report"
          and detections.record("house cam", {"objects": [{"label": "x", "score": 0.5, "box": [0, 0, 1]}]})
          == "not a report"
          and detections.record("house cam", report(*[("cup", 0.5)] * (detections.MAX_OBJECTS + 1)))
          == "not a report"
          and detections.record("", report()) == "no camera named",
          "a bad report is refused")
    check(len(said) == 1, "the refused reports said nothing")

    detections.record("house cam", report(("person", 0.88), ("cup", 0.81), ("cup", 0.6)))
    check(said[-1] == "Someone's in view of your house cam, sir." and len(said) == 2,
          f"someone coming into view is said; the cup, already there, is not said again ({said[-1]})")
    detections.record("house cam", report(("person", 0.9), ("cup", 0.8)))
    check(len(said) == 2, "not again while they stay in view")
    detections.record("house cam", report(("cup", 0.8)))
    detections.record("house cam", report(("person", 0.7)))
    check(len(said) == 2, f"nor when they step back in within {detections.ANNOUNCE_GAP:.0f} seconds")
    detections._announced[("house cam", "person")] -= detections.ANNOUNCE_GAP + 1
    detections.record("house cam", report(("cup", 0.8)))
    detections.record("house cam", report(("person", 0.7)))
    check(len(said) == 3, "and again once that has passed")

    detections._announced.clear()
    detections.record("house cam", report())
    detections.record("house cam", report(("cup", 0.8), ("person", 0.9), ("cup", 0.7)))
    check(said[-1] == "Someone and two cups are in view of your house cam, sir.",
          f"what arrives together is said together, someone first ({said[-1]})")
    detections.record("house cam", report())
    detections._announced[("house cam", "person")] -= detections.ANNOUNCE_GAP + 1
    detections.record("house cam", report(("cup", 0.8), ("person", 0.9)))
    check(said[-1] == "Someone's in view of your house cam, sir.",
          f"a cup back within {detections.OTHER_GAP:.0f} seconds is not said again; someone is ({said[-1]})")
    with patch.object(detections, "ANNOUNCE", ("person",)):
        detections._announced.clear()
        detections.record("house cam", report())
        count = len(said)
        detections.record("house cam", report(("dog", 0.8)))
        check(len(said) == count, "narrowed to people, a dog is not said")
    check(detections.arrival_words("house cam", {"person": 2}) == "Two people are in view of your house cam, sir.",
          "two people are two people")

    # ---- muting ---------------------------------------------------------------------------------------
    check(detections.muting_asked("mute the camera alerts") is True
          and detections.muting_asked("Jarvis turn off the person alerts") is True
          and detections.muting_asked("unmute the camera alerts") is False
          and detections.muting_asked("turn on the camera alerts") is False
          and detections.muting_asked("mute the music") is None and detections.muting_asked("show the camera") is None,
          "'mute the camera alerts' and 'unmute the camera alerts', and nothing else")
    detections.set_muted(True)
    detections._announced.clear()
    detections.record("house cam", report())
    written.clear()
    count, sent = len(said), len(phoned)
    detections.record("house cam", report(("person", 0.7)))
    check(len(said) == count and len(phoned) == sent and len(written) == 1,
          "muted: not said nor sent to the phone, but still journalled")
    detections.set_muted(False)

# ---- asking -----------------------------------------------------------------------------------------

detections.record("house cam", report(("person", 0.88), ("cup", 0.81), ("cup", 0.6)))
house = next(camera for camera in cameras.cameras() if camera["name"] == "house cam")
for text in ("what has the house cam detected", "what has my house cam spotted", "is anyone on the house cam",
             "Jarvis, what's the house cam detecting?", "has the house camera seen anyone"):
    check(detections.asked(text) == house, f"asked of the house cam: {text!r}")

for text in ("show me the house cam", "what's happening on the street", "is anyone in my room",
             "is anyone on my room cam", "what can you see"):
    check(detections.asked(text) is None, f"not claimed: {text!r}")

check(detections.summary(house) == "Your house cam sees a person and two cups, sir.",
      f"answered in words ({detections.summary(house)!r})")
detections.record("house cam", report())
check(detections.summary(house) == "Nothing your house cam recognises is in view, sir.", "and when nothing is there")
detections._latest["house cam"]["at"] -= 600
check("last reported 10 minutes ago" in detections.summary(house), "a stale report says how old it is")

# ---- through commands, and the phone ---------------------------------------------------------------------

try:
    import commands
except Exception as error:
    print(f"SKIP commands (could not import commands: {error})")
    commands = None

if commands is not None:
    detections.record("house cam", report(("person", 0.88)))
    asked = commands.handle_command("is anyone on the house cam")
    answer = asked["action"]()
    check(asked["intent"] == "camera_detections" and answer == "Your house cam sees a person, sir.",
          f"commands: answered free, no model call ({answer!r})")
    muted = commands.handle_command("mute the camera alerts")
    check(muted["intent"] == "camera_alerts" and "muted" in muted["action"]() and detections._muted,
          "commands: 'mute the camera alerts' mutes them")
    commands.handle_command("unmute the camera alerts")["action"]()
    check(not detections._muted, "and 'unmute the camera alerts' brings them back")

try:
    import phone
except Exception as error:
    print(f"SKIP the phone (could not import phone: {error})")
    phone = None

if phone is not None:
    server = phone.PhoneServer(token="seen-test")
    client = server._app.test_client()
    headers = {"X-Jarvis-Token": "seen-test", "X-Jarvis-Camera": "house cam"}
    check(client.post("/camera/seen", json=report(("dog", 0.7))).status_code == 403, "/camera/seen needs the token")
    check(client.post("/camera/seen", json={"objects": "dog"}, headers=headers).status_code == 400, "refuses a bad report")
    check(client.post("/camera/seen", json=report(("dog", 0.7)), headers=headers).status_code == 200
          and detections.latest("house cam")["objects"][0]["label"] == "dog", "and keeps a good one")
    with patch.object(server, "_send_push") as pushed:
        server.sighting("Someone's in view of your house cam, sir.", "house cam")
        for _ in range(50):
            if pushed.called:
                break

            time.sleep(0.02)
    check(pushed.called and pushed.call_args[0][1] == {"name": "house cam", "title": "HOUSE CAM"},
          "a sighting is pushed to the phone at once, and tapping it opens that camera")

# ---- the sketch, and the build -------------------------------------------------------------------------------

sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="utf-8")
ini = (ROOT / "arduino" / "jarvis_camera" / "platformio.ini").read_text(encoding="utf-8")
detect_part = sketch[sketch.index("void detectTask"):sketch.index("void startDetection")]
check("[env:s3detect]" in ini and "extends = env:s3cam" in ini and "-D JARVIS_DETECT" in ini
      and "+<detect/*.c>" in ini, "s3detect is s3cam with detection; s3cam itself is unchanged")
check(sketch.count("#ifdef JARVIS_DETECT") >= 3 and "xTaskCreatePinnedToCore(detectTask" in sketch
      and ", 0) == pdPASS" in sketch, "detection only in that build, on the other core from live view")
check("esp_camera_fb_return(frame);" in detect_part
      and detect_part.index("esp_camera_fb_return(frame);") < detect_part.index("run_model();"),
      "a frame is handed back before the model runs, so live view never waits on it")
check("/camera/seen" in sketch and "\\\"box\\\"" in sketch and "image/jpeg" not in detect_part,
      "it sends what it found, never the picture")
check("arduino/jarvis_camera/detect/" in (ROOT / ".gitignore").read_text(encoding="utf-8"),
      "the made sources are kept out of git")

from tools import femto_build  # noqa: E402

header = femto_build.labels_header(["person", "bicycle", "cup"])
check("#define LABEL_COUNT 3" in header and '  "person",' in header, "labels.h numbers the model's names")
check(femto_build.INPUT == [1, 3, 256, 256] and "MODEL_SIZE = 256" in sketch,
      "the builder expects the input the sketch letterboxes to")

detections.forget()
cameras.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
