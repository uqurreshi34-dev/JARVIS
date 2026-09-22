"""What is flying overhead, from whichever feed will answer.

Two public feeds carry live ADS-B positions and neither needs a key. They
disagree about almost everything else -- field names, altitude in feet
against metres, speed in knots against metres per second -- so nothing
above this module ever sees a feed's own shape. Everything here comes out
in one form: metres, metres per second, degrees.

The feeds are tried in order, as providers.py does for models, and for
the same reason: one being down should cost a slower answer rather than
no answer.

    adsb.lol      no rate limit seen, carries registration and type
    OpenSky       400 requests of budget, carries the operator's country

adsb.lol goes first precisely because OpenSky's budget is finite. A radar
face refreshing every few seconds would spend a day's OpenSky allowance
in an hour, so OpenSky is kept for when the other one is unreachable.

Where to look is never hardcoded. The phone's last position is best, the
remembered home is next, and the weather city is the fallback -- so this
works on a machine that has never seen a phone, just less precisely.
"""

import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

import phrases
from actions import location, memory


# How far around the point to look. Far enough to catch what is worth
# mentioning, near enough that the radar face is not a smear.
DEFAULT_RADIUS_NM = 25

# A fetch is reused for this long. The radar sweeps continuously but the
# sky does not change in a second, and a feed asked ten times a second
# stops answering whoever asks it next.
_CACHE_SECONDS = 12

_TIMEOUT = 12

_METRES_PER_FOOT = 0.3048
_METRES_PER_NM = 1852.0
_KNOTS_PER_MS = 1.9438445

# Compass points, in order from north. Used to turn a bearing into a word
# without a lookup table of every combination.
_POINTS = (
    "north", "north-east", "east", "south-east",
    "south", "south-west", "west", "north-west",
)


_lock = threading.Lock()
_cache = {"at": 0.0, "key": None, "aircraft": [], "source": ""}


def centre():
    """Where to look, and how we know. Returns (lat, lon, how) or None.

    Three sources, best first. Nothing here is a fixed coordinate, so the
    same code works wherever the machine is.
    """
    position = location.last_position()

    if position and position.get("latitude") is not None:
        return position["latitude"], position["longitude"], "your phone"

    lat, lon = memory.home_coordinates()

    if lat is not None:
        return lat, lon, "home"

    try:
        lat, lon = memory.coordinates()
    except Exception:
        lat, lon = None, None

    if lat is not None:
        return float(lat), float(lon), "your usual area"

    return None


def _get(url):
    """The body of a GET, or None."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "JARVIS/1.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")

    except (urllib.error.URLError, OSError) as error:
        print(f"[JARVIS] aircraft feed unreachable: {error}")
        return None


def _number(value):
    """A float, or None. Feeds send 'ground' and null where a number goes."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _from_adsb_lol(lat, lon, radius_nm):
    """adsb.lol. Feet, knots, and a registration worth having."""
    body = _get(
        f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius_nm:g}"
    )

    if not body:
        return None

    try:
        rows = json.loads(body).get("ac")
    except (ValueError, AttributeError):
        return None

    if not isinstance(rows, list):
        return None

    craft = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        # This feed writes the word rather than a number for anything
        # sitting on a runway. OpenSky has its own on_ground flag, and
        # both are dropped, so neither feed puts a parked aeroplane on
        # the radar.
        if row.get("alt_baro") == "ground":
            continue

        altitude = _number(row.get("alt_baro"))
        speed = _number(row.get("gs"))
        climb = _number(row.get("baro_rate"))

        craft.append({
            "id": row.get("hex") or "",
            "callsign": (row.get("flight") or "").strip(),
            "registration": (row.get("r") or "").strip(),
            "type": (row.get("t") or "").strip(),
            "country": "",
            "latitude": _number(row.get("lat")),
            "longitude": _number(row.get("lon")),
            # Feet in, metres out. The one conversion that matters, since
            # 1550 feet and 1550 metres are a different aeroplane.
            "altitude": altitude * _METRES_PER_FOOT if altitude else altitude,
            "speed": speed / _KNOTS_PER_MS if speed else speed,
            "track": _number(row.get("track")),
            "climb": climb * _METRES_PER_FOOT / 60.0 if climb else climb,
        })

    return craft


# OpenSky hands back a bare list per aircraft. Named here so the parser
# below reads as something other than a column of magic numbers.
_OPENSKY = {
    "id": 0, "callsign": 1, "country": 2, "longitude": 5, "latitude": 6,
    "altitude": 7, "on_ground": 8, "speed": 9, "track": 10, "climb": 11,
}


def _from_opensky(lat, lon, radius_nm):
    """OpenSky. Already metres and metres per second, but budgeted."""
    lat_span = radius_nm / 60.0
    lon_span = radius_nm / (60.0 * max(0.1, math.cos(math.radians(lat))))

    body = _get(
        "https://opensky-network.org/api/states/all"
        f"?lamin={lat - lat_span:.4f}&lomin={lon - lon_span:.4f}"
        f"&lamax={lat + lat_span:.4f}&lomax={lon + lon_span:.4f}"
    )

    if not body:
        return None

    try:
        states = json.loads(body).get("states")
    except (ValueError, AttributeError):
        return None

    if not isinstance(states, list):
        return None

    craft = []

    for row in states:
        if not isinstance(row, list):
            continue

        def field(name):
            index = _OPENSKY[name]

            return row[index] if index < len(row) else None

        if field("on_ground"):
            continue

        craft.append({
            "id": field("id") or "",
            "callsign": (field("callsign") or "").strip(),
            "registration": "",
            "type": "",
            "country": field("country") or "",
            "latitude": _number(field("latitude")),
            "longitude": _number(field("longitude")),
            "altitude": _number(field("altitude")),
            "speed": _number(field("speed")),
            "track": _number(field("track")),
            "climb": _number(field("climb")),
        })

    return craft


# Order matters: the first that answers is used. adsb.lol leads because
# its budget is not visibly finite and OpenSky's is.
_SOURCES = (
    ("adsb.lol", _from_adsb_lol),
    ("OpenSky", _from_opensky),
)


def bearing(origin, target):
    """Degrees from true north, origin to target."""
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    lat2, lon2 = math.radians(target[0]), math.radians(target[1])

    delta = lon2 - lon1

    y = math.sin(delta) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(delta))

    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass(degrees):
    """A bearing as a word. Eight points is as precise as speech needs."""
    if degrees is None:
        return ""

    step = 360.0 / len(_POINTS)

    return _POINTS[int((degrees + step / 2) % 360.0 // step)]


def overhead(radius_nm=DEFAULT_RADIUS_NM, force=False):
    """Everything flying near you, nearest first.

    Returns (aircraft, source, origin) where origin is (lat, lon, how), or
    (None, "", None) when there is nowhere to look or nothing will answer.

    Cached briefly, so a radar face redrawing at thirty frames a second
    costs one request every few seconds rather than thirty thousand.
    """
    here = centre()

    if not here:
        return None, "", None

    lat, lon, _ = here
    key = (round(lat, 3), round(lon, 3), radius_nm)

    with _lock:
        fresh = (
            not force
            and _cache["key"] == key
            and time.time() - _cache["at"] < _CACHE_SECONDS
        )

        if fresh:
            return list(_cache["aircraft"]), _cache["source"], here

    for name, fetch in _SOURCES:
        try:
            craft = fetch(lat, lon, radius_nm)
        except Exception as error:
            print(f"[JARVIS] {name} failed: {error}")
            continue

        if craft is None:
            continue

        for entry in craft:
            if entry["latitude"] is None or entry["longitude"] is None:
                entry["range"] = None
                entry["bearing"] = None
                continue

            target = (entry["latitude"], entry["longitude"])

            entry["range"] = location.distance_metres((lat, lon), target)
            entry["bearing"] = bearing((lat, lon), target)

        craft.sort(key=lambda e: (e["range"] is None, e["range"] or 0.0))

        with _lock:
            _cache.update({
                "at": time.time(), "key": key,
                "aircraft": list(craft), "source": name,
            })

        return craft, name, here

    return None, "", here


# Two halves, looked for anywhere and in either order, so "planes
# overhead" and "what's flying above me" both land without either being
# listed. A request has to name something that flies AND somewhere to
# look, which is what stops "book me a flight" arriving here.
_CRAFT = re.compile(
    r"\b(aircraft|aeroplanes?|airplanes?|planes?|jets?|flights?|flying)\b"
)

_SKY = re.compile(
    r"\b(overhead|above|nearby|around|up there|in the sky|over me|"
    r"near me|in the air)\b"
)

_RADAR = re.compile(r"\b(radar|the sky)\b")


def wanted(command):
    """Whether a command is asking about the sky. True or False."""
    text = (command or "").casefold()

    if _RADAR.search(text):
        return True

    return bool(_CRAFT.search(text) and _SKY.search(text))


def _feet(metres):
    return int(round(metres / _METRES_PER_FOOT / 100.0) * 100)


def _miles(metres):
    return metres / _METRES_PER_NM


def _away(metres, degrees):
    """How far and which way, as something sayable.

    Anything closer than a mile and a half is overhead in any sense that
    matters, and a bearing to it would be noise -- an aircraft that close
    is above you, not to your north.
    """
    if metres is None:
        return ""

    miles = _miles(metres)

    if miles < 1.5:
        return ", almost directly overhead"

    if miles < 2.5:
        return f", a mile or so {compass(degrees)}"

    return f", {miles:.0f} miles {compass(degrees)}"


def describe(radius_nm=DEFAULT_RADIUS_NM):
    """One spoken sentence about the sky, or why there isn't one."""
    craft, source, here = overhead(radius_nm)

    if here is None:
        return ("I don't know where you are, sir. Send me a position from "
                "your phone and I'll watch the sky from there.")

    if craft is None:
        return "I couldn't reach the aircraft feeds, sir."

    flying = [e for e in craft if e["altitude"] is not None]

    if not flying:
        return f"Nothing in the air within {radius_nm:g} miles, sir."

    # "One aircraft" rather than "1 aircraft" -- the same helper every
    # other spoken count in JARVIS goes through.
    lines = [
        f"{phrases.number(len(flying)).capitalize()} aircraft within "
        f"{radius_nm:g} miles, sir."
    ]

    nearest = flying[0]
    label = (nearest["callsign"] or nearest["registration"]
             or nearest["type"] or "One")

    if nearest["range"] is not None:
        lines.append(
            f"The nearest is {label}, {_feet(nearest['altitude']):,} feet"
            f"{_away(nearest['range'], nearest['bearing'])}."
        )

    highest = max(flying, key=lambda e: e["altitude"])

    if highest is not nearest:
        top = (highest["callsign"] or highest["registration"]
               or highest["type"] or "the highest")

        lines.append(f"The highest is {top} at "
                     f"{_feet(highest['altitude']):,} feet.")

    return " ".join(lines)
