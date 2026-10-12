"""A wifi camera watched from the phone: the phone page plays its live view, from home or away.

    python tools/test_camera_remote.py

No camera, no phone: frames are put in as the camera's live stream puts
them, and JARVIS's own phone server is run on this machine. Checked:

- the phone is told the cameras, and plays one as MJPEG over its token;
- watching keeps the camera live, at no more frames than the phone needs,
  and closing the picture on the desk does not end the phone's view;
- the phone going away lets the camera stop;
- a command shown from the phone ("show me the room cam") opens the
  view there, and the page has what plays it.
"""

import os
import socket
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import cameras, sensors  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 200

# ---- the stream itself -------------------------------------------------------------------------

cameras.forget()
sensors.reset()
cameras.store("my room cam", JPEG)

frames = cameras.remote_frames("my room cam", fps=100, wait=0.5)
first = next(frames)
check(first.startswith(b"--jarvisframe\r\nContent-Type: image/jpeg\r\n") and first.endswith(JPEG + b"\r\n"),
      "each frame is a part of a multipart JPEG stream")
check(cameras.live_wanted("my room cam"), "watching from the phone keeps the camera live")

cameras.watch("my room cam")
cameras.stop_watching()
check(cameras.live_wanted("my room cam"), "closing the picture on the desk does not end the phone's view")


def feed_frames(count, every):
    for _ in range(count):
        time.sleep(every)
        cameras.store("my room cam", JPEG)


threading.Thread(target=feed_frames, args=(20, 0.02), daemon=True).start()
started = time.monotonic()
sent = 0

for part in cameras.remote_frames("my room cam", fps=10, wait=0.5):
    sent += 1

took = time.monotonic() - started
check(sent >= 2 and sent / took <= 12, f"no more frames than the phone is to have ({sent} in {took:.1f}s)")

with cameras._lock:
    cameras._leases["my room cam"] = time.monotonic() - 1

check(not cameras.live_wanted("my room cam"), "a phone that stops watching lets its lease run out, and the camera stop")
check(list(cameras.remote_frames("nobody", wait=0.2)) == [], "a camera that sends nothing ends the stream")


# ---- the phone server ------------------------------------------------------------------------

try:
    import pywebpush  # noqa: F401
except ImportError:
    sys.modules["pywebpush"] = types.SimpleNamespace(webpush=None, WebPushException=Exception)

try:
    import phone
    from werkzeug.serving import ThreadedWSGIServer
except Exception as error:
    print(f"SKIP the phone server (could not import phone: {error})")
    phone = None

if phone is not None:
    cameras.forget()
    sensors.reset()
    sensors.report({"name": "my room cam", "event": "online"})
    cameras.wanted("my room cam")
    cameras.store("my room cam", JPEG)

    server = phone.PhoneServer(token="phone-token")
    server.set_handler(lambda text: "Here's your room cam, sir.")
    http = ThreadedWSGIServer("127.0.0.1", 0, server._app)
    port = http.server_address[1]
    threading.Thread(target=http.serve_forever, daemon=True).start()
    client = server._app.test_client()

    check(client.get("/cameras").status_code == 403, "the cameras need the token")
    listed = client.get("/cameras?t=phone-token").get_json()["cameras"]
    check({"name": "my room cam", "looks": "", "online": True} in listed, f"and are listed with it ({listed})")
    check(client.get("/camera/watch?t=phone-token&name=nobody").status_code == 404, "an unknown camera is refused")
    check(client.get("/camera/watch?name=my%20room%20cam").status_code == 403, "watching needs the token")

    answer = client.post("/command?t=phone-token", json={"text": "show me the room cam"}).get_json()
    check(answer.get("camera") == {"name": "my room cam", "title": "MY ROOM CAM"},
          f"a camera shown from the phone opens there ({answer.get('camera')})")
    answer = client.post("/command?t=phone-token", json={"text": "what's the weather"}).get_json()
    check(answer.get("camera") is None, "and nothing else does")

    # A real connection, as the phone's <img> makes it.
    link = socket.create_connection(("127.0.0.1", port), timeout=5)
    link.sendall(f"GET /camera/watch?t=phone-token&name=my%20room%20cam HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
                 .encode())
    threading.Thread(target=feed_frames, args=(10, 0.05), daemon=True).start()
    received = b""
    deadline = time.monotonic() + 3

    while received.count(b"--jarvisframe") < 3 and time.monotonic() < deadline:
        received += link.recv(65536)

    head = received.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
    check("200" in head.split("\r\n")[0] and "multipart/x-mixed-replace; boundary=jarvisframe" in head,
          "the phone is sent MJPEG")
    check(received.count(b"--jarvisframe") >= 3, f"frame after frame ({received.count(b'--jarvisframe')})")
    link.close()
    http.shutdown()

    page = phone.PAGE
    check('id="feeds-open"' in page and 'id="feed-img"' in page, "the page has a cameras button and a view")
    check("/camera/watch?t=" in page and "data.camera" in page and "visibilitychange" in page,
          "it plays the stream, opens it for a camera command, and stops when the phone is put away")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
