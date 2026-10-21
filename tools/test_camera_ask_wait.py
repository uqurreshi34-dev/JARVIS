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
    check(code == 200 and answer == {"snap": False, "live": False, "look": False} and took < 0.5,
          f"without a wait, answered at once, as before ({took:.2f}s)")

    code, answer, took = ask({"name": "house cam", "wait": 1})
    check(answer == {"snap": False, "live": False, "look": False} and 0.9 <= took < 1.8,
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

# ---- the sketch ---------------------------------------------------------------------------------------------

sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="utf-8")
asking = sketch[sketch.index("Wanted pictureWanted()"):sketch.index("// Live view's own connection")]
check("ASK_WAIT_SECONDS = 10UL" in sketch and '\\"wait\\":' in asking, "the board asks with a wait")
check("POST_TIMEOUT_MS + ASK_WAIT_SECONDS * 1000UL" in asking, "and waits for the answer longer than that")
check(cameras.ASK_WAIT_MAX >= 10, "JARVIS lets it wait that long")
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
