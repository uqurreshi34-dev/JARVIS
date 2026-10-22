"""A camera's ask that waits: POST /camera/wanted with "wait" (phone.py, actions/cameras.py, the sketch).

    python tools/test_camera_ask_wait.py

Every ask is a TLS handshake on the board, and one a second took about 0.4 s of every detection look
(20 October 2026). So the board asks with a wait, and JARVIS holds the ask until something is wanted
-- a picture, live view, a look -- and answers the moment it is, or with nothing when the wait is up.
Checked:

- without "wait", the answer comes at once and is what it always was (a board flashed before);
- with it, nothing wanted: the answer comes when the wait is up, and the wait is capped;
- something asked for meanwhile -- a picture, live view, a look -- is answered at once;
- a nonsense wait is no wait; the waiting board still counts as connected;
- the sketch asks with a wait, and waits for the answer longer than that.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

for _name in ("win32api", "win32con", "win32gui", "win32process", "win32event", "pythoncom", "sounddevice",
              "soundfile", "edge_tts", "pywebpush"):
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = MagicMock()

from actions import cameras, detections, sensors  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


sensors.reset()
cameras.forget()
detections.forget()

try:
    import phone
except Exception as error:
    print(f"SKIP the phone (could not import phone: {error})")
    phone = None

if phone is not None:
    server = phone.PhoneServer(token="ask-test")
    client = server._app.test_client()
    headers = {"X-Jarvis-Token": "ask-test", "X-Jarvis-Camera": "house cam"}

    def ask(payload):
        started = time.monotonic()
        answer = client.post("/camera/wanted", json=payload, headers=headers)
        return answer.status_code, answer.get_json() or {}, time.monotonic() - started

    code, answer, took = ask({"name": "house cam"})
    check(code == 200 and answer == {"snap": False, "live": False, "look": False, "still": False} and took < 0.5,
          f"without a wait, answered at once, as before ({took:.2f}s)")

    code, answer, took = ask({"name": "house cam", "wait": 1})
    check(answer == {"snap": False, "live": False, "look": False, "still": False} and 0.9 <= took < 1.8,
          f"with one, nothing wanted: answered when the wait is up ({took:.2f}s)")
    check(cameras.is_camera("house cam") and any(c["name"] == "house cam" and c["online"]
                                                 for c in cameras.cameras()),
          "a board waiting on its ask counts as connected")

    for wait in ("10", True, -5, None, [3]):
        code, answer, took = ask({"name": "house cam", "wait": wait})
        check(code == 200 and took < 0.5, f"a wait of {wait!r} is no wait ({took:.2f}s)")

    def later(seconds, action):
        timer = threading.Timer(seconds, action)
        timer.start()
        return timer

    later(0.4, lambda: cameras.want("house cam"))
    code, answer, took = ask({"name": "house cam", "wait": 10})
    check(answer.get("snap") is True and took < 1.5, f"a picture asked for meanwhile is answered at once ({took:.2f}s)")

    later(0.4, lambda: cameras.watch("house cam", 5))
    code, answer, took = ask({"name": "house cam", "wait": 10})
    check(answer.get("live") is True and took < 1.5, f"so is live view ({took:.2f}s)")
    cameras.stop_watching()

    def look():
        with detections._lock:
            detections._look_wanted["house cam"] = time.monotonic()
        cameras.changed()

    later(0.4, look)
    code, answer, took = ask({"name": "house cam", "wait": 10})
    check(answer.get("look") is True and took < 1.5, f"and a detection look ({took:.2f}s)")
    detections._look_wanted.clear()

    real = cameras.ASK_WAIT_MAX
    cameras.ASK_WAIT_MAX = 0.5
    code, answer, took = ask({"name": "house cam", "wait": 60})
    cameras.ASK_WAIT_MAX = real
    check(took < 1.2, f"the wait is capped ({took:.2f}s)")

    # A look JARVIS asks for (the question "what has the house cam detected") is heard by a waiting ask.
    heard = {}

    def board():
        heard["answer"] = ask({"name": "house cam", "wait": 10})

    waiting = threading.Thread(target=board)
    waiting.start()
    time.sleep(0.3)
    camera = {"name": "house cam"}
    detections.look_now.__globals__["LOOK_WAIT"] = 0.6
    detections.look_now(camera)
    waiting.join(5)
    check(heard.get("answer") and heard["answer"][1].get("look") is True and heard["answer"][2] < 1.5,
          "asked what the camera sees, the board waiting on its ask hears it at once")

    # ---- stills: asked of a board that can take one, answered once; a stream ends for one -------------------

    later(0.4, lambda: cameras.want_still("house cam"))
    code, answer, took = ask({"name": "house cam", "wait": 10})
    check(answer.get("still") is True and took < 1.5, f"a still asked for is answered at once ({took:.2f}s)")
    code, answer, took = ask({"name": "house cam"})
    check(answer.get("still") is False, "and once: the next ask is not asked for it again")
    real_still_wait = cameras.STILL_WAIT_SECONDS
    cameras.STILL_WAIT_SECONDS = 0.3
    cameras.want_still("house cam")
    time.sleep(0.5)
    code, answer, took = ask({"name": "house cam"})
    cameras.STILL_WAIT_SECONDS = real_still_wait
    check(answer.get("still") is False, "a still not taken in STILL_WAIT_SECONDS (a board that cannot) is let go")

    import io
    import struct

    class Slow(io.RawIOBase):
        """A live stream sending a small frame every 0.1 s, for ever."""
        frame = b"\xff\xd8\xff\xe0" + b"x" * 100

        def __init__(self):
            self.pending = b""

        def readable(self):
            return True

        def readinto(self, buffer):
            if not self.pending:
                time.sleep(0.1)
                self.pending = struct.pack(">I", len(self.frame)) + self.frame

            count = min(len(buffer), len(self.pending))
            buffer[:count] = self.pending[:count]
            self.pending = self.pending[count:]
            return count

    cameras.watch("house cam", 30)
    ended = {}
    streaming = threading.Thread(target=lambda: ended.update(
        result=cameras.receive_live("house cam", io.BufferedReader(Slow()))))
    streaming.start()
    time.sleep(0.5)
    cameras.want_still("house cam")
    streaming.join(5)
    cameras.stop_watching()
    check(ended.get("result") and ended["result"][2] == "a still was asked for" and ended["result"][0] > 0,
          f"a still asked for while the board streams ends the stream, so its next ask hears it ({ended.get('result')})")
    code, answer, took = ask({"name": "house cam"})
    check(answer.get("still") is True, "and that ask is told")

    # A still the board sends down its live stream itself, between frames, flagged.
    kept_live = []
    cameras.set_still_listener(lambda camera, jpeg: kept_live.append(jpeg))
    still_bytes = b"\xff\xd8\xff\xe0" + b"S" * 4000
    frame_bytes = b"\xff\xd8\xff\xe0" + b"f" * 100
    body = (struct.pack(">I", len(frame_bytes)) + frame_bytes
            + struct.pack(">I", len(still_bytes) | cameras.STILL_FLAG) + still_bytes
            + struct.pack(">I", len(frame_bytes)) + frame_bytes)
    cameras.watch("house cam", 30)
    frames_in, _, why = cameras.receive_live("house cam", io.BytesIO(body))
    cameras.stop_watching()
    check(kept_live == [still_bytes] and frames_in == 2 and cameras.latest("house cam") == frame_bytes,
          f"a still the board sends down its live stream is passed on, and is not a frame ({frames_in}; {why})")
    cameras.set_still_listener(None)

    kept = []
    cameras.set_still_listener(lambda camera, jpeg: kept.append((camera, jpeg)))
    still_headers = dict(headers, **{"Content-Type": "image/jpeg"})
    picture = b"\xff\xd8\xff\xe0" + b"s" * 5000
    check(client.post("/camera/still", data=picture, headers={"X-Jarvis-Camera": "house cam"}).status_code == 403,
          "/camera/still needs the token")
    check(client.post("/camera/still", data=b"not a jpeg", headers=still_headers).status_code == 400,
          "and a JPEG")
    check(client.post("/camera/still", data=b"\xff\xd8\xff" + b"x" * (cameras.MAX_STILL_BYTES + 1),
                      headers=still_headers).status_code == 413, "no larger than MAX_STILL_BYTES")
    check(client.post("/camera/still", data=picture, headers=still_headers).status_code == 200
          and kept == [("house cam", picture)], "and passes it on as it came")
    cameras.set_still_listener(None)

# ---- the sketch ---------------------------------------------------------------------------------------------

sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="utf-8")
asking = sketch[sketch.index("Wanted pictureWanted()"):sketch.index("// Live view's own connection")]
check("ASK_WAIT_SECONDS = 10UL" in sketch and '\\"wait\\":' in asking, "the board asks with a wait")
check("POST_TIMEOUT_MS + ASK_WAIT_SECONDS * 1000UL" in asking, "and waits for the answer longer than that")
check(cameras.ASK_WAIT_MAX >= 10, "JARVIS lets it wait that long")
setup = sketch[sketch.index("void setup()"):sketch.index("void loop()")]
check(setup.index("reserveDetection();") < setup.index("cameraReady = startCamera();"),
      "the board takes the model's memory before the camera sizes its buffers")
check("STILL_SIZES[] = {FRAMESIZE_QXGA, FRAMESIZE_UXGA, FRAMESIZE_SXGA}" in sketch
      and "ESP.getFreePsram() >= STILL_PSRAM_SPARE" in sketch and "set_framesize(sized, FRAMESIZE_VGA)" in sketch,
      "and starts the camera at the largest still size its memory allows, then VGA")
check("framesize_t taken = sized->status.framesize;" in sketch and "stillSize = taken;" in sketch,
      "the still size is the one the camera took, not the one asked for (an OV2640 is given its 1600 x 1200)")
ov2640 = sketch[sketch.index("OV2640_PID) {"):sketch.index('Serial.println("[jarvis] camera: OV2640")')]
check("set_aec2(sensor, 1)" in ov2640 and "set_gainceiling(sensor, OV2640_GAIN_CEILING)" in ov2640
      and "OV2640_GAIN_CEILING = GAINCEILING_16X" in sketch,
      "the OV2640 (the house cam) gains up to 16x in the dark, not the driver's 2x")
grab = sketch[sketch.index("camera_fb_t *grabStill()"):sketch.index("void stillDone(")]
done = sketch[sketch.index("void stillDone("):sketch.index("void takeStill()")]
asked = sketch[sketch.index("void takeStill()"):sketch.index("void streamLive()")]
check("set_framesize(sensor, stillSize)" in grab and "STILL_SETTLE_FRAMES" in grab
      and "set_framesize(sensor, FRAMESIZE_VGA)" in done and 'post("/camera/still"' in asked
      and "stillDone(frame)" in asked, "a still: the size changed, settled, sent, and set back")
live = sketch[sketch.index("void streamLive()"):sketch.index("void sendPicture()")]
check("LIVE_STILL_FLAG = 0x40000000UL" in sketch and cameras.STILL_FLAG == 0x40000000
      and "(uint32_t)still->len | LIVE_STILL_FLAG" in live and "STILL_EVERY_MS" in live,
      "while someone is in view the board sends a still down its live stream every STILL_EVERY_MS, flagged")
check("STILL_SETTLE_FRAMES = 4;" in sketch and "|| stillTaking) {" in sketch,
      "letting four frames go while the camera settles, detection holding off meanwhile")
looping = sketch[sketch.index("Wanted wanted = pictureWanted();"):sketch.index("if (!cameraReady)")]
check('"\\"still\\":true"' in sketch and looping.index("takeStill()") < looping.index("streamLive()"),
      "asked for a still, the board takes it before going back to live view")
joining = sketch[sketch.index("WiFi.mode(WIFI_STA);"):sketch.index("[jarvis] wifi not joined yet")]
check("WiFi.setSleep(false);" in joining and "WiFi.setTxPower(WIFI_POWER_19_5dBm);" in joining
      and joining.index("WiFi.begin(") < joining.index("WiFi.setTxPower("),
      "the board's radio is kept awake and at full strength, set once the wifi has begun")
check("WiFi.RSSI()" in sketch, "and it says how strong its signal is on joining")
ov3660 = sketch[sketch.index("OV3660_PID) {"):sketch.index('Serial.println("[jarvis] camera: OV3660")')]
check("set_aec2(sensor, 1)" in ov3660 and "OV3660_GAIN_CEILING" in ov3660 and "OV3660_GAIN_CEILING = 0x1F0" in sketch,
      "the OV3660 may expose longer than a frame and gain up to 31x in the dark")

cameras.forget()
detections.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
