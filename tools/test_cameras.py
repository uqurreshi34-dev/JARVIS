"""Cameras on the wifi: asked for a picture, get one back, show it.

Runs in a sandboxed JARVIS folder with a house plan whose window looks at
the street. Checked:

- no camera known, nothing claimed: the PC's own webcam commands stay theirs;
- a camera named after its room ("my room cam") looks through its window,
  and is asked for by what it looks at or its own name: "show me the
  street", "what's happening on the street", "what's outside", "show me my
  room cam"; other commands are left alone;
- a request is answered once, and lapses; a picture is a JPEG of sensible
  size or it is refused; asking waits for a picture taken after the asking;
- a camera that is not online says so, and one that never answers says so;
- the phone server's /camera/wanted and /camera: the token, the size checked
  before the body is read, a JPEG only;
- through commands: "show me the street" shows the picture, no model call.

    python tools/test_cameras.py
"""

import json
import os
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import cameras, sensors  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


JPEG = b"\xff\xd8\xff\xe0" + b"picture" * 20

with open(os.path.join(folder, "house.json"), "w", encoding="utf-8") as handle:
    json.dump({"places": {"home": {"rooms": [
        {"name": "my room", "aliases": ["room"], "x": 0, "y": 0, "w": 3.8, "h": 3.4,
         "features": [{"wall": "north", "at": 0.5, "width": 1.6, "kind": "window", "looks": "street"}]},
    ]}}}, handle)

sensors.reset()
cameras.forget()

# ---- what is said --------------------------------------------------------------------------------

check(cameras.asked("show me the street") is None, "no camera online: 'show me the street' is not claimed")

sensors.report({"name": "my room cam", "event": "online"})
check([(camera["name"], camera["looks"], camera["room"]) for camera in cameras.cameras()]
      == [("my room cam", "street", "my room")], "a camera named after its room looks out of its window")

SAID = {
    "show me the street": "show",
    "jarvis show me the street camera please": "show",
    "let me see the street": "show",
    "show me my room cam": "show",
    "show me my room camera": "show",
    "what's happening on the street": "describe",
    "what's going on in the street": "describe",
    "what's outside": "describe",
}

for spoken, kind in SAID.items():
    request = cameras.asked(spoken)
    check(request is not None and request[0] == kind and request[1]["name"] == "my room cam",
          f"{spoken!r} -> {request and request[0]}")

for spoken in ("what can you see", "open the camera", "show me the news", "what's happening",
               "show me my room", "show me the street map of birmingham", "close the camera"):
    check(cameras.asked(spoken) is None, f"left alone: {spoken!r}")

# ---- asking and answering -----------------------------------------------------------------------

check(not cameras.wanted("my room cam"), "nothing wanted until asked")
cameras.want("my room cam")
check(cameras.wanted("my room cam") and not cameras.wanted("my room cam"), "a request is answered once")

cameras.want("my room cam")
cameras._wanted["my room cam"] -= cameras.WAIT_SECONDS + 1
check(not cameras.wanted("my room cam"), "and lapses if the camera never comes for it")

check(cameras.store("my room cam", b"") == "empty", "an empty picture is refused")
check(cameras.store("my room cam", b"GIF89a") == "not a JPEG", "so is anything but a JPEG")
check(cameras.store("my room cam", b"\xff\xd8\xff" + b"0" * cameras.MAX_BYTES) == "too large", "and anything too big")

check(cameras.store("my room cam", JPEG) is None and cameras.latest("my room cam") == JPEG, "a JPEG is kept")
old = cameras.latest("my room cam")


def camera_answers(delay=0.2, data=JPEG):
    def run():
        time.sleep(delay)

        for _ in range(100):
            if cameras.wanted("my room cam"):
                cameras.store("my room cam", data)
                return

            time.sleep(0.02)

    threading.Thread(target=run, daemon=True).start()


fresh = JPEG + b"new"
camera_answers(data=fresh)
said, frame = cameras.picture("show me the street")
check(said == "The street, sir." and frame == fresh, f"asking waits for a new picture, not the old one ({said!r})")
check(frame != old, "taken after the asking")

check(cameras.fetch("my room cam", timeout=0.3) is None, "a camera that never answers: nothing, after a wait")
check("my room cam" not in cameras._wanted, "and the request is withdrawn")

sensors._sensors["my room cam"]["seen"] -= 600
said, frame = cameras.picture("show me the street")
check(said == "The street camera isn't connected, sir." and frame is None, f"an offline camera says so ({said!r})")
sensors.report({"name": "my room cam", "event": "online"})

# ---- the phone server -------------------------------------------------------------------------------

try:
    import phone
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP phone endpoints (could not import phone: {error})")
else:
    server = phone.PhoneServer(token="camera-test")
    client = server._app.test_client()
    token = {"X-Jarvis-Token": "camera-test"}

    check(client.post("/camera/wanted", json={"name": "my room cam"}).status_code == 403, "/camera/wanted needs the token")
    check(client.post("/camera/wanted", json={"name": "my room cam"}, headers=token).get_json() == {"snap": False},
          "and says no when nothing is wanted")
    cameras.want("my room cam")
    check(client.post("/camera/wanted", json={"name": "my room cam"}, headers=token).get_json() == {"snap": True},
          "and yes when a picture is")

    picture_headers = dict(token, **{"X-Jarvis-Camera": "my room cam", "Content-Type": "image/jpeg"})
    check(client.post("/camera", data=JPEG, headers={"X-Jarvis-Camera": "my room cam"}).status_code == 403,
          "/camera needs the token")
    check(client.post("/camera", data=b"\xff\xd8\xff" + b"0" * cameras.MAX_BYTES, headers=picture_headers).status_code == 413,
          "and refuses a picture too big before reading it")
    check(client.post("/camera", data=b"not a picture", headers=picture_headers).status_code == 400, "or not a JPEG")
    answer = client.post("/camera", data=JPEG + b"phone", headers=picture_headers)
    check(answer.status_code == 200 and cameras.latest("my room cam") == JPEG + b"phone", "and keeps a JPEG")

# ---- through commands --------------------------------------------------------------------------------

try:
    import commands
except Exception as error:
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    shown = []
    commands.set_camera_listener(lambda image, title: shown.append((image, title)))
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        camera_answers(data=JPEG + b"cmd")
        result = commands.handle_command("show me the street")
        said = result["action"]()
        check(result["intent"] == "camera_picture" and said == "The street, sir."
              and shown and shown[-1] == (JPEG + b"cmd", "STREET CAM"),
              f"commands: 'show me the street' shows it, no model call ({said!r})")
        check(commands._fast_path("what can you see") is None
              or commands._fast_path("what can you see")["intent"] != "camera_picture",
              "and 'what can you see' is still the PC's own camera")
    finally:
        commands.run_agent = real_agent
        commands.set_camera_listener(None)

cameras.forget()
sensors.reset()
sys.exit(1 if failures else 0)
