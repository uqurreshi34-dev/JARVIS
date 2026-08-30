"""Where you are, when the phone has said so.

The desk cannot answer this. A PC knows the city it was told about and
nothing more, so anything about where you actually are has to come from
the phone, and only when the phone is looking -- a browser stops
reporting position the moment its page is backgrounded or the screen
locks. That is a real limit and this module does not pretend otherwise:
it holds the last position reported and how old it is, so an answer can
say "you were at the station eight minutes ago" rather than implying it
knows where you are now.

Home is stored separately, in memory.py, because it is a fact about you
rather than a reading. It is captured by standing in it and saying so:
the coordinates already in memory are the city geocoded for the weather,
which is nowhere near precise enough to tell whether you are home.
"""

import math
import threading
import time

from actions import memory


# A position older than this is stale enough that it should be quoted
# with its age rather than stated flatly. Not discarded -- knowing where
# you were an hour ago is still worth having.
FRESH_SECONDS = 300

# Within this many metres of home counts as being home. Generous on
# purpose: phone GPS is commonly out by twenty metres and considerably
# worse indoors, so a tight radius would report you out when you are
# sitting on your own sofa.
HOME_RADIUS_METRES = 120

_lock = threading.Lock()

_last = {
    "latitude": None,
    "longitude": None,
    "accuracy": None,
    "at": None,
}


def remember_position(latitude, longitude, accuracy=None):
    """Record where the phone says it is. Returns True when accepted."""
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return False

    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return False

    try:
        accuracy = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        accuracy = None

    with _lock:
        _last.update({
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy,
            "at": time.monotonic(),
        })

    return True


def last_position():
    """The last reported position, or None."""
    with _lock:
        if _last["latitude"] is None:
            return None

        return dict(_last)


def age_seconds():
    """How long ago the position was reported, or None."""
    with _lock:
        if _last["at"] is None:
            return None

        return time.monotonic() - _last["at"]


def _spoken_age(seconds):
    """How long ago, said the way a person would."""
    if seconds is None:
        return ""

    if seconds < 90:
        return "just now"

    minutes = int(seconds // 60)

    if minutes < 60:
        return f"{minutes} minutes ago"

    hours = int(minutes // 60)

    if hours == 1:
        return "an hour ago"

    return f"{hours} hours ago"


def distance_metres(first, second):
    """Metres between two (latitude, longitude) pairs.

    The haversine formula: good to a few metres over the distances that
    matter here, and it needs no dependency.
    """
    radius = 6371000.0

    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])

    d_lat = lat2 - lat1
    d_lon = lon2 - lon1

    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )

    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def spoken_distance(metres):
    """A distance said the way a person would say it."""
    if metres < 1000:
        return f"{int(round(metres / 10.0) * 10)} metres"

    miles = metres / 1609.344

    if miles < 10:
        return f"{miles:.1f} miles"

    return f"{int(round(miles))} miles"


def set_home():
    """Make where the phone last was the definition of home."""
    position = last_position()

    if not position:
        return (
            "I don't know where you are, sir. Tap the location button "
            "on your phone first."
        )

    memory.set_home_coordinates(position["latitude"], position["longitude"])

    return "Noted, sir. I'll remember this as home."


def distance_from_home():
    """(metres, position) or (None, None) when either end is unknown."""
    position = last_position()
    home = memory.home_coordinates()

    if not position or home[0] is None:
        return None, None

    here = (position["latitude"], position["longitude"])

    return distance_metres(here, home), position


def is_home():
    """True, False, or None when it cannot be told."""
    metres, _ = distance_from_home()

    if metres is None:
        return None

    return metres <= HOME_RADIUS_METRES


def describe_position():
    """A spoken answer to "where am I"."""
    position = last_position()

    if not position:
        return (
            "I don't know where you are, sir. Tap the location button "
            "on your phone and ask again."
        )

    when = _spoken_age(age_seconds())
    metres, _ = distance_from_home()

    if metres is None:
        return (
            f"Your phone reported {position['latitude']:.4f}, "
            f"{position['longitude']:.4f}, {when}, sir. I don't know "
            "where home is yet -- say 'this is home' while you're there."
        )

    if metres <= HOME_RADIUS_METRES:
        return f"You were at home {when}, sir."

    return (
        f"You were {spoken_distance(metres)} from home {when}, sir."
    )


def describe_distance():
    """A spoken answer to "how far am I from home"."""
    metres, position = distance_from_home()

    if position is None:
        return (
            "I don't know where you are, sir. Tap the location button "
            "on your phone first."
        )

    if metres is None:
        return (
            "I don't know where home is, sir. Say 'this is home' while "
            "you're standing in it."
        )

    when = _spoken_age(age_seconds())

    if metres <= HOME_RADIUS_METRES:
        return f"You were home {when}, sir."

    return f"{spoken_distance(metres)}, sir, as of {when}."
