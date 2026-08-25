"""See and act on whatever window is currently focused.

Everything here is local: Windows' own UI Automation (UIA) tree tells us
what's on screen and where it is, so nothing is sent anywhere and nothing
costs an API call. This is deliberately not vision-based — matching a
spoken word against a control's real accessible name is exact, where
matching it against a screenshot would mean guessing pixel coordinates.

Needs `pywinauto` (which pulls in pywin32 on Windows):
    pip install pywinauto

Known limits, worth knowing before relying on this:
- UIA cannot see into a window running at a higher privilege level than
  JARVIS itself (an app run "as Administrator" while JARVIS runs normally
  will not be reachable). This is a Windows security boundary, not a bug.
- Chromium-based apps (Chrome, Edge, Electron apps like VS Code or Slack)
  sometimes expose a thin accessibility tree until something actually
  queries it, so the very first look at a freshly opened one may come back
  sparse. Asking again usually fixes it.
- This is written against pywinauto's documented API but has not been
  run against a live Windows session from here — the sandbox this was
  written in is Linux. Treat the first few uses as a test, starting with
  describe() (read-only) before trying click().
"""

import re
import threading
import time
from difflib import SequenceMatcher

try:
    import win32gui
    from pywinauto import Application

    _AVAILABLE = True
except ImportError as error:
    print(f"[JARVIS] screen control unavailable: {error}")
    _AVAILABLE = False

try:
    from pywinauto.keyboard import send_keys
except ImportError:
    send_keys = None

try:
    import win32clipboard
    import win32con

    _CLIPBOARD_AVAILABLE = True
except ImportError:
    _CLIPBOARD_AVAILABLE = False

# Only one type_text() call may be sending keystrokes at a time. Without
# this, two calls fired close together (e.g. back-to-back dictated
# phrases) can interleave mid-word — one call's tail landing inside the
# next call's stream — which is what produced garbled output like
# "world" coming out as "oorld" or "rrrld".
_TYPE_LOCK = threading.Lock()


# Control types worth offering as something to click. UIA has many more
# (Text, Pane, Image...) but those aren't things a person clicks.
_CLICKABLE_TYPES = frozenset({
    "Button", "MenuItem", "Hyperlink", "TabItem", "CheckBox",
    "RadioButton", "ListItem", "TreeItem", "SplitButton", "ComboBox",
})

# A match below this score is treated as no match at all, rather than
# clicking the closest-sounding thing on screen.
_MATCH_THRESHOLD = 0.6

# Characters send_keys treats as modifiers or grouping syntax rather than
# literal text. Only used by the send_keys fallback path in type_text —
# the primary clipboard-paste path doesn't need this at all, since a
# paste event carries the string verbatim with no key-combo parsing.
_SEND_KEYS_SPECIAL = frozenset("+^%~(){}")

# Words that mean a click shouldn't happen without asking first. Matched
# against the control's own name, so "Delete Account" is caught whether the
# user said "delete", "account", or the whole phrase.
_RISKY_WORDS = (
    "delete", "remove", "uninstall", "format", "erase", "wipe",
    "send", "submit", "pay", "purchase", "buy", "checkout", "order",
    "confirm", "discard", "empty trash", "sign out", "log out",
    "shut down", "restart", "reset", "unsubscribe", "cancel subscription",
)


def available():
    return _AVAILABLE


def _foreground_window():
    """The window currently in focus, wrapped for UI Automation."""
    if not _AVAILABLE:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()

        if not hwnd:
            return None

        app = Application(backend="uia").connect(handle=hwnd)

        return app.window(handle=hwnd)

    except Exception as error:
        print(f"[JARVIS] could not reach the active window: {error}")
        return None


def _clickable_elements(window):
    """(name, element) pairs for everything in the window worth clicking."""
    elements = []

    try:
        for element in window.descendants():
            try:
                info = element.element_info
                name = (info.name or "").strip()
                control_type = info.control_type
            except Exception:
                continue

            if name and control_type in _CLICKABLE_TYPES:
                elements.append((name, element))

    except Exception as error:
        print(f"[JARVIS] could not read the screen: {error}")

    return elements


def _contains_whole(haystack, needle):
    """True when needle appears in haystack as a whole word or phrase,
    not merely as a run of characters.

    Plain "needle in haystack" containment is how a control named "X" —
    a close icon, commonly — ends up matching "scores and fixtures": the
    letter x is genuinely present, inside "fixtures", with no relation to
    what was actually meant. Anchoring on word boundaries closes that off
    while still matching "log" inside "please click log in".
    """
    if not needle:
        return False

    return bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack))


def _best_match(wanted, elements):
    """The element whose name is the closest match to wanted, or None."""
    target = wanted.strip().casefold()

    if not target or not elements:
        return None

    for name, element in elements:
        if name.casefold() == target:
            return name, element

    contained = [
        (name, element) for name, element in elements
        if _contains_whole(target, name.casefold())
        or _contains_whole(name.casefold(), target)
    ]

    if contained:
        contained.sort(key=lambda pair: len(pair[0]))
        return contained[0]

    best = None
    best_score = 0.0

    for name, element in elements:
        score = SequenceMatcher(None, target, name.casefold()).ratio()

        if score > best_score:
            best_score = score
            best = (name, element)

    return best if best_score >= _MATCH_THRESHOLD else None


def is_risky(name):
    """True when a control's name suggests clicking it needs asking first."""
    lowered = name.casefold()

    return any(word in lowered for word in _RISKY_WORDS)


def find_clickable(wanted):
    """Locate something to click by name.

    Returns (element, label, risky). element is None if nothing on the
    active window matched closely enough, even after a retry.
    """
    window = _foreground_window()

    if not window:
        return None, None, False

    match = _best_match(wanted, _clickable_elements(window))

    if not match:
        # Chromium apps can expose a thin accessibility tree until
        # something has actually queried it once (see the module
        # docstring), so a freshly loaded page's real controls can be
        # briefly invisible. One quiet retry means that alone is never
        # why "click X" fails — and means nobody has to ask "what's on
        # my screen" first purely to warm the tree up before a click
        # will work; that was never a real requirement, just this gap.
        time.sleep(0.2)
        match = _best_match(wanted, _clickable_elements(window))

    if not match:
        return None, None, False

    label, element = match

    return element, label, is_risky(label)


def click(element):
    """Click a previously located element. Returns True on success."""
    try:
        # click_input() simulates a real mouse click at the element's
        # position, which works across native, Qt, and Chromium UIs alike;
        # the element-level click() method does not.
        element.click_input()

        return True

    except Exception as error:
        print(f"[JARVIS] could not click: {error}")
        return False


def _get_clipboard_text():
    """Current clipboard text, or None if it isn't text (or is empty)."""
    win32clipboard.OpenClipboard()

    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)

        return None

    finally:
        win32clipboard.CloseClipboard()


def _set_clipboard_text(text):
    win32clipboard.OpenClipboard()

    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)

    finally:
        win32clipboard.CloseClipboard()


def _type_via_paste(text):
    """Type by pasting, so no character is ever interpreted as a key
    combination. This is the reliable path: send_keys has to parse
    +^%~(){} as modifiers/grouping even when each is individually
    escaped, and back-to-back send_keys calls can still catch a
    modifier mid-release from the previous call (this is what produced
    garbled output like "world" typing as "oorld" or "rrrld" — a stray
    Alt state from an escaped "%" bleeding into the next call). A paste
    event carries the string as one block with no key parsing at all.
    """
    previous = None
    had_previous = False

    try:
        previous = _get_clipboard_text()
        had_previous = True
    except Exception:
        pass  # nothing usable on the clipboard yet; nothing to restore

    try:
        _set_clipboard_text(text)
        send_keys("^v", pause=0.05)
        # let the paste land before we touch the clipboard again
        time.sleep(0.05)

        return True

    finally:
        if had_previous and previous is not None:
            try:
                _set_clipboard_text(previous)
            except Exception as error:
                print(f"[JARVIS] could not restore clipboard: {error}")


def _type_via_send_keys(text):
    """Fallback for when the clipboard isn't reachable. Escapes every
    character send_keys treats as special (+^%~(){}) so it types
    literally rather than firing as a modifier or grouping — done in
    one pass over the original text, since chaining separate .replace()
    calls would let an earlier substitution's output (e.g. the braces
    inserted while escaping a brace) get re-matched and mangled by a
    later one.
    """
    escaped = "".join(
        f"{{{char}}}" if char in _SEND_KEYS_SPECIAL else char
        for char in text
    )

    send_keys(escaped, with_spaces=True, pause=0.01)

    return True


def type_text(text):
    """Type into whatever currently has keyboard focus."""
    if not _AVAILABLE or send_keys is None:
        print("[JARVIS] cannot type: pywinauto is not available")
        return False

    if not text:
        return False

    with _TYPE_LOCK:
        try:
            if _CLIPBOARD_AVAILABLE:
                return _type_via_paste(text)

            return _type_via_send_keys(text)

        except Exception as error:
            print(f"[JARVIS] could not type: {error}")
            return False


# Controls a person actually types into. "Text" is deliberately absent: it
# is the type used for labels, and including it swept up Notepad's status
# bar ("Ln 1, Col 67", "UTF-8") as though it were part of the document.
_TEXT_TYPES = ("Edit", "Document")

# Reading everything on a busy screen would produce nonsense, so only this
# much is taken.
MAX_SCREEN_CHARS = 20_000


def _element_text(element):
    """Whatever text a control holds, or an empty string."""
    # A value pattern is how an edit control exposes what is typed in it;
    # the accessible name is only a label.
    try:
        value = element.get_value()

        if value and str(value).strip():
            return str(value)

    except Exception:
        pass

    try:
        text = element.window_text()

        if text and text.strip():
            return text

    except Exception:
        pass

    return ""


def _focused_among(elements):
    """The element with keyboard focus, or None."""
    for element in elements:
        try:
            if element.has_keyboard_focus():
                return element
        except Exception:
            continue

    return None


def read_text():
    """The text of whatever is being written in the active window.

    Returns (text, window title). The text is empty when nothing readable
    was found, which is common in applications that draw their own text
    rather than using a standard control.
    """
    window = _foreground_window()

    if not window:
        return "", None

    try:
        title = window.window_text().strip().lstrip("*").strip()
    except Exception:
        title = None

    collected = []
    seen = set()
    candidates = []

    try:
        for element in window.descendants():
            try:
                control_type = element.element_info.control_type
            except Exception:
                continue

            if control_type not in _TEXT_TYPES:
                continue

            candidates.append(element)

    except Exception as error:
        print(f"[JARVIS] could not read the screen text: {error}")
        return "", title

    # If something has the cursor, that is the thing being written in, and
    # nothing else in the window should be included.
    focused = _focused_among(candidates)

    if focused is not None:
        candidates = [focused]

    try:
        for element in candidates:
            text = _element_text(element)

            if not text:
                continue

            key = text.strip()[:120]

            # The same text often appears at several levels of the tree.
            if key in seen:
                continue

            seen.add(key)
            collected.append(text)

            if sum(len(part) for part in collected) > MAX_SCREEN_CHARS:
                break

    except Exception as error:
        print(f"[JARVIS] could not read the screen text: {error}")

    return "\n".join(collected).strip(), title


def describe():
    """Spoken summary of the active window, built from its own UI text.

    No screenshot, no vision call — just names UIA already knows.
    """
    window = _foreground_window()

    if not window:
        return "I can't reach the active window, sir."

    try:
        raw_title = window.window_text().strip() or "an untitled window"
    except Exception:
        raw_title = "an untitled window"

    # Windows prefixes a title with "*" to mean unsaved changes (e.g.
    # "*documents.txt - Notepad"). Left in, that symbol gets read aloud
    # literally as "asterisk" — strip it and say what it means instead.
    unsaved = raw_title.startswith("*")
    title = raw_title.lstrip("*").strip() or "an untitled window"

    if unsaved:
        title = f"{title}, with unsaved changes"

    seen = set()
    names = []

    for name, _ in _clickable_elements(window):
        key = name.casefold()

        if key not in seen:
            seen.add(key)
            names.append(name)

        if len(names) >= 8:
            break

    if not names:
        return f"You're looking at {title}, sir. Nothing on it looks clickable."

    if len(names) == 1:
        listed = names[0]
    else:
        listed = ", ".join(names[:-1]) + f", and {names[-1]}"

    return f"You're looking at {title}, sir. I can see {listed}."
