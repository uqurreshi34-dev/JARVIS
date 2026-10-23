"""What the house cam recognises on its own board (actions/detections.py, POST /camera/seen, the sketch).

    python tools/test_detections.py

Without a camera: reports are made here as the sketch sends them. Checked:

- a report is kept, and a bad one refused: not a list, a score out of range, a box that is not four
  numbers from 0 to 1, too many things;
- what comes into view is said once, in one sentence, someone first -- "Someone and two cups are in
  view of your house cam, sir." -- to the phone at once with the camera to open, aloud, and
  journalled; not again while it stays, nor after a look or two missed, but again once it has been
  out of view for AWAY_SECONDS, and when more of it come than were said ("Three bottles");
  JARVIS_DETECT_ANNOUNCE narrows it to some things, JARVIS_DETECT_IGNORE leaves some out;
- "mute the camera alerts" stops the saying (it is still journalled), "unmute" starts it;
- "what has the house cam detected" and "is anyone on the house cam" are answered free, in words:
  "Your house cam sees a person and two cups, sir."; a stale report says so; a camera that only takes
  pictures is not claimed; a board that can is told to take a look then, and the answer is that look;
- the phone server's /camera/seen: the token, the size, a report;
- the sketch: detection only in the s3detect build, beside live view, and labels from the model;
- the sketch's face finder: after the report, for the first looks with someone in them until one
  picture with a face in it is sent; built only when its sources were made; timed apart in s3profile;
- the sketch says once whether FEMTO-ML's depthwise convs passed their own check, and still builds
  with a FEMTO-ML from before that check;
- tools/femto_build.py: the labels header, the model it expects, and the face finder's tuning frames,
  left out (and none left from before) without what it needs.
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

    detections.record("house cam", report(("person", 0.88), ("cup", 0.81)))
    check(said[-1] == "Someone's in view of your house cam, sir." and len(said) == 2,
          f"someone coming into view is said; the cup, already there, is not said again ({said[-1]})")
    detections.record("house cam", report(("person", 0.9), ("cup", 0.8)))
    check(len(said) == 2, "not again while they stay in view")
    detections.record("house cam", report(("cup", 0.8)))
    detections.record("house cam", report(("person", 0.7)))
    check(len(said) == 2, "nor when the model misses someone for a look and finds them again")

    def away(*labels):
        for label in labels:
            detections._present[("house cam", label)]["last"] -= detections.AWAY_SECONDS + 1

    detections.record("house cam", report(("cup", 0.8)))
    away("person")
    detections.record("house cam", report(("cup", 0.8), ("person", 0.7)))
    check(len(said) == 3 and said[-1] == "Someone's in view of your house cam, sir.",
          f"but again once they have been away {detections.AWAY_SECONDS:.0f} seconds ({said[-1]})")

    detections.record("house cam", report(("bottle", 0.6)))
    check(said[-1] == "A bottle is in view of your house cam, sir.", f"one bottle ({said[-1]})")
    detections.record("house cam", report(("bottle", 0.6), ("bottle", 0.5), ("bottle", 0.5)))
    check(said[-1] == "Three bottles are in view of your house cam, sir.",
          f"then three: more of them than were said is said ({said[-1]})")
    count = len(said)
    detections.record("house cam", report(("bottle", 0.6), ("bottle", 0.5)))
    detections.record("house cam", report(("bottle", 0.6), ("bottle", 0.5), ("bottle", 0.5)))
    check(len(said) == count, "but the model's count flickering between two and three is not news")

    detections._present.clear()
    detections.record("house cam", report())
    detections.record("house cam", report(("cup", 0.8), ("person", 0.9), ("cup", 0.7)))
    check(said[-1] == "Someone and two cups are in view of your house cam, sir.",
          f"what arrives together is said together, someone first ({said[-1]})")
    detections.record("house cam", report())
    away("person")
    detections.record("house cam", report(("cup", 0.8), ("person", 0.9)))
    check(said[-1] == "Someone's in view of your house cam, sir.",
          f"a cup back within {detections.AWAY_SECONDS:.0f} seconds is not said again; someone is ({said[-1]})")
    with patch.object(detections, "ANNOUNCE", ("person",)):
        detections._present.clear()
        detections.record("house cam", report())
        count = len(said)
        detections.record("house cam", report(("dog", 0.8)))
        check(len(said) == count, "narrowed to people, a dog is not said")
    detections._present.clear()
    detections.record("house cam", report())
    detections.record("house cam", report(("cup", 0.8)))
    sentence = said[-1]
    house_cam = next(camera for camera in cameras.cameras() if camera["name"] == "house cam")
    detections.summary(house_cam)
    check(sentence == "A cup is in view of your house cam, sir." and detections.answered(sentence)
          and not detections.answered(sentence),
          "a sighting your question was just answered with is not said again after the answer, once")
    detections.record("house cam", report(("cup", 0.8), ("bottle", 0.7)))
    check(not detections.answered(said[-1]), "but one the answer did not cover still is")
    with patch.object(detections, "IGNORE", ("sink",)):
        detections._present.clear()
        count = len(said)
        detections.record("house cam", report(("sink", 0.5), ("cup", 0.8)))
        check(len(said) == count + 1 and said[-1] == "A cup is in view of your house cam, sir.",
              f"a sink, ignored, is left out of what is said ({said[-1]})")
        house_cam = next(camera for camera in cameras.cameras() if camera["name"] == "house cam")
        check("sink" in detections.summary(house_cam), "but still answered when asked")
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
    detections._present.clear()
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

# JARVIS restarted with nothing new in view: the board has said nothing since, but it is still the
# house cam's question -- it was left to the model, which said it had no access to the feed.
with open(detections._detecting_path(), encoding="utf-8") as kept:
    check(kept.read() == "house cam\n", "a camera that reports is remembered in JARVIS's folder")
with detections._lock:
    detections._latest.clear()
    detections._detecting = None
check(detections.asked("what has my house cam detected") == house,
      "and after a restart, before it reports again, the question is still the house cam's")
check(detections.asked("is anyone on my room cam") is None, "a camera that has never reported is still not claimed")
with detections._lock:
    detections._cannot_look["house cam"] = time.monotonic()
check(detections.asked("what has my house cam detected") is None,
      "one remembered but no longer answering a request to look is left as before")
with detections._lock:
    detections._cannot_look.clear()
detections.record("house cam", report(("person", 0.88), ("cup", 0.81), ("cup", 0.6)))

check(detections.summary(house) == "Your house cam sees a person and two cups, sir.",
      f"answered in words ({detections.summary(house)!r})")
detections.record("house cam", report())
check(detections.summary(house) == "Nothing your house cam recognises is in view, sir.", "and when nothing is there")
detections._latest["house cam"]["at"] -= 600
check("last reported 10 minutes ago" in detections.summary(house), "a stale report says how old it is")

# ---- asked: a look taken now --------------------------------------------------------------------------------

import threading  # noqa: E402

detections.forget()
detections.record("house cam", report())
with patch.object(detections, "LOOK_WAIT", 0.2):
    started = time.monotonic()
    first = detections.look_now(house)
    waited = time.monotonic() - started
    started = time.monotonic()
    second = detections.look_now(house)
    again = time.monotonic() - started
check(first == second == "Nothing your house cam recognises is in view, sir." and waited >= 0.2 and again < 0.1
      and not detections.look_wanted("house cam"),
      "a board that does not answer a request to look is answered from its last report, and not waited on again")
detections.forget()
detections.record("house cam", report())


def fresh(*objects, now=()):
    looked = report(*now)
    return dict(report(*objects), fresh=True, now=looked["objects"])


check(detections.record("house cam", fresh()) is None and detections.record("house cam", {**report(), "fresh": True, "now": "x"})
      == "not a report", "a look taken on request is kept, and a bad one refused")


def board():
    for _ in range(200):
        if detections.look_wanted("house cam"):
            # sure of nothing yet -- it has only just been held up -- but this look saw it
            detections.record("house cam", fresh(now=(("laptop", 0.55),)))
            return
        time.sleep(0.01)


threading.Thread(target=board, daemon=True).start()
answer = detections.look_now(house)
check(answer == "Your house cam sees a laptop, sir." and not detections.look_wanted("house cam"),
      f"asked, the board is told to look, and the answer is what that look saw ({answer!r})")
with patch.object(detections, "LOOK_WAIT", 0.2):
    started = time.monotonic()
    answer = detections.look_now(house)
check(time.monotonic() - started < 1 and answer == "Nothing your house cam recognises is in view, sir.",
      f"a look that does not come is waited for only so long, then what the board is sure of answers ({answer!r})")
detections.record("house cam", fresh(now=(("cup", 0.6),)))
threading.Thread(target=board, daemon=True).start()
check("laptop" in detections.look_now(house), "and once it answers again, it is asked again")
with patch.object(detections, "LOOK_WAIT", 0.2):
    detections.look_now(house)
threading.Thread(target=board, daemon=True).start()
check("laptop" in detections.look_now(house),
      "a board that has answered before and misses one -- its live view open -- is still asked the next time")


def peeking():
    for _ in range(200):
        when = detections.look_asked_at("House Cam")
        if when is not None:
            peeked.append(when)
            detections.record("house cam", fresh())
            return
        time.sleep(0.01)


peeked = []
threading.Thread(target=peeking, daemon=True).start()
detections.look_now(house)
check(peeked and detections.look_asked_at("house cam") is None,
      "when a look was asked for is told, by any case of the name, until it is answered")
detections.forget()

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
    detections._look_wanted["house cam"] = time.monotonic()
    asked = client.post("/camera/wanted", json={"name": "house cam"}, headers=headers).get_json() or {}
    detections._look_wanted.clear()
    told = client.post("/camera/wanted", json={"name": "house cam"}, headers=headers).get_json() or {}
    check(asked.get("look") is True and told.get("look") is False,
          "/camera/wanted tells the board to take a look when JARVIS has been asked what it sees")
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
face_part = detect_part[detect_part.index("#ifdef JARVIS_FACES\n    // After the report"):]
send_face = sketch[sketch.index("static void sendFace("):sketch.index("// A decoded frame:")]
check("/camera/seen" in sketch and "\\\"box\\\"" in sketch and "image/jpeg" not in detect_part
      and "image/jpeg" in send_face,
      "the look itself sends what it found; a picture only from sendFace")
check("if (face >= FACE_SURE) {" in face_part and face_part.index("if (face >= FACE_SURE) {") < face_part.index("sendFace(face)")
      and "faceLooks < FACE_LOOKS" in face_part and "looked[i].label == 0" in face_part
      and "!faceSent" in face_part and face_part.index("sendFace(face);") < face_part.index("faceSent = true;"),
      "and only a look's picture with a face in it, the first one only, from the first looks with someone in them")
check(face_part.index("face_run_model_int8_input();") < face_part.index("femto_prof_report(faceProfile")
      < face_part.index("if (face >= FACE_SURE) {"),
      "the profile build reports the face finder's own steps, apart from YOLO's")
check(detect_part.index("sendSeen(seen, count, took);") < detect_part.index("face_run_model_int8_input();"),
      "the face finder runs after the report, so JARVIS has begun the sighting the picture belongs to")
check('#if __has_include("face_model.h")' in sketch and "-I faces" in ini and "+<faces/*.c>" in ini
      and "-D FEMTO_NO_FLOAT_INPUT" in ini and "face_MODEL_INPUT_W" in sketch and "320" not in send_face,
      "the face finder is built only when its sources were made, sized from them, and no float inputs taken")
said_check = sketch[sketch.index("void sayDepthwiseCheck"):sketch.index("void takeFastBuffer")]
check("esp_nn_depthwise_conv_s8_simd_self_check(void) __attribute__((weak))" in sketch
      and "if (!esp_nn_depthwise_conv_s8_simd_self_check)" in said_check
      and sketch.index("esp_nn_conv_s8_simd_set_cores(2)") < sketch.index("sayDepthwiseCheck();"),
      "the depthwise check is said once, after both cores are set, and weak for an older FEMTO-ML")
gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("arduino/jarvis_camera/detect/" in gitignore and "arduino/jarvis_camera/faces/" in gitignore,
      "the made sources are kept out of git")

from tools import femto_build  # noqa: E402

header = femto_build.labels_header(["person", "bicycle", "cup"])
check("#define LABEL_COUNT 3" in header and '  "person",' in header, "labels.h numbers the model's names")
check(femto_build.INPUT == [1, 3, 256, 256] and "MODEL_SIZE = 256" in sketch,
      "the builder expects the input the sketch letterboxes to")

# The face finder: tuned on frames spread over every sighting; without what it needs, left out.
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as made:
    made = Path(made)

    for sighting in range(4):
        (made / f"house-cam-{sighting}").mkdir()

        for frame in range(50):
            (made / f"house-cam-{sighting}" / f"frame-{frame:04d}.jpg").write_bytes(b"")

    picked = femto_build.calibration_frames(made, at_most=20)
    check(len(picked) == 20 and len({path.parent.name for path in picked}) == 4 and picked == sorted(picked)
          and femto_build.calibration_frames(made, at_most=20) == picked,
          "the face finder is tuned on frames spread evenly over all the sightings, the same each time")
    check(len(femto_build.calibration_frames(made / "none")) == 0, "and none when there are no sightings")
    old_femto = made / "old-femto"
    (old_femto / "nhwc_compiler").mkdir(parents=True)
    faces_out = made / "faces"
    faces_out.mkdir()
    check(femto_build.build_faces(old_femto, out=faces_out) is None and not faces_out.exists(),
          "a FEMTO-ML without --prefix: no face finder, and none left from before")
    (old_femto / "nhwc_compiler" / "prefix.py").write_text("", encoding="ascii")
    check(femto_build.build_faces(old_femto, out=faces_out, float_model=made / "nothing.onnx",
                                  int8_model=made / "nothing-int8.onnx") is None,
          "no YuNet: no face finder")
    check(femto_build.build_faces(old_femto, out=faces_out, float_model=made / "house-cam-0" / "frame-0000.jpg",
                                  int8_model=made / "nothing-int8.onnx", sightings=made / "none") is None,
          "too few sightings' frames to tune it: no face finder")
check(femto_build.FACE_W == 320 and femto_build.FACE_ROWS == 240 and femto_build.FACE_H % 32 == 0
      and "JPEG_IMAGE_SCALE_1_2" in sketch,
      "it is sized for the board's half-size picture, padded to whole rows of 32")

detections.forget()
cameras.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
