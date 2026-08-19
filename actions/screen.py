import os
from datetime import datetime

try:
    from PIL import ImageGrab

    _PIL = True
except ImportError:
    _PIL = False


# Screenshots land in the user's Pictures folder, under a JARVIS subfolder.
_FOLDER_NAME = "JARVIS"


def _target_folder():
    pictures = os.path.join(os.path.expanduser("~"), "Pictures")

    if not os.path.isdir(pictures):
        pictures = os.path.expanduser("~")

    folder = os.path.join(pictures, _FOLDER_NAME)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not create {folder}: {error}")
        return None

    return folder


def _filename(now=None):
    now = now or datetime.now()

    return now.strftime("jarvis-%Y-%m-%d-%H%M%S.png")


def capture():
    """Save a screenshot of all displays. Returns the path, or None."""
    if not _PIL:
        print("[JARVIS] Pillow is not installed; screenshots unavailable")
        return None

    folder = _target_folder()

    if not folder:
        return None

    path = os.path.join(folder, _filename())

    try:
        # all_screens captures every monitor, not just the primary one.
        image = ImageGrab.grab(all_screens=True)
        image.save(path)

    except Exception as error:
        print(f"[JARVIS] screenshot failed: {error}")
        return None

    print(f"[JARVIS] screenshot saved to {path}")

    return path


def describe_capture():
    """Take a screenshot and report the outcome in a spoken sentence."""
    path = capture()

    if not path:
        return None

    return "Screenshot saved to your Pictures folder, sir."
