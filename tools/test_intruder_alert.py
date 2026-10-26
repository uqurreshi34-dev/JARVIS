"""Movement at home while your phone is away reaches the phone at once, and opens the camera.

    python tools/test_intruder_alert.py

No board, no phone, no push service: reports are put in as a board sends
them, Tailscale's answer is set by hand, and the push is caught before it
leaves. Checked:

- movement with your phone away is an alarm; with it home, or a second
  movement, it is not, and what is said is as before;
- the alarm offers the online camera in the same room on the house plan,
  and nothing when there is none;
- it is pushed straight away, marked urgent, kept a while for a phone out
  of signal, carries the camera, and is sent even in quiet hours -- which
  still silence ordinary notices;
- the phone page collects it with its camera; the service worker opens
  that camera when the notification is tapped;
- the phones that asked for notifications are kept across a restart;
- main.py sends it to the phone itself, still says it in the room, and
  does not send it to the phone a second time, even when it waited
  behind a conversation.
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ---- the alarm itself --------------------------------------------------------------------------

phone_is = {"value": "away"}
sensors.set_presence(lambda: phone_is["value"])
sensors.set_namer(lambda board: {"room": "my room"}.get(board))


def arrive():
    sensors.reset()
    sensors.report({"name": "room", "event": "online"})
    return sensors.examine({"name": "room", "event": "motion"})


heard = arrive()
check(heard == {"name": "room", "say": "Movement in your room, sir, and your phone isn't home.", "alarm": True},
      f"movement with your phone away is an alarm ({heard})")
check(sensors.examine({"name": "room", "event": "motion"})["alarm"] is False,
      "and the movement after it is not another")

phone_is["value"] = "home"
heard = arrive()
check(heard["say"] == "Welcome back, sir." and heard["alarm"] is False, "with your phone home it is welcome back")

phone_is["value"] = "away"
sensors.reset()
check(sensors.report({"name": "room", "event": "online"}) == "Room sensor online, sir.",
      "report() still answers with only what to say")
check(sensors.examine("not a report") == {"name": None, "say": None, "alarm": False}, "and nonsense is nothing")


# ---- which camera it offers -------------------------------------------------------------------

listed = [
    {"name": "street cam", "looks": "street", "room": "room 1", "online": True},
    {"name": "room cam", "looks": "", "room": "my room", "online": True},
]

with patch.object(cameras.house, "room_of", side_effect=lambda board: {"room": "my room"}.get(board)), \
     patch.object(cameras, "cameras", return_value=listed):
    check(cameras.beside("room") == {"name": "room cam", "title": "ROOM CAM"},
          "the alarm offers the camera in the same room")
    check(cameras.beside("hall") is None, "and nothing for a board not on the plan")
    listed[1]["online"] = False
    check(cameras.beside("room") is None, "or when that camera is offline")


# ---- the phone ---------------------------------------------------------------------------------

try:
    import pywebpush  # noqa: F401
except ImportError:
    sys.modules["pywebpush"] = types.SimpleNamespace(webpush=None, WebPushException=Exception)

try:
    import phone
except Exception as error:
    # Only where the PC's own audio libraries are missing (a Linux sandbox); never on Windows.
    print(f"SKIP the phone and main.py (could not import phone: {error})")
    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
    sys.exit(1 if failed else 0)

sent = []


def fake_push(**kwargs):
    sent.append(kwargs)


class Now:
    """A thread that runs at once, so what was pushed can be checked."""

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


folder = tempfile.mkdtemp()
kept = os.path.join(folder, phone.PUSH_FILE_NAME)
subscription = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}

with patch.object(phone.PhoneServer, "_push_path", staticmethod(lambda: kept)), \
     patch.dict(os.environ, {phone.VAPID_PRIVATE_ENV: "private", phone.VAPID_SUBJECT_ENV: "mailto:me@example.com"}), \
     patch.object(phone, "webpush", side_effect=fake_push), \
     patch.object(phone, "threading", types.SimpleNamespace(Thread=Now, Lock=phone.threading.Lock)), \
     patch.object(phone.PhoneServer, "_push_allowed", staticmethod(lambda: False)), \
     patch.object(phone.cameras, "beside", return_value={"name": "room cam", "title": "ROOM CAM"}):

    server = phone.PhoneServer(token="phone-token")
    client = server._app.test_client()
    check(client.post("/push/subscribe?t=phone-token", json={"subscription": subscription}).status_code == 200,
          "the phone asks for notifications")
    check(json.load(open(kept, encoding="utf-8")) == [subscription], "and is kept in the JARVIS folder")

    later = phone.PhoneServer(token="phone-token")
    later._load_push()
    check(later._push_subscriptions == [subscription], "so a restart still knows the phone")

    server.announce("Bitcoin is up 2 percent, sir.")
    check(sent == [], "in quiet hours an ordinary notice does not buzz the phone")

    server.alarm("Movement in your room, sir, and your phone isn't home.", "room")
    pushed = sent[-1] if sent else {}
    body = json.loads(pushed.get("data") or "{}")
    check(body == {"title": "JARVIS", "body": "Movement in your room, sir, and your phone isn't home.",
                   "alarm": True, "camera": {"name": "room cam", "title": "ROOM CAM"}},
          f"an alarm does, with the camera to open ({body})")
    check(pushed.get("headers") == {"Urgency": "high"} and pushed.get("ttl") == phone.ALARM_TTL_SECONDS,
          "marked urgent, and kept for a phone out of signal")

    with patch.dict(os.environ, {phone.ALARM_TTL_ENV: "120"}):
        check(server._alarm_ttl() == 120, "how long it is kept can be set")

    notices = client.get("/notices?t=phone-token&since=0").get_json()["notices"]
    check(notices[-1].get("camera") == {"name": "room cam", "title": "ROOM CAM"} and "camera" not in notices[0],
          "the page collects it with its camera, and other notices without one")

    gone = phone.WebPushException("gone")
    gone.response = types.SimpleNamespace(status_code=410)

    with patch.object(phone, "webpush", side_effect=gone):
        server.alarm("Movement in your room, sir, and your phone isn't home.", "room")

    check(server._push_subscriptions == [] and json.load(open(kept, encoding="utf-8")) == [],
          "a phone that has gone is forgotten, on disk too")

    client.post("/push/subscribe?t=phone-token", json={"subscription": subscription})
    client.post("/push/unsubscribe?t=phone-token", json={"endpoint": subscription["endpoint"]})
    check(json.load(open(kept, encoding="utf-8")) == [], "and so is one that says so")

worker = phone.SERVICE_WORKER
check("requireInteraction" in worker and '"jarvis-alarm"' in worker and "&watch=" in worker
      and "postMessage({ watch: camera })" in worker,
      "tapping the alarm opens its camera, in the open page or a new one")
page = phone.PAGE
check('asked.get("watch")' in page and 'serviceWorker.addEventListener("message"' in page
      and "notice.camera" in page and "renewPush" in page,
      "the page plays it however it is asked, and tells JARVIS of its notifications on every visit")

from actions import folder_organizer  # noqa: E402

check(phone.PUSH_FILE_NAME in folder_organizer._PROTECTED_NAMES, "the kept phones are never sorted into a folder")


# ---- main.py: to the phone once, and said in the room -------------------------------------------

def _load_main():
    """Import main.py with its heavy neighbours replaced, as tools/test_announcement_hud.py does."""
    for name in (
        "sounddevice", "edge_tts", "soundfile", "pyttsx3",
        "commands", "news_panel", "camera_panel", "chart_panel",
        "image_panel", "image_choices_panel", "brain_panel", "beam",
        "transcriber", "agent", "outlook",
    ):
        sys.modules.setdefault(name, MagicMock())

    voice = types.ModuleType("voice")

    for name in (
        "arm_follow_up", "consume_follow_up_answer", "disarm", "last_was_named", "listen",
        "set_follow_up_expired_listener", "set_level_listener",
        "set_status_listener", "set_wake_listener", "user_talking", "FILLERS", "set_phone_active", "is_muted", "set_muted",
    ):
        setattr(voice, name, MagicMock())

    sys.modules["voice"] = voice

    import main

    return main


class Hud:
    def __init__(self):
        for name in ("heard_changed", "shutdown", "boot_requested", "boot_updated", "state_changed",
                     "reply_changed"):
            setattr(self, name, MagicMock())


main = _load_main()
assistant = main.Assistant(Hud())
said = []
phone_is["value"] = "away"

with patch.object(main, "speak", side_effect=said.append), patch.object(main, "phone_server", MagicMock()) as at_phone:
    sensors.reset()
    sensors.report({"name": "room", "event": "online"})
    assistant._on_sensor({"name": "room", "event": "motion"})

    check(at_phone.alarm.call_args_list == [(("Movement in your room, sir, and your phone isn't home.", "room"),)],
          "main sends the alarm to the phone itself")
    check(said == ["Movement in your room, sir, and your phone isn't home."], "says it in the room")
    check(not at_phone.announce.called, "and does not send it to the phone twice")

    # The same while you are mid-conversation at the desk: the phone hears now, the room after.
    at_phone.reset_mock()
    said.clear()
    assistant._interaction_open = True
    sensors.reset()
    sensors.report({"name": "room", "event": "online"})
    assistant._on_sensor({"name": "room", "event": "motion"})
    check(at_phone.alarm.called and said == [], "a conversation does not hold the alarm back from the phone")
    assistant._end_interaction()
    check(said == ["Movement in your room, sir, and your phone isn't home."] and not at_phone.announce.called,
          "and once it ends it is said in the room, without a second push")

    at_phone.reset_mock()
    said.clear()
    assistant._on_alert("Bitcoin is up 2 percent, sir.")
    check(at_phone.announce.call_args_list == [(("Bitcoin is up 2 percent, sir.",),)],
          "an ordinary announcement still goes to the phone as before")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
