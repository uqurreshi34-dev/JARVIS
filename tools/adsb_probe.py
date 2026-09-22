"""Which live aircraft feed JARVIS can actually use, and how hard.

Three public feeds claim to answer "what is flying near this point".
They differ in whether they still allow anonymous access at all, how
many requests they permit, and what they hand back -- and none of that
can be taken from documentation, because all three have tightened their
terms at various points.

So this asks all three, once, and then asks the best one repeatedly to
find where it starts refusing. One run decides the design: a feed with a
generous limit can be polled live behind a radar sweep, and a stingy one
has to be cached and refreshed on a timer instead.

    python tools/adsb_probe.py                      # defaults below
    python tools/adsb_probe.py --lat 53.48 --lon -2.24
    python tools/adsb_probe.py --burst 30           # find the limit

No keys, no JARVIS imports, nothing written. Safe to run while JARVIS is
closed, and safe to stop at any point.
"""

import argparse
import json
import math
import time
import urllib.error
import urllib.request


# Somewhere over England, so the default run has traffic in it whoever
# happens to be running this. Pass --lat/--lon for your own sky.
DEFAULT_LAT = 51.5074
DEFAULT_LON = -0.1278

# How far around the point to look. Kept modest: a radar face that shows
# four hundred aircraft is a smear, not a display.
DEFAULT_RADIUS_NM = 25

TIMEOUT = 15

# OpenSky returns each aircraft as a bare list. These are the positions
# that matter, named, so the probe can report what is actually usable
# rather than printing seventeen anonymous fields.
OPENSKY_FIELDS = {
    0: "icao24",
    1: "callsign",
    2: "origin_country",
    5: "longitude",
    6: "latitude",
    7: "baro_altitude",
    8: "on_ground",
    9: "velocity",
    10: "true_track",
    11: "vertical_rate",
    13: "geo_altitude",
}


def get(url):
    """(status, body, headers). status is None when nothing landed."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "JARVIS-probe/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return (
                response.status,
                response.read().decode("utf-8", errors="replace"),
                dict(response.headers),
            )

    except urllib.error.HTTPError as error:
        body = ""

        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            pass

        return error.code, body, dict(error.headers or {})

    except (urllib.error.URLError, OSError) as error:
        return None, f"{type(error).__name__}: {error}", {}


def bounding_box(lat, lon, radius_nm):
    """A degrees box round a point, since OpenSky wants corners."""
    lat_span = radius_nm / 60.0
    lon_span = radius_nm / (60.0 * max(0.1, math.cos(math.radians(lat))))

    return lat - lat_span, lon - lon_span, lat + lat_span, lon + lon_span


def try_opensky(lat, lon, radius_nm):
    lamin, lomin, lamax, lomax = bounding_box(lat, lon, radius_nm)
    url = (
        "https://opensky-network.org/api/states/all"
        f"?lamin={lamin:.4f}&lomin={lomin:.4f}"
        f"&lamax={lamax:.4f}&lomax={lomax:.4f}"
    )

    status, body, headers = get(url)

    try:
        states = json.loads(body).get("states")
    except (ValueError, AttributeError):
        states = None

    craft = []

    for row in states or []:
        if not isinstance(row, list):
            continue

        entry = {}

        for index, name in OPENSKY_FIELDS.items():
            entry[name] = row[index] if index < len(row) else None

        craft.append(entry)

    return url, status, body, headers, craft


def _point_feed(name, url, lat_key="lat", lon_key="lon"):
    """adsb.lol and airplanes.live share a response shape."""
    status, body, headers = get(url)

    try:
        payload = json.loads(body)
    except ValueError:
        return url, status, body, headers, []

    rows = payload.get("ac") if isinstance(payload, dict) else None

    craft = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        craft.append({
            "icao24": row.get("hex"),
            "callsign": (row.get("flight") or "").strip(),
            "latitude": row.get(lat_key),
            "longitude": row.get(lon_key),
            "baro_altitude": row.get("alt_baro"),
            "velocity": row.get("gs"),
            "true_track": row.get("track"),
            "vertical_rate": row.get("baro_rate"),
            "origin_country": row.get("r"),
        })

    return url, status, body, headers, craft


def report(label, url, status, body, headers, craft, show):
    print(f"--- {label}")
    print(f"    {url}")
    print(f"    HTTP {status}")

    for key in ("X-Rate-Limit-Remaining", "X-Rate-Limit-Retry-After-Seconds",
                "Retry-After", "x-ratelimit-remaining"):
        if key in headers:
            print(f"    {key}: {headers[key]}")

    if status != 200:
        print(f"    body: {(body or '(empty)')[:240]}")
        print()
        return False

    print(f"    {len(craft)} aircraft in range")

    for entry in craft[:show]:
        callsign = (entry.get("callsign") or "?").strip() or "?"
        altitude = entry.get("baro_altitude")
        track = entry.get("true_track")

        print(f"      {callsign:<10} "
              f"alt {altitude if altitude is not None else '?':>7} "
              f"track {track if track is not None else '?':>6} "
              f"{entry.get('origin_country') or ''}")

    if craft:
        present = [k for k, v in craft[0].items() if v is not None]
        print(f"    fields present: {', '.join(present)}")

    print()

    return True


def burst(lat, lon, radius_nm, count):
    """Ask repeatedly until it refuses, to find the real limit."""
    print(f"--- burst: {count} OpenSky requests, back to back")

    started = time.time()
    ok = 0

    for attempt in range(1, count + 1):
        _, status, body, headers, _ = try_opensky(lat, lon, radius_nm)

        if status == 200:
            ok += 1
            continue

        remaining = headers.get("X-Rate-Limit-Remaining", "?")

        print(f"    refused on request {attempt} after {ok} good one(s) "
              f"in {time.time() - started:.1f}s")
        print(f"    HTTP {status}, remaining header: {remaining}")
        print(f"    body: {(body or '(empty)')[:200]}")

        return ok

    print(f"    {ok}/{count} succeeded in {time.time() - started:.1f}s "
          f"with no refusal")

    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS_NM,
                        help="nautical miles (default 25)")
    parser.add_argument("--show", type=int, default=6,
                        help="how many aircraft to print per feed")
    parser.add_argument("--burst", type=int, default=0,
                        help="after the comparison, hammer OpenSky this "
                             "many times to find where it refuses")
    args = parser.parse_args()

    print(f"{args.lat:.4f}, {args.lon:.4f} — {args.radius:g} nm\n")

    working = []

    url, status, body, headers, craft = try_opensky(
        args.lat, args.lon, args.radius
    )

    if report("OpenSky Network", url, status, body, headers, craft, args.show):
        working.append(("opensky", len(craft)))

    url = (f"https://api.adsb.lol/v2/point/"
           f"{args.lat:.4f}/{args.lon:.4f}/{args.radius:g}")
    result = _point_feed("adsb.lol", url)

    if report("adsb.lol", *result, args.show):
        working.append(("adsb.lol", len(result[4])))

    url = (f"https://api.airplanes.live/v2/point/"
           f"{args.lat:.4f}/{args.lon:.4f}/{args.radius:g}")
    result = _point_feed("airplanes.live", url)

    if report("airplanes.live", *result, args.show):
        working.append(("airplanes.live", len(result[4])))

    if not working:
        print("No feed answered. Nothing to build on yet.")
        return 1

    print("Usable: " + ", ".join(
        f"{name} ({count} aircraft)" for name, count in working
    ))

    if args.burst:
        print()
        burst(args.lat, args.lon, args.radius, args.burst)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
