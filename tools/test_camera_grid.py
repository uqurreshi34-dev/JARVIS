"""Every camera at once: "show me all the cameras" opens a tile for each, a tap makes one big, BACK returns.

    python tools/test_camera_grid.py

Without a camera: boards report themselves and pictures are made here. Checked:

- "show me all the cameras", "show my cameras", "show every camera" ask for them all; "show me the
  camera" and "open the camera" stay the PC's own webcam, and one camera by name stays one camera;
- "close all the cameras" puts them away;
- through commands: every connected camera streams at once, each frame goes to its own tile, one asked
  for by name while the grid is up is made big rather than ending the others, and closing ends them all;
  with one camera it is shown as asking for it by name would, and with none it says so;
- the panel: a tile each, a tap on one makes it fill the panel, BACK returns to them all, a drag is not a
  tap, and a single picture afterwards puts the grid away;
- the phone: the reply carries every camera for a grid, and a tile asks for fewer frames a second.
"""

import os
import sys
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

from actions import cameras, sensors  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


JPEG = b"\xff\xd8\xff\xe0" + b"picture" * 20

sensors.reset()
cameras.forget()

# ---- what is said ----------------------------------------------------------------------------------

for said in ("show me all the cameras", "show all cameras", "Jarvis, show my cameras please", "show me the cameras",
             "show every camera", "show all cams", "show me all of the camera feeds", "bring up all the cameras"):
    check(cameras.all_asked(said), f"every camera: {said!r}")

for said in ("show me the camera", "open the camera", "show me my room cam", "show me the house cam",
             "what can you see", "show me the house"):
    check(not cameras.all_asked(said), f"not every camera: {said!r}")

check(cameras.all_dismissed("close all the cameras") and cameras.all_dismissed("hide my cameras")
      and not cameras.all_dismissed("close the camera") and not cameras.all_dismissed("close my room cam"),
      "'close all the cameras' puts them away; 'close the camera' stays the webcam's")

# ---- two cameras -------------------------------------------------------------------------------------

sensors.report({"name": "my room cam", "event": "online"})
sensors.report({"name": "house cam", "event": "online"})
check(sorted(camera["name"] for camera in cameras.online()) == ["house cam", "my room cam"],
      "both cameras are online")
house = next(camera for camera in cameras.cameras() if camera["name"] == "house cam")
check(cameras.asked("show me the house cam") == ("show", house) and cameras.asked("show me the house") is None,
      "'show me the house cam' is the new camera; 'show me the house' is still the hologram's")

cameras.watch_all(["my room cam", "house cam"])
check(cameras.live_wanted("my room cam") and cameras.live_wanted("house cam"), "the grid streams both at once")
cameras.stop_watching()

# ---- through commands --------------------------------------------------------------------------------

try:
    import commands
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP commands (could not import commands: {error})")
    commands = None

if commands is not None:
    told, shown = [], []
    commands.set_camera_grid_listener(lambda event, payload: told.append((event, payload)))
    commands.set_camera_listener(lambda image, title: shown.append((image, title)))

    try:
        result = commands.handle_command("show me all the cameras")
        said = result["action"]()
        names = {tile["name"] for tile in told[0][1]} if told else set()
        check(result["intent"] == "camera_grid" and said == "Here are your two cameras, sir."
              and told and told[0][0] == "grid" and names == {"my room cam", "house cam"},
              f"commands: a tile for each camera ({said!r})")
        check(cameras.live_wanted("my room cam") and cameras.live_wanted("house cam"), "and both stream")

        commands._live_frame("house cam", JPEG)
        check(told[-1] == ("frame", ("house cam", JPEG)) and not shown, "a frame goes to its own tile")

        result = commands.handle_command("show me the house cam")
        said = result["action"]()
        check(told[-1] == ("focus", "house cam") and said == "Here's house cam, sir."
              and cameras.live_wanted("my room cam"),
              f"one asked for by name is made big, and the other keeps streaming for back ({said!r})")

        closing = commands.handle_command("close all the cameras")
        said = closing["action"]()
        check(closing["intent"] == "camera_close" and said == "Cameras closed, sir."
              and not cameras.live_wanted("my room cam") and not cameras.live_wanted("house cam")
              and shown[-1] == (b"", "") and not commands._grid,
              f"closing ends them all and puts the panel away ({said!r})")

        sensors._sensors["house cam"]["seen"] -= 600
        cameras._polled.clear()
        told.clear()
        cameras.store("my room cam", JPEG)
        real_fetch = cameras.fetch
        cameras.fetch = lambda name, timeout=None: JPEG
        said = commands.handle_command("show me all the cameras")["action"]()
        cameras.fetch = real_fetch
        check(not told and shown[-1] == (JPEG, "MY ROOM CAM") and said == "Here's your room cam, sir.",
              f"with one camera online it is shown as asking for it would ({said!r})")
        commands._stop_looking()

        sensors.reset()
        cameras.forget()
        said = commands.handle_command("show me all the cameras")["action"]()
        check(said == "No cameras are connected, sir.", f"and with none, it says so ({said!r})")
    finally:
        commands.set_camera_grid_listener(None)
        commands.set_camera_listener(None)
        commands._end_live()

# ---- the panel ---------------------------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication

    import camera_panel
except Exception as error:
    print(f"SKIP the panel (could not load Qt: {error})")
    camera_panel = None

if camera_panel is not None:
    app = QApplication.instance() or QApplication([])
    panel = camera_panel.CameraPanel()
    panel._on_grid([{"name": "my room cam", "title": "MY ROOM CAM"}, {"name": "house cam", "title": "HOUSE CAM"}])
    check(panel._grid and list(panel._tiles) == ["my room cam", "house cam"]
          and panel.width() == camera_panel._GRID_WIDTH, "a tile each, in a bigger panel")

    picture = QImage(64, 48, QImage.Format.Format_RGB32)
    picture.fill(QColor(30, 90, 140))
    from PyQt6.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, "JPG")
    frame = bytes(buffer.data())
    panel._on_grid_frame("house cam", frame)
    check(panel._tiles["house cam"]["image"] is not None and panel._tiles["my room cam"]["image"] is None,
          "a frame fills its own tile only")
    panel.grab()

    tiles = dict(panel._tile_rects())
    left, right = tiles["my room cam"], tiles["house cam"]
    check(left.right() < right.left() and left.width() > 300, "two tiles side by side, each a good size")
    panel._tapped(right.center())
    check(panel._focused == "house cam", "a tap on a tile makes that camera big")
    panel.grab()
    panel._tapped(QPointF(left.center()))
    check(panel._focused == "house cam", "while big, a tap on the picture does nothing")
    panel._tapped(panel._back_rect().center())
    check(panel._focused is None and panel._grid, "BACK returns to every camera")

    panel._on_focus("my room cam")
    check(panel._focused == "my room cam", "a camera asked for by name is made big")
    panel._on_show(frame, "STREET CAM")
    check(not panel._grid and panel.width() == camera_panel._WIDTH, "a single picture puts the grid away")
    panel._on_hide()

# ---- the phone -----------------------------------------------------------------------------------------

try:
    import phone
except Exception as error:
    print(f"SKIP the phone (could not import phone: {error})")
    phone = None

if phone is not None:
    sensors.reset()
    cameras.forget()
    sensors.report({"name": "my room cam", "event": "online"})
    sensors.report({"name": "house cam", "event": "online"})
    server = phone.PhoneServer(token="grid-test")
    reply = server._camera_or_grid("show me all the cameras")
    check(reply["camera"] is None and sorted(tile["name"] for tile in reply["cameras"]) == ["house cam", "my room cam"],
          "the phone is told every camera, for its grid")
    check(server._camera_or_grid("show me the house cam") == {"camera": {"name": "house cam", "title": "HOUSE CAM"},
                                                              "cameras": None},
          "and one camera by name, as before")
    page = phone.PAGE
    check("function openGrid" in page and 'id="feed-back"' in page and "watchUrl(camera.name, 4)" in page,
          "the page draws the grid, with BACK, at four frames a second a tile")

cameras.forget()
sensors.reset()
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
