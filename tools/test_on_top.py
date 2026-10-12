"""JARVIS's always-on-top windows are put back on top when Windows takes it away (on_top.py).

    python tools/test_on_top.py

On Windows a real window is made, marked on top, has that taken away as
Windows took it from the sensor panel (12 October 2026), and is checked
to be put back, once, without being activated. Elsewhere, that nothing is
done and nothing fails. Either way, that the sensor panel and the HUD
keep themselves on top.
"""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import on_top  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


class Window:
    """What keep() asks of a Qt window."""

    def __init__(self, hwnd, visible=True):
        self.hwnd, self.visible = hwnd, visible

    def isVisible(self):
        return self.visible

    def winId(self):
        return self.hwnd


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    user32.DestroyWindow.argtypes = (wintypes.HWND,)

    WS_POPUP, WS_EX_TOOLWINDOW, HWND_NOTOPMOST = 0x80000000, 0x00000080, -2
    hwnd = user32.CreateWindowExW(on_top.WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "STATIC", "jarvis-on-top-test",
                                  WS_POPUP, 0, 0, 10, 10, None, None, None, None)

    if not hwnd:
        print(f"SKIP no window could be made here (error {ctypes.get_last_error()})")
    else:
        try:
            check(on_top.on_top(hwnd) is True, "a window made always-on-top is on top")
            check(on_top.restore(hwnd) is False, "and is left alone while it is")

            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            check(on_top.on_top(hwnd) is False, "Windows can take it away")

            window = Window(hwnd)
            check(on_top.keep(window, "test window") is True and on_top.on_top(hwnd) is True,
                  "keep() puts it back on top")
            check(on_top.keep(window, "test window") is False, "and does nothing once it is back")
            check(on_top.keep(Window(hwnd, visible=False), "test window") is False, "a hidden window is left alone")
        finally:
            user32.DestroyWindow(hwnd)
else:
    check(on_top.on_top(123) is None and on_top.restore(123) is False
          and on_top.keep(Window(123), "test window") is False,
          "off Windows nothing is done, and nothing fails")

import sensor_panel  # noqa: E402
import hud  # noqa: E402

check('on_top.keep(self, "sensor panel")' in inspect.getsource(sensor_panel.SensorPanel._tick),
      "the sensor panel keeps itself on top as it animates")
check('on_top.keep(self, "HUD")' in inspect.getsource(hud.Hud._poll_telemetry),
      "the HUD keeps itself on top as it reads the telemetry")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
