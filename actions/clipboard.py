import time

import win32clipboard
import win32con


# Reading aloud more than this is tedious, so longer text is summarised.
_SPEAK_LIMIT = 240

# The clipboard is a shared resource and another app may hold it briefly.
_ATTEMPTS = 5
_RETRY_DELAY = 0.05


def _open():
    """Open the clipboard, retrying while another app holds it."""
    last_error = None

    for _ in range(_ATTEMPTS):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception as error:
            last_error = error
            time.sleep(_RETRY_DELAY)

    print(f"[JARVIS] could not open the clipboard: {last_error}")

    return False


def _close():
    try:
        win32clipboard.CloseClipboard()
    except Exception:
        pass


def read():
    """Return clipboard contents as (kind, value), or ("empty", None).

    kind is one of "text", "files", "image", or "empty".
    """
    if not _open():
        return "error", None

    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return "text", win32clipboard.GetClipboardData(
                win32con.CF_UNICODETEXT
            )

        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            return "files", win32clipboard.GetClipboardData(win32con.CF_HDROP)

        for image_format in (win32con.CF_DIB, win32con.CF_BITMAP):
            if win32clipboard.IsClipboardFormatAvailable(image_format):
                return "image", None

        return "empty", None

    except Exception as error:
        print(f"[JARVIS] could not read the clipboard: {error}")
        return "error", None

    finally:
        _close()


def write(text):
    """Put text on the clipboard. Returns True on success."""
    if text is None:
        return False

    if not _open():
        return False

    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, str(text))
        return True

    except Exception as error:
        print(f"[JARVIS] could not write to the clipboard: {error}")
        return False

    finally:
        _close()


def clear():
    """Empty the clipboard. Returns True on success."""
    if not _open():
        return False

    try:
        win32clipboard.EmptyClipboard()
        return True

    except Exception as error:
        print(f"[JARVIS] could not clear the clipboard: {error}")
        return False

    finally:
        _close()


def _tidy(text):
    """Collapse whitespace so multi-line content reads naturally aloud."""
    return " ".join(str(text).split())


def format_text(text):
    """Spoken description of text on the clipboard."""
    tidied = _tidy(text)

    if not tidied:
        return "Your clipboard holds only whitespace, sir."

    if len(tidied) <= _SPEAK_LIMIT:
        return f"Your clipboard says: {tidied}"

    words = len(tidied.split())
    opening = tidied[:_SPEAK_LIMIT].rsplit(" ", 1)[0] or tidied[:_SPEAK_LIMIT]

    # A single enormous token is better described by length than word count.
    if words > 1:
        size = f"{words} words"
    else:
        size = f"{len(tidied)} characters"

    return f"Your clipboard holds {size}, sir. It begins: {opening}..."


def _basename(path):
    """Last path component, tolerant of either separator."""
    cleaned = str(path).rstrip("\\/")

    for separator in ("\\", "/"):
        cleaned = cleaned.rsplit(separator, 1)[-1]

    return cleaned


def format_files(paths):
    """Spoken description of files on the clipboard."""
    names = [_basename(path) for path in paths]
    names = [name for name in names if name]

    if not names:
        return "Your clipboard holds files, sir, but I couldn't read them."

    if len(names) == 1:
        return f"Your clipboard holds one file, sir: {names[0]}."

    shown = names[:5]
    listed = ", ".join(shown[:-1]) + f", and {shown[-1]}"

    if len(names) > len(shown):
        return (
            f"Your clipboard holds {len(names)} files, sir, including "
            f"{listed}."
        )

    return f"Your clipboard holds {len(names)} files, sir: {listed}."


def describe():
    """Spoken description of whatever is on the clipboard."""
    kind, value = read()

    if kind == "text":
        return format_text(value)

    if kind == "files":
        return format_files(value)

    if kind == "image":
        return "Your clipboard holds an image, sir."

    if kind == "empty":
        return "Your clipboard is empty, sir."

    return None
