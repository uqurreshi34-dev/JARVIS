"""Which window JARVIS looks at, and how it captures it (actions/screen_vision.py).

Windows itself is played by a small stand-in, so this runs anywhere.
Checked:

- the foreground window is the one described;
- but never JARVIS's own: with the HUD in front (it takes the foreground
  whenever it is touched), the application beneath it is described, past
  tooltips, untitled helpers, minimised, hidden and shell windows;
- a foreground window is taken as it is, even small or untitled, unless it
  is JARVIS's own or the taskbar's;
- the box is the window as seen, not with the invisible border that put a
  strip of the taskbar into every capture;
- capture asks the window to draw itself; a black result falls back to
  copying the screen, and a capture black both ways is not described at all;
- a click goes only to the window he is looking at, and only when nothing
  lies over the point.

    python tools/test_screen_vision.py
"""

import os
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- a pretend Windows -------------------------------------------------------------------------

OWN = os.getpid()


class Window:
    def __init__(self, title, cls="App", pid=1000, rect=(0, 0, 1920, 1040), visible=True, minimised=False,
                 tool=False, cloaked=False, frame=None):
        self.title, self.cls, self.pid, self.rect = title, cls, pid, rect
        self.visible, self.minimised, self.tool, self.cloaked = visible, minimised, tool, cloaked
        self.frame = frame or rect


# Top of the stack first, as Windows orders them.
HUD = Window("JARVIS", "Qt6QWindowToolSaveBits", OWN, (1500, 0, 1920, 300))
PANEL = Window("Sensors", "Qt6QWindowIcon", OWN, (1400, 300, 1920, 700))
TOOLTIP = Window("Buy", "tooltips_class32", 1000, (300, 300, 500, 340), tool=True)
HELPER = Window("", "Chrome_WidgetWin_2", 1000, (0, 0, 1920, 1040))
HIDDEN = Window("Settings", "ApplicationFrameWindow", 2000, cloaked=True)
MINIMISED = Window("Notepad", "Notepad", 3000, minimised=True)
TASKBAR = Window("", "Shell_TrayWnd", 4000, (0, 1040, 1920, 1080))
CHROME = Window("Access trading platform - Google Chrome", "Chrome_WidgetWin_1", 1000,
                (-8, -8, 1928, 1048), frame=(0, 0, 1920, 1040))
DESKTOP = Window("Program Manager", "Progman", 4000, (0, 0, 1920, 1080))

stack = [HUD, PANEL, TOOLTIP, HELPER, HIDDEN, MINIMISED, TASKBAR, CHROME, DESKTOP]
foreground = {"window": HUD}
under_point = {"window": CHROME}


def handle(window):
    return stack.index(window) + 1 if window in stack else 0


def window(hwnd):
    return stack[hwnd - 1]


fake = types.ModuleType("win32gui")
fake.GetForegroundWindow = lambda: handle(foreground["window"])
fake.GetWindow = lambda hwnd, _which: hwnd + 1 if hwnd < len(stack) else 0
fake.IsWindowVisible = lambda hwnd: window(hwnd).visible
fake.IsIconic = lambda hwnd: window(hwnd).minimised
fake.GetWindowLong = lambda hwnd, _index: 0x80 if window(hwnd).tool else 0
fake.GetClassName = lambda hwnd: window(hwnd).cls
fake.GetWindowText = lambda hwnd: window(hwnd).title
fake.GetWindowRect = lambda hwnd: window(hwnd).rect
fake.WindowFromPoint = lambda point: handle(under_point["window"])
fake.GetAncestor = lambda hwnd, _flag: hwnd
sys.modules["win32gui"] = fake

from actions import screen_vision  # noqa: E402

# Set on the module too, in case it was imported before the stand-in was.
screen_vision.win32gui = fake
screen_vision._window_process = lambda hwnd: window(hwnd).pid
screen_vision._cloaked = lambda hwnd: window(hwnd).cloaked
screen_vision._frame = lambda hwnd: window(hwnd).frame


def target():
    hwnd, box, title, _cls = screen_vision._target_window()
    return (window(hwnd) if hwnd else None), box, title


# ---- which window ------------------------------------------------------------------------------

foreground["window"] = CHROME
check(target()[0] is CHROME, "the foreground window is the one described")

foreground["window"] = HUD
seen, box, title = target()
check(seen is CHROME and "trading platform" in title,
      f"with the HUD in front, the application beneath it, past everything that is not one ({title!r})")
check(box == (0, 0, 1920, 1040), f"the box is the window as seen, without its invisible border ({box})")

foreground["window"] = PANEL
check(target()[0] is CHROME, "and the same from any of JARVIS's panels")

foreground["window"] = HELPER
check(target()[0] is HELPER, "an untitled window in front is still taken as it is")

foreground["window"] = TASKBAR
check(target()[0] is CHROME, "the taskbar in front: the window beneath it")

foreground["window"] = HUD
check(screen_vision.target_window() == handle(CHROME), "target_window gives the handle, for UI Automation too")

source = (ROOT / "actions" / "screen_control.py").read_text(encoding="utf-8")
body = source[source.index("def _foreground_window"):source.index("def _clickable_elements")]
check("screen_vision.target_window()" in body and "GetForegroundWindow" not in body,
      "screen_control reads and clicks the same window, never the HUD")

# ---- capturing it --------------------------------------------------------------------------------

drawn = Image.new("RGB", (1920, 1040), (16, 26, 46))
black = Image.new("RGB", (1920, 1040), (0, 0, 0))
grabbed = []


def grab(bbox=None, all_screens=False):
    grabbed.append(bbox)
    return screen_grab["image"]


screen_vision.ImageGrab = types.SimpleNamespace(grab=grab)
screen_grab = {"image": drawn}

screen_vision._window_image = lambda hwnd, box: drawn
jpeg = screen_vision.capture_active_window()
check(jpeg and not grabbed, "the window draws itself: nothing copied off the screen, so nothing over it")

screen_vision._window_image = lambda hwnd, box: black
jpeg = screen_vision.capture_active_window()
check(jpeg and grabbed == [(0, 0, 1920, 1040)], "drawn black, the screen is copied instead")

screen_grab["image"] = black
grabbed.clear()
check(screen_vision.capture_active_window() is None, "black both ways: not described as a black screen")

terminal = Image.new("RGB", (800, 600), (0, 0, 0))
terminal.putpixel((10, 10), (200, 200, 200))
check(not screen_vision._blank(terminal), "a dark window with a little text is not blank")

# ---- clicking it ---------------------------------------------------------------------------------

clicked = []
sys.modules["pywinauto"] = types.SimpleNamespace(mouse=types.SimpleNamespace(click=lambda coords: clicked.append(coords)))

foreground["window"] = HUD
under_point["window"] = CHROME
target_click = screen_vision._VisionClickTarget(400, 500, "Buy", handle(CHROME))
check(target_click.exists(), "the window beneath the HUD is still the target")
target_click.click_input()
check(clicked == [(400, 500)], "and a click on it goes through")

under_point["window"] = HUD

try:
    target_click.click_input()
    refused = False
except RuntimeError:
    refused = True

check(refused and clicked == [(400, 500)], "but not when the HUD lies over the point")

foreground["window"] = MINIMISED
MINIMISED.minimised = False
check(not target_click.exists(), "nor once he has moved to another window")

sys.exit(1 if failures else 0)
