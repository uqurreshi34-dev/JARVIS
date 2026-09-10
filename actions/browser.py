"""Read the web through the Chrome you are already using.

This attaches to a running Chrome over its remote debugging port rather
than launching its own.

It attaches to a DEDICATED Chrome profile, not your everyday one, and that
is not a design preference. From Chrome 136, --remote-debugging-port is
ignored outright when it points at the default Chrome profile: Google
closed that door so malware could not attach to a real profile and read
its saved passwords and cookies. The flag only works alongside a
--user-data-dir pointing somewhere non-standard. jarvis-chrome.bat does
exactly that, at %LOCALAPPDATA%/JarvisChrome.

So the profile starts empty and you sign in to it once. Those logins then
persist, because the directory is kept between runs. Being a separate
profile, it also runs happily alongside your normal Chrome.

The safety gate comes for free: Chrome only opens that port when started
that way, so Chrome launched normally is unreachable from here.

Worth knowing plainly: while that port is open it is not JARVIS-only. Any
local process could attach to it, because Chrome does not check who is
asking. It binds to localhost, so nothing on your network can reach it,
but it is a "this browser is controllable" switch rather than a lock.
Keep genuinely sensitive accounts in your normal Chrome.

THIS STAGE IS READ ONLY. Navigate, read, and describe. Nothing here types,
clicks, submits a form, or fills a field. Those come later, once you have
watched this behave against your own logged-in pages.

Needs selenium:
    pip install selenium

Selenium itself is pure Python. It ships one executable, selenium-manager,
which it runs to find a driver. To avoid that entirely (Smart App Control
may object to it), set JARVIS_CHROMEDRIVER to the full path of a
chromedriver.exe you downloaded yourself, and it will be used directly.
"""

import ctypes
import os
import re
import socket
import threading
from urllib.parse import quote_plus, urlparse

from actions import journal, safety

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    _AVAILABLE = True
except ImportError as error:
    print(f"[JARVIS] browser control unavailable: {error}")
    _AVAILABLE = False
    WebDriverException = Exception


# Where Chrome's debugging port is expected. Overridable for the rare case
# of a different port, but the default matches the shortcut in the notes.
_DEBUG_HOST = os.environ.get("JARVIS_CHROME_DEBUG", "127.0.0.1:9222")

# A chromedriver.exe you downloaded yourself. When set, selenium-manager
# never runs, so there is no unsigned executable for Application Control
# to object to.
_DRIVER_PATH = os.environ.get("JARVIS_CHROMEDRIVER") or None

# One driver, reused. Attaching is slow enough (about a second) that doing
# it per command would be felt, and there is no reason to reconnect.
_driver = None
_lock = threading.Lock()

# How much page text to keep. Long enough for a real article, short enough
# that nothing downstream chokes on it.
_MAX_PAGE_CHARS = 20000

# How much to actually say out loud. A page read in full is unbearable, so
# the opening is spoken and the rest is available on request.
_SPOKEN_WORDS = 70

_SEARCH_URL = "https://www.google.com/search?q={query}"

# Bare words that are really sites, so "open google" reaches the site
# rather than being treated as a search for the word "google".
_KNOWN_SITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "bbc": "https://www.bbc.co.uk",
    "bbc news": "https://www.bbc.co.uk/news",
    "wikipedia": "https://www.wikipedia.org",
    "amazon": "https://www.amazon.co.uk",
    "reddit": "https://www.reddit.com",
    "stack overflow": "https://stackoverflow.com",
    "linkedin": "https://www.linkedin.com",
    "maps": "https://www.google.com/maps",
    "google maps": "https://www.google.com/maps",
}

# Looks like a domain rather than something to search for.
_DOMAIN = re.compile(
    r"^(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?$", re.IGNORECASE
)

# Pulls the readable part out of a page. Runs in the page itself, so there
# is no HTML parser to install and no second copy of the DOM to walk in
# Python. Scores containers by how much real paragraph text they hold,
# which is what separates an article from the furniture around it.
_EXTRACT_JS = r"""
const strip = 'script,style,noscript,nav,header,footer,aside,form,iframe,' +
              'button,svg,[aria-hidden="true"],[role="navigation"],' +
              '[role="banner"],[role="complementary"]';

function textOf(node) {
    if (!node) return '';
    const clone = node.cloneNode(true);
    clone.querySelectorAll(strip).forEach(el => el.remove());
    return (clone.innerText || '').replace(/\s+\n/g, '\n')
                                  .replace(/\n{3,}/g, '\n\n')
                                  .trim();
}

// A semantic container is the author telling us where the content is.
for (const selector of ['article', 'main', '[role="main"]', '#content',
                        '.post-content', '.article-body']) {
    const found = document.querySelector(selector);
    if (found) {
        const text = textOf(found);
        if (text.length > 200) return text;
    }
}

// Otherwise score every candidate by the paragraph text inside it, and
// take the richest. Shallow wrappers lose to the block that actually
// holds the writing.
let best = null, bestScore = 0;
for (const node of document.querySelectorAll('div,section,td')) {
    let score = 0;
    for (const p of node.querySelectorAll('p')) {
        const len = (p.innerText || '').trim().length;
        if (len > 40) score += len;
    }
    if (score > bestScore) { bestScore = score; best = node; }
}

if (best && bestScore > 200) return textOf(best);

return textOf(document.body);
"""

# Headings and links, for "what's on this page". Capped in the page rather
# than in Python so a huge page does not cross the wire in full.
_OVERVIEW_JS = r"""
const headings = [];
for (const node of document.querySelectorAll('h1,h2,h3')) {
    const text = (node.innerText || '').trim();
    if (text && text.length < 120) headings.push(text);
    if (headings.length >= 25) break;
}

const links = [];
const seen = new Set();
for (const node of document.querySelectorAll('a[href]')) {
    const text = (node.innerText || '').trim();
    if (!text || text.length > 80) continue;
    const key = text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({text: text, href: node.href});
    if (links.length >= 30) break;
}

return {headings: headings, links: links};
"""


def available():
    """True when selenium is installed. Says nothing about Chrome."""
    return _AVAILABLE


def _how_to_start():
    """The exact thing to do when Chrome is not reachable."""
    return (
        "Chrome isn't listening for me, sir. Start the JARVIS Chrome "
        "shortcut and ask me again. It runs alongside your normal "
        "browser, so nothing needs closing."
    )


def _port_open():
    """Is anything listening on the debugging port right now?

    Selenium takes several seconds to fail when nothing is there, and
    those seconds are spent with JARVIS visibly frozen mid-command. A
    socket check answers the same question in milliseconds, so the far
    more common "Chrome isn't running" case is instant.
    """
    host, _, port = _DEBUG_HOST.partition(":")

    try:
        with socket.create_connection((host, int(port or 9222)), timeout=0.4):
            return True
    except (OSError, ValueError):
        return False


def _connect():
    """Attach to the running Chrome, or return None.

    Never launches a browser. If Chrome is not listening on the debugging
    port, that is the answer, not a reason to start one: a browser JARVIS
    opened himself would have none of your logins in it.
    """
    global _driver

    if not _AVAILABLE:
        return None

    if _driver is None and not _port_open():
        # Nothing is listening, so there is no point spending several
        # seconds letting selenium discover that the slow way.
        return None

    if _driver is not None:
        # Cheap liveness check. Chrome may have been closed since we
        # attached, and every later call would otherwise fail obscurely.
        try:
            _driver.current_url
            return _driver
        except WebDriverException:
            print("[JARVIS] the attached Chrome has gone; reattaching")
            _driver = None

    options = Options()
    options.add_experimental_option("debuggerAddress", _DEBUG_HOST)

    try:
        if _DRIVER_PATH:
            _driver = webdriver.Chrome(
                service=Service(executable_path=_DRIVER_PATH),
                options=options,
            )
        else:
            _driver = webdriver.Chrome(options=options)

    except WebDriverException as error:
        # The common case by far is "Chrome isn't running with the flag",
        # so say that rather than printing a stack trace at the user.
        print(f"[JARVIS] could not attach to Chrome: {error}")
        _driver = None

    except Exception as error:
        print(f"[JARVIS] could not attach to Chrome: {error}")
        _driver = None

    return _driver


def attached():
    """True when a live Chrome is reachable right now."""
    with _lock:
        return _connect() is not None


def release():
    """Let go of the browser without closing it.

    Deliberately not driver.quit(): that would close the user's own Chrome
    and all their tabs. Detaching is the only correct thing to do to a
    browser we did not open.
    """
    global _driver

    with _lock:
        _driver = None


def _foreground_window_title():
    """The title of whichever window has OS-level focus right now.

    Read via ctypes straight to Windows' own user32.dll -- no extra
    dependency, and Smart App Control never objects to Microsoft's own
    signed DLLs. This has to be read before anything below touches
    Selenium at all: switch_to.window() itself visibly brings that tab
    to the front (this is documented, ordinary Selenium/CDP behaviour --
    it calls Target.activateTarget under the hood -- not a bug specific
    to this file), which would corrupt this exact signal if read
    afterward. Checking "does this tab have focus" by switching to it
    is self-defeating: the very act of checking makes the answer yes.

    Returns "" on anything other than Windows, or if the call fails for
    any reason -- callers already treat an empty title as "couldn't
    determine this" and fall back accordingly.
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)

        return buffer.value
    except Exception:
        return ""


def _titles_without_switching(driver):
    """Every open tab's title, via a raw CDP call rather than Selenium's
    switch_to.window() + driver.title.

    Target.getTargets reads a tab's title without ever activating or
    foregrounding it -- unlike the standard Selenium approach, this
    causes no visible disruption at all. Returns {handle: title}, or
    {} if the call isn't supported or nothing usable comes back; either
    is treated as "this path didn't work" by the caller, which falls
    back to a different approach rather than failing outright.
    """
    try:
        response = driver.execute_cdp_cmd("Target.getTargets", {})
    except Exception:
        return {}

    titles = {}

    for target in (response or {}).get("targetInfos", []):
        target_id = target.get("targetId")
        title = target.get("title")

        if target_id and title:
            titles[target_id] = title

    return titles


def _focus_active_window(driver):
    """Point the session at whichever tab is actually in front.

    Two layers. First, the fast path: read every tab's title without
    switching to any of them (_titles_without_switching), and match
    against the OS-level foreground window's title
    (_foreground_window_title) -- read before any of this touches
    Selenium, since switching corrupts that exact signal. If a match
    is found, this switches once, directly to the correct tab, with
    nothing else ever touched or made briefly visible.

    Falls back to switching through each tab in turn only if the CDP
    call didn't help (older Chrome, or its target IDs not matching
    Selenium's own window handles). That path is still correct -- it
    ends on the OS-confirmed right tab, not whichever it happened to
    check first -- but may be briefly visible while it checks.
    """
    try:
        handles = driver.window_handles
    except WebDriverException:
        return

    if len(handles) <= 1:
        return

    target_title = _foreground_window_title()

    try:
        started_on = driver.current_window_handle
    except WebDriverException:
        started_on = None

    if target_title:
        titles_by_handle = _titles_without_switching(driver)

        for handle in handles:
            title = titles_by_handle.get(handle)

            if title and title in target_title:
                try:
                    driver.switch_to.window(handle)
                except WebDriverException:
                    pass

                return

    # Fallback: the fast path found nothing usable. Still correct in
    # the end, just potentially visible while it checks each tab.
    matched = None

    for handle in handles:
        try:
            driver.switch_to.window(handle)
        except WebDriverException:
            continue

        try:
            tab_title = (driver.title or "").strip()
        except WebDriverException:
            continue

        if tab_title and target_title and tab_title in target_title:
            matched = handle
            break

    final = matched or started_on

    if final and final != driver.current_window_handle:
        try:
            driver.switch_to.window(final)
        except WebDriverException:
            pass


def _current(driver):
    """(title, url) for whatever tab is actually in front, safely."""
    _focus_active_window(driver)

    try:
        return (driver.title or "").strip(), (driver.current_url or "").strip()
    except WebDriverException:
        return "", ""


_LEADING_COUNT = re.compile(r"^\(\d+\+?\)\s*")


def _spoken_title(title):
    """A page title fit to say aloud.

    Strips a leading notification-count badge -- YouTube, Gmail, and
    plenty of other sites prepend "(7) " to the title while there are
    unread items, which reads strangely spoken aloud ("You're on
    seven YouTube") and has nothing to do with what page this is.
    """
    return _LEADING_COUNT.sub("", title or "").strip()


def _speakable_host(host):
    """A domain with every dot spelled out as the word "dot".

    Verified directly against the real edge-tts CLI, not guessed:
    "bbc.co.uk" read as literal text mispronounces ".co.", and
    "bbc co dot uk" (only one dot spelled out) drops the other one
    entirely -- the engine is making a per-dot judgment call about
    whether it's a decimal point, an abbreviation, or a domain
    separator, and getting ".co." wrong specifically. Spelling out
    every dot removes that judgment call rather than trying to predict
    which one it'll get wrong.
    """
    return host.replace(".", " dot ")


def _site_label(url):
    host = urlparse(url).netloc or url
    host = host[4:] if host.startswith("www.") else host

    return _speakable_host(host)


def resolve_target(spoken):
    """Turn what was said into a URL to visit, or None.

    Three shapes: a known site by name, something that already looks like
    a domain, or anything else, which becomes a search.
    """
    text = (spoken or "").strip()

    if not text:
        return None

    lowered = text.casefold().strip()

    for prefix in ("go to ", "open ", "visit ", "navigate to ", "browse to "):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):].strip()
            text = text[len(prefix):].strip()

    # "the bbc" is the bbc. Harmless to drop, and the caller does not
    # always strip it before asking.
    if lowered.startswith("the "):
        lowered = lowered[4:].strip()
        text = text[4:].strip() if text.lower().startswith("the ") else text

    if not lowered:
        return None

    if lowered in _KNOWN_SITES:
        return _KNOWN_SITES[lowered]

    # Spoken domains lose their dots to normalisation ("bbc co uk"), so
    # rejoin the tail when the words look like one.
    rejoined = re.sub(r"\s+(co|com|org|net|uk|io|dev)\b", r".\1", lowered)

    if _DOMAIN.match(rejoined.replace(" ", "")):
        candidate = rejoined.replace(" ", "")

        if not candidate.startswith(("http://", "https://")):
            candidate = f"https://{candidate}"

        return candidate

    if _DOMAIN.match(text):
        return text if text.startswith("http") else f"https://{text}"

    return None


def search_url(query):
    return _SEARCH_URL.format(query=quote_plus((query or "").strip()))


def navigate(url):
    """Point the browser at a URL. Returns a spoken sentence."""
    if not _AVAILABLE:
        return "I don't have browser control installed, sir."

    with _lock:
        driver = _connect()

        if not driver:
            journal.browser("navigate", url, "no browser")
            return _how_to_start()

        try:
            driver.get(url)

        except WebDriverException as error:
            print(f"[JARVIS] could not navigate: {error}")
            journal.browser("navigate", url, "failed")

            return f"I couldn't open {_site_label(url)}, sir."

        title, current = _current(driver)

        journal.browser("navigate", current or url, "ok")

        if title:
            return f"{safety.clean(title, 120)}, sir."

        return f"{_site_label(current or url)} is open, sir."


def search(query):
    """Search the web and land on the results page."""
    text = (query or "").strip()

    if not text:
        return "What should I search for, sir?"

    outcome = navigate(search_url(text))

    if outcome and outcome.endswith(", sir."):
        return f"Searching for {text}, sir. {outcome}"

    return outcome


def _page_text(driver):
    """The readable part of the current page, trimmed."""
    try:
        raw = driver.execute_script(_EXTRACT_JS) or ""
    except WebDriverException as error:
        print(f"[JARVIS] could not read the page: {error}")
        return ""

    return str(raw)[:_MAX_PAGE_CHARS].strip()


def page_text():
    """Raw readable text of the current page, for saving to a file.

    Returns (title, url, text). Text is empty when nothing was readable.
    """
    if not _AVAILABLE:
        return "", "", ""

    with _lock:
        driver = _connect()

        if not driver:
            journal.browser("read", "current page", "no browser")
            return "", "", ""

        title, url = _current(driver)
        body = _page_text(driver)

        journal.browser("read", url, f"{len(body.split())} words")

        return title, url, body


def search_results():
    """Return visible external search-result links from the current page."""
    if not _AVAILABLE:
        return []

    with _lock:
        driver = _connect()

        if not driver:
            return []

        try:
            return driver.execute_script(
                """
                const results = [];
                const seen = new Set();

                for (const link of document.querySelectorAll('a[href]')) {
                    const href = link.href || '';
                    const text = (link.innerText || '').trim();

                    if (!href || !text) {
                        continue;
                    }

                    if (href.startsWith('javascript:')) {
                        continue;
                    }

                    let url;

                    try {
                        url = new URL(href);
                    } catch {
                        continue;
                    }

                    const host = url.hostname.toLowerCase();

                    if (
                        host === 'google.com' ||
                        host.endsWith('.google.com') ||
                        host === 'google.co.uk' ||
                        host.endsWith('.google.co.uk')
                    ) {
                        continue;
                    }

                    if (text.length < 4 || text.length > 300) {
                        continue;
                    }

                    if (seen.has(href)) {
                        continue;
                    }

                    seen.add(href);

                    results.push({
                        title: text,
                        url: href,
                    });

                    if (results.length >= 20) {
                        break;
                    }
                }

                return results;
                """
            ) or []

        except Exception as error:
            print(f"[JARVIS] could not read search results: {error}")
            return []


def _spoken_opening(body):
    """The first bit of a page, cut at a sentence rather than mid-word."""
    words = body.split()

    if len(words) <= _SPOKEN_WORDS:
        return " ".join(words)

    opening = " ".join(words[:_SPOKEN_WORDS])

    # Prefer to stop at the last full sentence, so it does not trail off.
    cut = max(opening.rfind(". "), opening.rfind("! "), opening.rfind("? "))

    if cut > len(opening) // 2:
        return opening[:cut + 1]

    return f"{opening}..."


def describe_page():
    """Read the current page: what it is, how long, and the opening."""
    if not _AVAILABLE:
        return "I don't have browser control installed, sir."

    title, url, body = page_text()

    if not url:
        return _how_to_start()

    if not body:
        return "There's nothing readable on that page, sir."

    # Page text is outside content. It cannot give orders here — nothing
    # below feeds it to a model — but it can still be written to fool the
    # person listening, so it gets flagged rather than read out neutrally.
    warning = safety.warning_for(body)

    words = len(body.split())
    heading = safety.clean(_spoken_title(title), 120) or _site_label(url)
    opening = safety.clean(_spoken_opening(body), 900)

    spoken = f"{heading}, sir. About {words} words. It begins: {opening}"

    if warning:
        return f"{spoken} — {warning}"

    return spoken


def describe_overview():
    """What's on this page: its headings, and where it can go."""
    if not _AVAILABLE:
        return "I don't have browser control installed, sir."

    with _lock:
        driver = _connect()

        if not driver:
            journal.browser("overview", "current page", "no browser")
            return _how_to_start()

        title, url = _current(driver)

        try:
            found = driver.execute_script(_OVERVIEW_JS) or {}
        except WebDriverException as error:
            print(f"[JARVIS] could not survey the page: {error}")
            journal.browser("overview", url, "failed")

            return "I couldn't read that page, sir."

    headings = [safety.clean(h, 100) for h in (found.get("headings") or [])]
    links = [
        safety.clean((link or {}).get("text"), 60)
        for link in (found.get("links") or [])
    ]

    headings = [h for h in headings if h][:6]
    links = [link for link in links if link][:8]

    journal.browser(
        "overview", url, f"{len(headings)} headings, {len(links)} links"
    )

    heading = safety.clean(_spoken_title(title), 120) or _site_label(url)

    if not headings and not links:
        return f"{heading}, sir. Nothing on it I can pick out."

    parts = [f"You're on {heading}, sir."]

    if headings:
        parts.append(f"Sections: {_listed(headings)}.")

    if links:
        parts.append(f"Links include {_listed(links)}.")

    return " ".join(parts)


def describe_current():
    """Which page is in front, without reading it."""
    if not _AVAILABLE:
        return "I don't have browser control installed, sir."

    with _lock:
        driver = _connect()

        if not driver:
            journal.browser("current", "current page", "no browser")
            return _how_to_start()

        title, url = _current(driver)

    if not url:
        return "There's nothing open, sir."

    heading = safety.clean(_spoken_title(title), 120) or _site_label(url)

    return f"You're on {heading}, sir, at {_site_label(url)}."


def _listed(items):
    """Read a short list the way a person would say it."""
    if not items:
        return ""

    if len(items) == 1:
        return items[0]

    return ", ".join(items[:-1]) + f", and {items[-1]}"
