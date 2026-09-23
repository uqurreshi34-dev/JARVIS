"""Where every aircraft goes between the feed and the spoken count.

The feed said 281 and JARVIS said 95. Both numbers can be right -- an
aeroplane parked at a gate is not something flying overhead -- but
"can be" is not "is", and the only way to tell is to follow every row
through the same gates the real code uses and see which one it falls
through.

So this fetches once and accounts for all of it. Nothing is summarised
away: every row lands in exactly one bucket, the buckets add up to the
total, and each bucket prints examples so the decision to drop it can
be judged rather than trusted.

    python tools/aircraft_count_probe.py
    python tools/aircraft_count_probe.py --lat 48.8566 --lon 2.3522
    python tools/aircraft_count_probe.py --radius 50 --show 12

The gates, in the order actions/aircraft.py applies them:

    1. alt_baro == "ground"        dropped in the parser
    2. alt_baro not a number       kept on the radar face, but silently
                                   missing from the spoken count
    3. no latitude or longitude    kept, but cannot be placed or ranged
    4. everything else             plotted and counted

Gate 2 is the one worth staring at. A row dropped there still appears
as a contact on the face, so the face and the sentence disagree, and
nothing anywhere says so.

No JARVIS imports, no keys, nothing written. The filters below are
copied from actions/aircraft.py rather than imported precisely so that
this can disagree with it -- an oracle that shares the code under test
cannot catch the code under test being wrong.
"""

import argparse
import json
import math
import urllib.error
import urllib.request


DEFAULT_LAT = 52.48
DEFAULT_LON = -1.90
DEFAULT_RADIUS_NM = 100

TIMEOUT = 20

_METRES_PER_NM = 1852.0
_EARTH_RADIUS_M = 6371000.0


def get(url):
    """(status, body). status is None when nothing landed."""
    request = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")

    except urllib.error.HTTPError as error:
        body = ""

        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:
            pass

        return error.code, body

    except (urllib.error.URLError, OSError) as error:
        return None, f"{type(error).__name__}: {error}"


def number(value):
    """A float, or None. Exactly what aircraft.py's _number does."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def distance_nm(origin, target):
    """Great-circle distance, so 'within the radius' can be checked."""
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    lat2, lon2 = math.radians(target[0]), math.radians(target[1])

    dlat, dlon = lat2 - lat1, lon2 - lon1

    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)

    return (2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))) / _METRES_PER_NM


def label(row):
    """Something to call it, in the order aircraft.py prefers."""
    for key in ("flight", "r", "t", "hex"):
        value = (row.get(key) or "").strip()

        if value:
            return value

    return "?"


def classify(rows, origin, radius_nm):
    """Every row into exactly one bucket. Nothing is discarded quietly."""
    buckets = {
        "not a dict": [],
        "on the ground": [],
        "altitude not a number": [],
        "no position": [],
        "counted": [],
    }

    outside = []

    for row in rows:
        if not isinstance(row, dict):
            buckets["not a dict"].append(row)
            continue

        raw = row.get("alt_baro")

        # Gate 1, in the parser.
        if raw == "ground":
            buckets["on the ground"].append(row)
            continue

        lat = number(row.get("lat"))
        lon = number(row.get("lon"))

        # Gate 3. Checked before gate 2 only for reporting; the real code
        # reaches the same verdict either way, since a row with neither
        # is dropped by whichever gate sees it first.
        if lat is None or lon is None:
            buckets["no position"].append(row)
            continue

        # How far out the feed actually placed it, against what we asked
        # for. A feed that overshoots its own radius would inflate the
        # count without any filter being at fault.
        away = distance_nm(origin, (lat, lon))

        if away > radius_nm + 0.5:
            outside.append((row, away))

        # Gate 2, in describe(): flying = [e for e in craft
        #                                  if e["altitude"] is not None]
        if number(raw) is None:
            buckets["altitude not a number"].append(row)
            continue

        buckets["counted"].append(row)

    return buckets, outside


def report(buckets, outside, total, radius_nm, show):
    spoken = len(buckets["counted"])
    on_face = spoken + len(buckets["altitude not a number"])

    print(f"{total} rows from the feed.\n")

    order = ("on the ground", "altitude not a number", "no position",
             "not a dict", "counted")

    width = max(len(name) for name in order)

    for name in order:
        rows = buckets[name]

        if not rows:
            continue

        share = 100.0 * len(rows) / total if total else 0.0
        print(f"  {name:<{width}}  {len(rows):>4}  ({share:>4.1f}%)")

    counted = sum(len(rows) for rows in buckets.values())

    print(f"  {'':<{width}}  {'----':>4}")
    print(f"  {'total':<{width}}  {counted:>4}   "
          f"{'accounted for' if counted == total else 'MISMATCH'}")

    print()
    print(f"JARVIS would say:      {spoken} aircraft within {radius_nm:g} nm")
    print(f"The face would show:   {on_face} contacts")

    if on_face != spoken:
        print()
        print(f"  ** The face and the sentence disagree by "
              f"{on_face - spoken}. **")
        print("  A row whose altitude is not a number is still drawn as a")
        print("  contact, but describe() filters it out of the count. Both")
        print("  numbers are on screen at once, so the gap is visible.")

    for name in ("on the ground", "altitude not a number", "no position"):
        rows = buckets[name]

        if not rows:
            continue

        print()
        print(f"--- dropped: {name} ({len(rows)})")

        for row in rows[:show]:
            print(f"      {label(row):<10} alt_baro={row.get('alt_baro')!r:<12} "
                  f"gs={row.get('gs')!r} type={row.get('t') or '?'}")

        if len(rows) > show:
            print(f"      ... and {len(rows) - show} more")

    if outside:
        print()
        print(f"--- outside the {radius_nm:g} nm asked for ({len(outside)})")

        for row, away in sorted(outside, key=lambda p: -p[1])[:show]:
            print(f"      {label(row):<10} {away:.1f} nm out")

        print("      The feed returned these; nothing in JARVIS trims them.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS_NM,
                        help="nautical miles (default 100)")
    parser.add_argument("--show", type=int, default=8,
                        help="examples to print per dropped bucket")
    args = parser.parse_args()

    url = (f"https://api.adsb.lol/v2/point/"
           f"{args.lat:.4f}/{args.lon:.4f}/{args.radius:g}")

    print(f"{args.lat:.4f}, {args.lon:.4f} — {args.radius:g} nm")
    print(f"{url}\n")

    status, body = get(url)

    if status != 200:
        print(f"HTTP {status}")
        print((body or "(empty)")[:400])

        return 1

    try:
        rows = json.loads(body).get("ac")
    except (ValueError, AttributeError):
        print("The feed answered, but not with JSON we understand:")
        print(body[:400])

        return 1

    if not isinstance(rows, list):
        print(f"Expected a list of aircraft, got {type(rows).__name__}.")

        return 1

    buckets, outside = classify(rows, (args.lat, args.lon), args.radius)

    report(buckets, outside, len(rows), args.radius, args.show)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
