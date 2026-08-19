import win32api
import win32con

try:
    import win32com.client as win32com_client
except ImportError:
    win32com_client = None


# Virtual key codes for the standard media and volume keys.
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_VK_MEDIA_NEXT = 0xB0
_VK_MEDIA_PREVIOUS = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3

# Each volume keypress moves the system volume by roughly two percent.
_VOLUME_STEPS = 5


def _tap(key, times=1):
    """Press and release a virtual key."""
    try:
        for _ in range(times):
            win32api.keybd_event(key, 0, 0, 0)
            win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)

    except Exception as error:
        print(f"[JARVIS] key press failed: {error}")
        return False

    return True


def volume_up(steps=_VOLUME_STEPS):
    return _tap(_VK_VOLUME_UP, steps)


def volume_down(steps=_VOLUME_STEPS):
    return _tap(_VK_VOLUME_DOWN, steps)


def toggle_mute():
    return _tap(_VK_VOLUME_MUTE)


def play_pause():
    return _tap(_VK_MEDIA_PLAY_PAUSE)


def next_track():
    return _tap(_VK_MEDIA_NEXT)


def previous_track():
    return _tap(_VK_MEDIA_PREVIOUS)


def _shell():
    if not win32com_client:
        return None

    try:
        return win32com_client.Dispatch("Shell.Application")
    except Exception as error:
        print(f"[JARVIS] could not reach the Windows shell: {error}")
        return None


def minimise_all():
    """Minimise every window, revealing the desktop."""
    shell = _shell()

    if not shell:
        return False

    try:
        shell.MinimizeAll()
    except Exception as error:
        print(f"[JARVIS] minimise failed: {error}")
        return False

    return True


def restore_all():
    """Undo the last minimise-all."""
    shell = _shell()

    if not shell:
        return False

    try:
        shell.UndoMinimizeALL()
    except Exception as error:
        print(f"[JARVIS] restore failed: {error}")
        return False

    return True
