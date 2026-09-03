"""
See and act on whatever window is currently focused.

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
    "Button",
    "MenuItem",
    "Hyperlink",
    "TabItem",
    "CheckBox",
    "RadioButton",
    "ListItem",
    "TreeItem",
    "SplitButton",
    "ComboBox",
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
    "delete",
    "remove",
    "uninstall",
    "format",
    "erase",
    "wipe",
    "send",
    "submit",
    "pay",
    "purchase",
    "buy",
    "checkout",
    "order",
    "confirm",
    "discard",
    "empty trash",
    "sign out",
    "log out",
    "shut down",
    "restart",
    "reset",
    "unsubscribe",
    "cancel subscription",
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


def _normalise_text(text):
    """
    Normalise speech and UIA labels into comparable words.

    Important:
    - '&' and 'and' become equivalent.
    - punctuation is removed.
    - repeated whitespace disappears.
    - case is ignored.
    """
    if not text:
        return ""

    text = str(text).casefold()

    # Spoken "and" should match UI labels using "&".
    text = text.replace("&", " and ")

    # Treat common separators as spaces.
    text = re.sub(r"[/\\|_\-]+", " ", text)

    # Remove remaining punctuation.
    text = re.sub(r"[^\w\s]", " ", text)

    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _words(text):
    """Return normalised word tokens."""
    normalised = _normalise_text(text)
    return normalised.split() if normalised else []


def _contains_whole(haystack, needle):
    """
    True when needle occurs as complete words/phrase.

    This deliberately does NOT use plain substring matching.

    Therefore:
        'x' does not match 'fixtures'
        'score' does not accidentally match 'scores'
    """
    haystack_words = _words(haystack)
    needle_words = _words(needle)

    if not haystack_words or not needle_words:
        return False

    needle_len = len(needle_words)

    if needle_len > len(haystack_words):
        return False

    for index in range(len(haystack_words) - needle_len + 1):
        if haystack_words[index:index + needle_len] == needle_words:
            return True

    return False


def _token_overlap(target_words, candidate_words):
    """
    Measure how much of the spoken request is represented by the candidate.

    Uses whole tokens only. This prevents:
        score -> scores
        x -> fixtures

    from being treated as valid word matches.
    """
    if not target_words or not candidate_words:
        return 0.0

    target_set = set(target_words)
    candidate_set = set(candidate_words)

    return len(target_set & candidate_set) / len(target_set)


def _sequence_score(target, candidate):
    """
    Character-level similarity.

    Useful as a secondary signal, never as the sole reason to click.
    """
    return SequenceMatcher(None, target, candidate).ratio()


def _score_match(wanted, name):
    """
    Return a confidence score for one UIA control.

    Higher is better.

    The scoring deliberately favours:
      1. exact matches
      2. exact phrase matches
      3. complete token coverage
      4. strong token overlap
      5. fuzzy similarity

    A short partial match cannot beat a substantially better full match.
    """
    target = _normalise_text(wanted)
    candidate = _normalise_text(name)

    if not target or not candidate:
        return 0.0

    target_words = target.split()
    candidate_words = candidate.split()

    # ------------------------------------------------------------
    # 1. Exact normalised match — unbeatable.
    # ------------------------------------------------------------
    if target == candidate:
        return 1.0

    # ------------------------------------------------------------
    # 2. Exact phrase containment.
    #
    # Example:
    #   "log" -> "log in"
    #
    # This is useful, but deliberately below exact matching.
    # ------------------------------------------------------------
    target_in_candidate = _contains_whole(candidate, target)
    candidate_in_target = _contains_whole(target, candidate)

    target_len = len(target_words)
    candidate_len = len(candidate_words)

    if target_in_candidate:
        coverage = target_len / candidate_len

        # Full phrase contained in a longer control.
        #
        # "scores and fixtures"
        #     ->
        # "scores and fixtures tab"
        #
        # scores strongly, but doesn't automatically beat a closer
        # exact candidate.
        return 0.88 + (0.08 * coverage)

    if candidate_in_target:
        coverage = candidate_len / target_len

        # The candidate is a shorter, complete phrase inside the request.
        #
        # This is intentionally weaker than target_in_candidate because
        # clicking a short control from a longer request is more ambiguous.
        return 0.78 + (0.07 * coverage)

    # ------------------------------------------------------------
    # 3. Token overlap.
    # ------------------------------------------------------------
    overlap = _token_overlap(target_words, candidate_words)

    if target_len > 1 and overlap == 0:
        token_score = 0.0
    else:
        token_score = overlap

    # ------------------------------------------------------------
    # 4. Character similarity.
    # ------------------------------------------------------------
    sequence = _sequence_score(target, candidate)

    # ------------------------------------------------------------
    # 5. Word-count / length similarity.
    # ------------------------------------------------------------
    length_ratio = min(target_len, candidate_len) / max(
        target_len,
        candidate_len,
    )

    # ------------------------------------------------------------
    # Combined score.
    #
    # Token overlap is deliberately weighted more heavily than raw
    # character similarity.
    # ------------------------------------------------------------
    score = (
        token_score * 0.55
        + sequence * 0.30
        + length_ratio * 0.15
    )

    # ------------------------------------------------------------
    # Multi-word requests need stronger evidence.
    #
    # "scores and fixtures" should not match "scores" merely because
    # one token is shared.
    # ------------------------------------------------------------
    if target_len >= 2:
        if overlap >= 1.0:
            score += 0.15
        elif overlap >= 0.5:
            score -= 0.08
        else:
            score -= 0.20

    return min(score, 0.99)


def _best_match(wanted, elements):
    """
    Return the safest/highest-confidence UIA match.

    Returns:
        (name, element)

    or:
        None

    The matcher deliberately refuses ambiguous matches.
    """
    target = _normalise_text(wanted)

    if not target or not elements:
        return None

    scored = []

    # Deduplicate identical UI labels. Chromium can expose the same
    # accessible control through several UIA nodes.
    seen = set()

    for name, element in elements:
        normalised_name = _normalise_text(name)

        if not normalised_name:
            continue

        key = normalised_name

        if key in seen:
            continue

        seen.add(key)

        score = _score_match(target, normalised_name)

        scored.append((score, name, element))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)

    best_score, best_name, best_element = scored[0]

    # ------------------------------------------------------------
    # Absolute exact-match safety.
    # ------------------------------------------------------------
    if _normalise_text(best_name) == target:
        return best_name, best_element

    # ------------------------------------------------------------
    # Minimum confidence.
    # ------------------------------------------------------------
    if best_score < _MATCH_THRESHOLD:
        return None

    # ------------------------------------------------------------
    # Ambiguity protection.
    #
    # If two controls are extremely close, don't randomly click one.
    # ------------------------------------------------------------
    if len(scored) > 1:
        second_score = scored[1][0]
        margin = best_score - second_score

        if margin < 0.06:
            print(
                "[JARVIS] ambiguous screen match: "
                f"{best_name!r} ({best_score:.3f}) vs "
                f"{scored[1][1]!r} ({second_score:.3f})"
            )
            return None

    return best_name, best_element


def is_risky(name):
    """True when a control's name suggests clicking it needs asking first."""
    lowered = name.casefold()
    return any(word in lowered for word in _RISKY_WORDS)


def find_clickable(wanted):
    """
    Locate something to click by name.

    Returns (element, label, risky). element is None if nothing on the
    active window matched closely enough, even after a retry.
    """
    if not _AVAILABLE:
        return None, None, False

    wanted = (wanted or "").strip()

    if not wanted:
        return None, None, False

    # First attempt.
    window = _foreground_window()

    if not window:
        return None, None, False

    elements = _clickable_elements(window)
    match = _best_match(wanted, elements)

    if match:
        label, element = match
        return element, label, is_risky(label)

    # Chromium/Electron accessibility-tree warm-up.
    # Reacquire the foreground window rather than reusing the old wrapper.
    time.sleep(0.20)

    window = _foreground_window()

    if not window:
        return None, None, False

    elements = _clickable_elements(window)
    match = _best_match(wanted, elements)

    if not match:
        return None, None, False

    label, element = match

    return element, label, is_risky(label)


def click(element):
    """
    Click a previously located UIA element.

    Uses click_input() because it works reliably across native,
    Qt, Chromium and Electron interfaces.
    """
    if element is None:
        return False

    try:
        # Make sure the control still exists.
        if hasattr(element, "exists") and not element.exists(timeout=0.2):
            print("[JARVIS] target disappeared before click")
            return False

        # Make sure it isn't disabled.
        try:
            if hasattr(element, "is_enabled") and not element.is_enabled():
                print("[JARVIS] target is disabled")
                return False
        except Exception:
            pass

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
    """
    Type by pasting, so no character is ever interpreted as a key
    combination.

    This is the reliable path: send_keys has to parse
    +^%~(){} as modifiers/grouping even when each is individually
    escaped, and back-to-back send_keys calls can still catch a
    modifier mid-release from the previous call.

    A paste event carries the string as one block with no key parsing.
    """
    previous = None
    had_previous = False

    try:
        previous = _get_clipboard_text()
        had_previous = True

    except Exception:
        pass

    try:
        _set_clipboard_text(text)

        send_keys("^v", pause=0.05)

        # Let the paste land before we touch the clipboard again.
        time.sleep(0.05)

        return True

    finally:
        if had_previous and previous is not None:
            try:
                _set_clipboard_text(previous)

            except Exception as error:
                print(f"[JARVIS] could not restore clipboard: {error}")


def _type_via_send_keys(text):
    """
    Fallback for when the clipboard isn't reachable.

    Escapes every character send_keys treats as special
    (+^%~(){}) so it types literally rather than firing as a modifier
    or grouping.
    """
    escaped = "".join(
        f"{{{char}}}" if char in _SEND_KEYS_SPECIAL else char
        for char in text
    )

    send_keys(
        escaped,
        with_spaces=True,
        pause=0.01,
    )

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


def replace_focused_text(text):
    """Replace the focused code document without saving it."""
    if not _AVAILABLE or send_keys is None:
        print("[JARVIS] cannot replace text: pywinauto is not available")
        return False

    if not text:
        return False

    _, title, unsaved = active_document_state()

    if not title:
        print("[JARVIS] could not identify the focused document")
        return False

    code_extensions = (
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".cs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".html",
        ".css",
        ".sql",
        ".sh",
        ".ps1",
    )

    if not any(
        extension in title.casefold()
        for extension in code_extensions
    ):
        print("[JARVIS] focused window does not appear to be a code document")
        return False

    if unsaved:
        print(
            "[JARVIS] code document has unsaved changes and its text "
            "could not be verified"
        )
        return False

    try:
        with _TYPE_LOCK:
            send_keys("^a", pause=0.05)
            return _type_via_paste(text)

    except Exception as error:
        print(f"[JARVIS] could not replace focused code: {error}")
        return False


# Controls a person actually types into. "Text" is deliberately absent:
# it is the type used for labels, and including it swept up Notepad's status
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


def active_document_state():
    """Return focused document text, title, and whether it has unsaved changes."""
    window = _foreground_window()

    if not window:
        return "", None, False

    try:
        raw_title = window.window_text().strip()
    except Exception:
        raw_title = ""

    unsaved = raw_title.startswith("*")

    text, title = read_text()

    return text, title or raw_title.lstrip("*").strip(), unsaved


def read_text():
    """
    The text of whatever is being written in the active window.

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
    """
    Spoken summary of the active window, built from its own UI text.

    No screenshot, no vision call — just names UIA already knows.
    """
    window = _foreground_window()

    if not window:
        return "I can't reach the active window, sir."

    try:
        raw_title = window.window_text().strip() or "an untitled window"

    except Exception:
        raw_title = "an untitled window"

    # Windows prefixes a title with "*" to mean unsaved changes
    # (e.g. "*documents.txt - Notepad"). Left in, that symbol gets read
    # aloud literally as "asterisk" — strip it and say what it means instead.
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
        return (
            f"You're looking at {title}, sir. "
            "Nothing on it looks clickable."
        )

    if len(names) == 1:
        listed = names[0]

    else:
        listed = ", ".join(names[:-1]) + f", and {names[-1]}"

    return f"You're looking at {title}, sir. I can see {listed}."
