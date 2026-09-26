"""Open and close tabs in the JARVIS Chrome, precisely, by the tabs' own ids.

A protocol opens your morning sites and a clean slate closes them again --
those tabs and no others. That needs to know which tabs JARVIS opened, which
only Chrome's own debugging endpoint can say: every tab has an id, a tab is
opened by asking for one (PUT /json/new) and closed by naming it
(/json/close/<id>). No Selenium, no clicking, nothing read from the pages.

It is the same JARVIS Chrome the browser commands use (actions/browser.py,
jarvis-chrome.bat): a separate profile at %LOCALAPPDATA%/JarvisChrome with
its debugging port on localhost. Started here if it is not running, exactly
as the shortcut starts it. Your everyday Chrome is never touched -- Chrome
does not open that port on a normal profile at all.
"""

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


DEBUG_HOST = os.environ.get("JARVIS_CHROME_DEBUG", "127.0.0.1:9222")
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "JarvisChrome")

# How long a freshly started Chrome gets to open its port.
START_SECONDS = 15.0

_ASK_SECONDS = 3.0


def _host_port():
    host, _, port = DEBUG_HOST.partition(":")
    return host or "127.0.0.1", int(port or 9222)


def running():
    """Is the JARVIS Chrome listening right now?"""
    try:
        with socket.create_connection(_host_port(), timeout=0.4):
            return True
    except (OSError, ValueError):
        return False


def _chrome():
    """chrome.exe, where the JARVIS Chrome shortcut looks for it."""
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            path = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")

            if os.path.exists(path):
                return path

    return None


def _ask(path, method="GET"):
    host, port = _host_port()
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method)

    # Straight to Chrome on this machine: never through a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with opener.open(request, timeout=_ASK_SECONDS) as answer:
        body = answer.read().decode("utf-8", errors="replace")

    try:
        return json.loads(body)
    except ValueError:
        return body


def tabs():
    """The open tabs: [{"id", "url", "title"}]."""
    try:
        listed = _ask("/json/list")
    except (OSError, urllib.error.URLError):
        return []

    return [
        {"id": item.get("id"), "url": item.get("url", ""), "title": item.get("title", "")}
        for item in listed if isinstance(item, dict) and item.get("type") == "page"
    ] if isinstance(listed, list) else []


def start():
    """Start the JARVIS Chrome if it is not running. Returns (started, reason)."""
    if running():
        return False, None

    chrome = _chrome()

    if not chrome:
        return False, "Google Chrome isn't installed where I look for it"

    port = _host_port()[1]

    try:
        subprocess.Popen(
            [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE_DIR}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        return False, f"Chrome would not start ({error})"

    deadline = time.monotonic() + START_SECONDS

    while time.monotonic() < deadline:
        if running() and tabs():
            return True, None

        time.sleep(0.3)

    return False, "Chrome started but never opened its port"


_BLANK = ("chrome://newtab", "about:blank", "chrome://new-tab-page")


def open_tabs(urls):
    """Open each url in its own tab. Returns (ids opened, reason if none could be).

    When Chrome was started just for this, the empty tab it opens with is
    counted as opened by JARVIS too, so closing them all leaves nothing
    behind. Any tab that was already there is never included.
    """
    started, reason = start()

    if reason:
        return [], reason

    opened = []

    if started:
        opened += [tab["id"] for tab in tabs() if tab["url"].startswith(_BLANK)]

    sites = []

    for url in urls:
        target = urllib.parse.quote(str(url), safe=":/?&=#%+@,;~")

        try:
            made = _ask(f"/json/new?{target}", method="PUT")
        except (OSError, urllib.error.URLError) as error:
            print(f"[JARVIS] could not open a tab for {url}: {error}")
            continue

        if isinstance(made, dict) and made.get("id"):
            sites.append(made["id"])

    # The first of them in front, where you will look.
    if sites:
        try:
            _ask(f"/json/activate/{sites[0]}")
        except (OSError, urllib.error.URLError):
            pass

    opened += sites
    return opened, (None if sites else "Chrome would not open the tabs")


def close_tabs(ids):
    """Close the tabs with these ids, and only those. Returns how many closed.

    A tab already closed by hand, or a Chrome closed since, is simply not
    there to close -- not a failure.
    """
    if not ids or not running():
        return 0

    present = {tab["id"] for tab in tabs()}
    closed = 0

    for tab_id in ids:
        if tab_id not in present:
            continue

        try:
            _ask(f"/json/close/{tab_id}")
            closed += 1
        except (OSError, urllib.error.URLError) as error:
            print(f"[JARVIS] could not close tab {tab_id}: {error}")

    return closed
