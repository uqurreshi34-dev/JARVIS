import threading

import win32api
import win32con

try:
    import win32com.client as win32com_client
except ImportError:
    win32com_client = None

# pycaw gives absolute volume control. Without it, only the relative
# volume keys below are available.
try:
    from ctypes import POINTER, cast

    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    _PYCAW = True
except ImportError:
    try:
        from ctypes import POINTER, cast

        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.api.endpointvolume import IAudioEndpointVolume
        from pycaw.utils import AudioUtilities

        _PYCAW = True
    except ImportError:
        _PYCAW = False

# Used to reach the default audio device directly, which works regardless of
# how the installed pycaw version wraps its device objects.
try:
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator

    _MMDEVICE = True
except ImportError:
    _MMDEVICE = False


# Virtual key codes for the standard media and volume keys.
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_VK_MEDIA_NEXT = 0xB0
_VK_MEDIA_PREVIOUS = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3

# Each volume keypress moves the system volume by roughly two percent.
_VOLUME_STEPS = 5

# COM interfaces belong to the thread that created them.
_local = threading.local()


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


def _activate(device):
    """Get IAudioEndpointVolume from a device, whatever pycaw handed us."""
    if device is None:
        return None

    # Older pycaw returns the raw COM IMMDevice.
    if hasattr(device, "Activate"):
        return device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)

    # Newer pycaw wraps it in an AudioDevice, keeping the COM object on _dev.
    inner = getattr(device, "_dev", None)

    if inner is not None and hasattr(inner, "Activate"):
        return inner.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)

    return None


def _default_device():
    """Reach the default playback device straight through COM."""
    if not _MMDEVICE:
        return None

    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator,
        IMMDeviceEnumerator,
        comtypes.CLSCTX_INPROC_SERVER,
    )

    # eRender = 0, eMultimedia = 1
    return enumerator.GetDefaultAudioEndpoint(0, 1)


def _wrap(interface):
    """Turn a raw activated interface into a usable volume object."""
    if interface is None:
        return None

    # QueryInterface keeps comtypes' reference counting happy, where a bare
    # cast can emit "COM method call without VTable" during collection.
    try:
        return interface.QueryInterface(IAudioEndpointVolume)
    except Exception:
        return cast(interface, POINTER(IAudioEndpointVolume))


def _endpoint_volume():
    """Return the system volume interface, or None if unavailable.

    COM must be initialised on the calling thread, and the interface is cached
    per thread. JARVIS runs commands on a worker thread, so neither can be
    skipped.
    """
    if not _PYCAW:
        print("[JARVIS] pycaw is not installed; absolute volume unavailable")
        return None

    cached = getattr(_local, "volume", None)

    if cached is not None:
        return cached

    try:
        comtypes.CoInitialize()
    except Exception:
        pass

    interface = None

    try:
        interface = _activate(AudioUtilities.GetSpeakers())
    except Exception as error:
        print(f"[JARVIS] pycaw device lookup failed ({error}); trying COM")

    if interface is None:
        try:
            interface = _activate(_default_device())
        except Exception as error:
            print(f"[JARVIS] could not reach the audio endpoint: {error}")
            return None

    volume = _wrap(interface)

    if volume is None:
        print("[JARVIS] could not reach the audio endpoint")
        return None

    _local.volume = volume

    return volume


def set_volume(percent):
    """Set the system volume to an absolute percentage."""
    volume = _endpoint_volume()

    if volume is None:
        return False

    level = max(0.0, min(1.0, float(percent) / 100.0))

    try:
        volume.SetMasterVolumeLevelScalar(level, None)

        # Setting a level while muted should also unmute.
        if level > 0 and volume.GetMute():
            volume.SetMute(0, None)

    except Exception as error:
        print(f"[JARVIS] failed to set volume: {error}")
        return False

    return True


def get_volume():
    """Current volume as a percentage, or None if unavailable."""
    volume = _endpoint_volume()

    if volume is None:
        return None

    try:
        if volume.GetMute():
            return 0

        return round(volume.GetMasterVolumeLevelScalar() * 100)

    except Exception as error:
        print(f"[JARVIS] failed to read volume: {error}")
        return None


def describe_volume():
    """Spoken description of the current volume."""
    volume = _endpoint_volume()

    if volume is None:
        return None

    try:
        muted = bool(volume.GetMute())
        level = round(volume.GetMasterVolumeLevelScalar() * 100)
    except Exception as error:
        print(f"[JARVIS] failed to read volume: {error}")
        return None

    if muted:
        return f"Sound is muted, sir. The level is set to {level} percent."

    return f"Volume is at {level} percent, sir."


def set_mute(muted):
    """Explicitly mute or unmute, rather than toggling."""
    volume = _endpoint_volume()

    if volume is None:
        # Fall back to the toggle key if pycaw is unavailable.
        return toggle_mute()

    try:
        volume.SetMute(1 if muted else 0, None)
    except Exception as error:
        print(f"[JARVIS] failed to change mute: {error}")
        return False

    return True


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


if __name__ == "__main__":
    print("pycaw available:      ", _PYCAW)
    print("mmdevice available:   ", _MMDEVICE)

    if _PYCAW:
        try:
            import pycaw
            print("pycaw version:        ", getattr(
                pycaw, "__version__", "unknown"))
        except Exception:
            pass

        try:
            comtypes.CoInitialize()
            speakers = AudioUtilities.GetSpeakers()
            print("GetSpeakers type:     ", type(speakers).__name__)
            print("has Activate:         ", hasattr(speakers, "Activate"))
            print("has _dev:             ", hasattr(speakers, "_dev"))

            inner = getattr(speakers, "_dev", None)
            if inner is not None:
                print("_dev type:            ", type(inner).__name__)
                print("_dev has Activate:    ", hasattr(inner, "Activate"))

            print("attributes:           ",
                  [a for a in dir(speakers) if not a.startswith("__")][:12])
        except Exception as error:
            print("GetSpeakers failed:   ", error)

    print()
    print("describe_volume():    ", describe_volume())
