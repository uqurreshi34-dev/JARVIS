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
    """Screenshots live in a JARVIS folder in the user's home directory."""
    folder = os.path.join(os.path.expanduser("~"), _FOLDER_NAME)

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

    return "Screenshot saved to your JARVIS folder, sir."


# Visual screen inspection is layered on top of this existing screenshot
# capability. Normal screen-control matching remains local/UIA; the visual
# layer only takes over when rendered content is worth sending to the vision
# model, such as a browser or code editor.
try:
    from actions import screen_control, screen_vision

    screen_vision.install(screen_control)
except Exception as error:
    print(f"[JARVIS] screen vision integration unavailable: {error}")
