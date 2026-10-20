"""A wifi camera's live view: one encrypted connection, frame after frame, while its picture is shown.

    python tools/test_camera_live.py

No camera and no network: frames are made here and sent to JARVIS's own
phone server, on this machine, exactly as the ESP32-CAM sends them -- a
chunked POST, each frame its length in four bytes then the JPEG. Checked:

- showing a camera asks for live view, describing one does not, and a
  camera that sends nothing ends it;
- the poll answers "live" while the view is wanted;
- frames are kept, handed to the panel, and keep the camera counted as
  online; a bad frame, a stream that stops, or the view being put away
  ends it;
- the server refuses a stream nobody asked for, and a wrong token, and
  ends one when the picture is put away, answering the camera;
- the firmware sends what the server reads.
"""

import io
import socket
import struct
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


def frames(count, body=JPEG):
    return b"".join(struct.pack(">I", len(body)) + body for _ in range(count))


# ---- receiving a stream ----------------------------------------------------------------------

cameras.forget()
sensors.reset()
seen = []
cameras.set_live_listener(lambda name, frame: seen.append((name, len(frame))))

check(not cameras.live_wanted("my room cam"), "nothing is live until asked")
cameras.watch("My Room Cam")
check(cameras.live_wanted("my room cam"), "watching a camera wants it live, whatever case its name is in")

kept, seconds, ended = cameras.receive_live("my room cam", io.BytesIO(frames(5)))
check(kept == 5 and len(seen) == 5 and seen[0] == ("my room cam", len(JPEG)),
      "each frame is kept and handed to the panel")
check(ended == "the camera stopped sending", f"a stream that ends says so ({ended})")
check(cameras.latest("my room cam") == JPEG, "the newest frame is the picture")
check(cameras.is_camera("my room cam") and "my room cam" in sensors.known(),
      "a streaming camera counts as connected, on the plan and the sensor panel")

bad = struct.pack(">I", cameras.MAX_BYTES + 1) + b"x"
check(cameras.receive_live("my room cam", io.BytesIO(bad))[0] == 0, "a frame claiming to be too large ends it")
check("not a JPEG" in cameras.receive_live("my room cam", io.BytesIO(frames(1, b"hello")))[2],
      "so does one that is not a JPEG")
check("part way" in cameras.receive_live("my room cam", io.BytesIO(frames(1)[:-10]))[2],
      "and one cut short")

# A detection look asked for while it streams: the stream ends so the board hears it, then comes back.
asked_at = {}
asked_at["my room cam"] = time.monotonic() + 60   # asked once this stream is under way
kept, _, ended = cameras.receive_live("my room cam", io.BytesIO(frames(3)), look_asked_at=asked_at.get)
check(kept == 0 and ended == "a detection look was asked for",
      f"a look asked for while it streams ends the stream, so the board hears it ({ended})")
asked_at["my room cam"] = time.monotonic() - 1    # asked before the stream it comes back with
kept, _, ended = cameras.receive_live("my room cam", io.BytesIO(frames(3)), look_asked_at=asked_at.get)
check(kept == 3, "and the stream it comes back with, begun after the asking, is not ended for it again")

cameras.stop_watching()
check(cameras.receive_live("my room cam", io.BytesIO(frames(3)))[0] == 0, "put away, no frame is taken")

cameras.watch("my room cam", seconds=0.2)
time.sleep(0.3)
check(not cameras.live_wanted("my room cam"), "a view ends by itself after its time")

cameras.watch("my room cam")
cameras.watch("street cam")
cameras.stop_watching("my room cam")
check(not cameras.live_wanted("my room cam") and cameras.live_wanted("street cam"), "one camera's view ends alone")
cameras.stop_watching()


# ---- showing and describing ------------------------------------------------------------------

cameras.forget()
sensors.reset()
sensors.report({"name": "my room cam", "event": "online"})
watched = []


def answer_poll():
    """The camera: polls, and on 'live' streams three frames."""
    for _ in range(100):
        time.sleep(0.02)
        snap = cameras.wanted("my room cam")
        live = cameras.live_wanted("my room cam")
        watched.append((snap, live))

        if live:
            cameras.receive_live("my room cam", io.BytesIO(frames(3)))
            return

        if snap:
            cameras.store("my room cam", JPEG)
            return


poller = threading.Thread(target=answer_poll)
poller.start()
said, frame = cameras.picture("show me my room cam")
poller.join()
check(frame == JPEG and said == "Here's your room cam, sir.", "showing a camera shows its first frame")
check(watched and watched[-1] == (True, True), "the camera was asked for live view as well as the picture")
check(cameras.live_wanted("my room cam"), "and it stays live while shown")

cameras.stop_watching()
watched.clear()
poller = threading.Thread(target=answer_poll)
poller.start()
said, frame = cameras.picture("what's happening in my room cam")
poller.join()
check(frame == JPEG and not cameras.live_wanted("my room cam") and watched[-1] == (True, False),
      "describing takes one picture, not live view")

with_nothing = cameras.fetch.__defaults__
cameras.fetch.__defaults__ = (0.2,)
said, frame = cameras.picture("show me my room cam")
cameras.fetch.__defaults__ = with_nothing
check(frame is None and not cameras.live_wanted("my room cam"), "a camera that sends nothing is not left live")


# ---- the server: a real HTTP connection, as the camera makes it ------------------------------

try:
    import pywebpush  # noqa: F401
except ImportError:
    # Only for running here without it: the phone server imports it for the phone's notifications.
    sys.modules["pywebpush"] = types.SimpleNamespace(webpush=None, WebPushException=Exception)

try:
    import phone
    from werkzeug.serving import ThreadedWSGIServer
except Exception as error:
    print(f"SKIP the live endpoint (could not import phone: {error})")
else:
    server = phone.PhoneServer(token="right-token")
    http = ThreadedWSGIServer("127.0.0.1", 0, server._app)
    port = http.server_address[1]
    threading.Thread(target=http.serve_forever, daemon=True).start()

    def open_stream(token="right-token"):
        link = socket.create_connection(("127.0.0.1", port), timeout=5)
        link.sendall((f"POST /camera/live HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Jarvis-Token: {token}\r\n"
                      "X-Jarvis-Camera: my room cam\r\nContent-Type: application/octet-stream\r\n"
                      "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode())
        return link

    def send_frame(link, body=JPEG):
        frame = struct.pack(">I", len(body)) + body
        link.sendall(f"{len(frame):X}\r\n".encode() + frame + b"\r\n")

    def reply(link):
        data = b""

        while True:
            try:
                piece = link.recv(4096)
            except socket.timeout:
                break

            if not piece:
                break

            data += piece

        return data.decode(errors="replace")

    cameras.forget()
    sensors.reset()
    seen.clear()

    link = open_stream()
    link.sendall(b"0\r\n\r\n")
    check(" 409 " in reply(link).split("\r\n")[0], "a stream nobody asked for is refused")

    cameras.watch("my room cam")
    link = open_stream("wrong-token")
    check(" 403 " in reply(link).split("\r\n")[0], "a wrong token is refused")

    link = open_stream()

    for _ in range(10):
        send_frame(link)

    deadline = time.monotonic() + 3

    while len(seen) < 10 and time.monotonic() < deadline:
        time.sleep(0.02)

    check(len(seen) == 10, f"frames sent down one connection reach the panel ({len(seen)} of 10)")

    cameras.stop_watching()
    send_frame(link)
    answer = reply(link)
    check(" 200 " in answer.split("\r\n")[0] and '"frames"' in answer,
          "putting the picture away ends the stream, and the camera is answered")

    http.shutdown()


# ---- the panel: live frames replace the picture until it is put away ---------------------------

try:
    import commands
except Exception as error:
    print(f"SKIP the panel wiring (could not import commands: {error})")
else:
    shown = []
    previous = commands._camera_listener
    commands.set_camera_listener(lambda data, title: shown.append((len(data), title)))
    cameras.set_live_listener(commands._live_frame)
    cameras.forget()
    sensors.reset()
    sensors.report({"name": "my room cam", "event": "online"})

    poller = threading.Thread(target=answer_poll)
    poller.start()
    commands._camera_picture("show me my room cam")
    poller.join()
    check(shown and shown[0] == (len(JPEG), "MY ROOM CAM"), "the first frame opens the panel")
    check(len(shown) >= 2 and all(title == "MY ROOM CAM" for _size, title in shown),
          f"and each live frame replaces it ({len(shown)} shown)")

    commands._stop_looking()
    check(not cameras.live_wanted("my room cam") and shown[-1] == (0, ""),
          "putting it away closes the panel and ends live view")

    before = len(shown)
    commands._live_frame("my room cam", JPEG)
    check(len(shown) == before, "a frame arriving after that is not shown")

    commands.set_camera_listener(previous)
    cameras.set_live_listener(commands._live_frame)


# ---- the panel window: a new frame replaces the picture, without moving the window ------------

try:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPoint
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication

    import camera_panel
except Exception as error:
    print(f"SKIP the panel window (could not load Qt: {error})")
else:
    app = QApplication.instance() or QApplication([])

    def jpeg(colour):
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor(colour))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "JPG")
        return bytes(data)

    panel = camera_panel.CameraPanel()
    panel._on_show(jpeg("red"), "MY ROOM CAM")
    panel.move(QPoint(40, 40))
    moved_to = panel.pos()
    panel._on_show(jpeg("blue"), "MY ROOM CAM")
    check(panel.pos() == moved_to and panel._image.pixelColor(10, 10).blue() > 150,
          "a live frame replaces the picture where you put the window")
    panel._on_show(jpeg("green"), "CAMERA")
    check(panel._title == "CAMERA", "a different picture takes the panel as before")
    panel._on_hide()


# ---- the firmware sends what the server reads -------------------------------------------------

sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="utf-8")
check("POST /camera/live HTTP/1.1" in sketch and "Transfer-Encoding: chunked" in sketch,
      "the sketch streams to /camera/live, chunked")
check("frame->len >> 24" in sketch and "X-Jarvis-Token" in sketch and "X-Jarvis-Camera" in sketch,
      "each frame led by its length in four bytes, with the token and its name")
check('\\"live\\":true' in sketch and "setCACert(JARVIS_CA)" in sketch and "liveLink.setCACert" in sketch,
      "it streams only when asked, and only to JARVIS's own certificate")

check('esp_log_level_set("cam_hal", ESP_LOG_NONE)' in sketch
      and sketch.index("esp_log_level_set") < sketch.index("esp_camera_init"),
      "the camera driver's task is kept from printing, which overflowed its stack, before it starts")


# ---- "the room cam" is heard as said, not as "the room come" ---------------------------------

try:
    import transcriber
except Exception as error:
    print(f"SKIP the transcription prompt (could not import transcriber: {error})")
else:
    sensors.reset()
    check("Devices:" not in transcriber._prompt(), "with no boards, the prompt is as before")
    sensors.report({"name": "my room cam", "event": "online"})
    sensors.report({"name": "room", "temperature": 21.0})
    check("Devices: my room cam, room." in transcriber._prompt(),
          "the boards on the wifi are named in Whisper's prompt, so 'cam' is expected")

check("LIVE_MISSES_ALLOWED" in sketch and "continue;" in sketch.split("LIVE_MISSES_ALLOWED) {")[1][:200],
      "live view carries on past a frame the camera fails to take")
check(cameras.LIVE_READ_TIMEOUT > 8, "and JARVIS waits longer than two of the camera's own four-second waits")


# ---- a camera's JPEG is described as a JPEG ---------------------------------------------------

try:
    import providers
except Exception as error:
    print(f"SKIP the image type (could not import providers: {error})")
else:
    check(providers.image_mime(JPEG) == "image/jpeg", "a JPEG is sent as image/jpeg, whatever the default says")
    check(providers.image_mime(b"\x89PNG\r\n\x1a\n...") == "image/png", "a PNG as image/png")
    check(providers.image_mime(b"RIFF0000WEBPVP8 ") == "image/webp", "a WebP as image/webp")
    check(providers.image_mime(b"unknown", "image/jpeg") == "image/jpeg", "and unknown bytes keep the type given")

cameras.set_live_listener(None)
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
