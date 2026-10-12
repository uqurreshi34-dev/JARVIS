"""Keeping JARVIS's always-on-top windows on top, on Windows.

Qt's WindowStaysOnTopHint sets WS_EX_TOPMOST once, when the window is
made. Windows can take it away again: on 12 October 2026 the sensor panel,
docked above the HUD, went behind every open window once a picture opened
over it and was closed -- behind windows that are not on top themselves,
which only a window that has lost WS_EX_TOPMOST can be. Minimising them all
showed it still there, still updating; it had only lost its place.

So a window that should be on top is checked now and then (keep()), and
when the flag has gone it is put back, without taking the focus from
whatever you are typing in. Anywhere but Windows, nothing is done.
"""

import sys

_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

_user32 = None
_told = set()


def _api():
    """user32, with the two calls used here typed, or None off Windows."""
    global _user32

    if not _WINDOWS:
        return None

    if _user32 is None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        user32.SetWindowPos.restype = wintypes.BOOL
        _user32 = user32

    return _user32


def on_top(hwnd):
    """Whether native window [hwnd] is marked always-on-top; None off Windows or without a window."""
    user32 = _api()

    if user32 is None or not hwnd:
        return None

    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def restore(hwnd):
    """Put native window [hwnd] back on top if it has lost it. True when it had, and was put back."""
    if on_top(hwnd) is not False:
        return False

    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    return bool(_api().SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags))


def keep(widget, name):
    """Put Qt window [widget] back on top if Windows has taken it away. Cheap: one call when nothing is wrong.

    Said once per window in the console the first time it happens, so it can be seen that it did.
    """
    if not _WINDOWS or not widget.isVisible():
        return False

    try:
        put_back = restore(int(widget.winId()))
    except Exception as error:  # noqa: BLE001 - a window check must never stop a panel drawing
        if name not in _told:
            _told.add(name)
            print(f"[JARVIS] could not keep the {name} on top: {error}")
        return False

    if put_back and name not in _told:
        _told.add(name)
        print(f"[JARVIS] the {name} had dropped behind other windows; it is back on top")

    return put_back
