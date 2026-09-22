"""What is underneath a point, in words.

An aircraft's position is two numbers, which tells you nothing. "Over
Erdington" tells you everything. This turns one into the other using
OpenStreetMap's Nominatim, which is free and needs no key.

Free and keyless comes with an obligation, and it is taken seriously
here: Nominatim's usage policy asks for no more than one request a
second and a User-Agent that says who is calling. Both are enforced
below rather than hoped for -- requests queue behind a lock that will
not let two through inside a second, and every answer is written to disk
so the same patch of ground is never asked about twice.

Nothing here knows about aircraft. It answers "what is at this point",
which is equally useful for anything else that has coordinates.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from actions import files


_API = "https://nominatim.openstreetmap.org/reverse"

_CACHE_NAME = "places.json"

# Their policy is one request a second. A little over, to be safe about
# clock resolution and to stay a good citizen of a service that costs
# nothing.
_MIN_INTERVAL = 1.1

_TIMEOUT = 10

# How finely to remember. Three decimal places is about a hundred
# metres, which is far finer than a place name changes, so it makes the
# cache hit constantly while never returning the wrong neighbourhood.
_PRECISION = 3

# Zoom 14 is roughly suburb level: fine enough to say Erdington rather
# than Birmingham, coarse enough not to return a street nobody knows.
_ZOOM = 14

# Which parts of an address to say, closest-in first. The first two that
# are present and different from each other are used, so a point lands
# as "Erdington, Birmingham" in a city and "Wythall, Worcestershire" in
# the country, with no special cases for either.
# Administrative wrapping that adds length without adding meaning.
# "Metropolitan Borough of Solihull" is Solihull to everyone who lives
# there, and a radar tag has no room for the rest. Matched by shape, so
# it handles the ones nobody thought to list.
_NOISE = re.compile(
    r"^(?:the\s+)?"
    r"(?:city|town|royal\s+borough|metropolitan\s+borough|london\s+borough|"
    r"borough|district|county|municipality|unitary\s+authority|"
    r"administrative\s+county)"
    r"\s+of\s+",
    re.IGNORECASE,
)

_PARTS = (
    "neighbourhood", "suburb", "village", "town", "hamlet",
    "city_district", "city", "county", "state", "country",
)


_lock = threading.Lock()
_last_call = 0.0

_cache = None
_cache_lock = threading.Lock()


def _path():
    root = files.root()

    return os.path.join(root, _CACHE_NAME) if root else None


def _load():
    global _cache

    with _cache_lock:
        if _cache is not None:
            return _cache

        _cache = {}
        path = _path()

        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    stored = json.load(handle)

                if isinstance(stored, dict):
                    _cache = stored

            except (OSError, ValueError) as error:
                print(f"[JARVIS] could not read place names: {error}")

        return _cache


def _save():
    path = _path()

    if not path:
        return

    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_load(), handle, ensure_ascii=False)
    except OSError as error:
        print(f"[JARVIS] could not cache place names: {error}")


def _key(latitude, longitude):
    return f"{round(latitude, _PRECISION)},{round(longitude, _PRECISION)}"


def _name_from(address):
    """Two levels of the address, closest in, without repeating itself."""
    if not isinstance(address, dict):
        return ""

    seen = []

    for part in _PARTS:
        value = _NOISE.sub("", (address.get(part) or "").strip()).strip()

        if value and value not in seen:
            seen.append(value)

        if len(seen) == 2:
            break

    return ", ".join(seen)


def describe_point(latitude, longitude):
    """Where this is, in words.

    Three outcomes, and the caller needs to tell them apart:

        "Erdington, Birmingham"   a name
        ""                        no name here -- open sea, usually.
                                  Remembered, so it is never asked twice
        None                      the lookup failed. Nothing is
                                  remembered, and asking again is fair

    Blocks for up to a second while it waits its turn, so it belongs on
    a background thread -- never on the one drawing the radar.
    """
    global _last_call

    if latitude is None or longitude is None:
        return ""

    key = _key(latitude, longitude)
    cache = _load()

    if key in cache:
        return cache[key]

    query = urllib.parse.urlencode({
        "lat": f"{latitude:.5f}",
        "lon": f"{longitude:.5f}",
        "format": "jsonv2",
        "zoom": _ZOOM,
        "addressdetails": 1,
    })

    request = urllib.request.Request(
        f"{_API}?{query}",
        headers={
            # Nominatim asks callers to identify themselves. Anything
            # anonymous is liable to be blocked, and fairly so.
            "User-Agent": "JARVIS/1.0 (personal assistant)",
            "Accept-Language": "en",
        },
    )

    with _lock:
        # One a second, measured from the last call rather than slept
        # unconditionally, so a lookup after a long pause is immediate.
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)

        if wait > 0:
            time.sleep(wait)

        _last_call = time.monotonic()

        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))

        except (urllib.error.URLError, ValueError, OSError) as error:
            print(f"[JARVIS] could not look up a place: {error}")

            # None, not "". A refused or timed-out request says nothing
            # about the ground below, so remembering it as nameless
            # would blank that point for good.
            return None

    name = _name_from(payload.get("address") if isinstance(payload, dict)
                      else None)

    if not name:
        name = (payload.get("name") or "").strip() if isinstance(
            payload, dict
        ) else ""

    # Remembered either way. A point over the sea has no name, and asking
    # again will not give it one.
    cache[key] = name
    _save()

    return name
