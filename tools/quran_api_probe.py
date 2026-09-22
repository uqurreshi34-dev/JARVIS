"""What the Quran API actually answers, one verse at a time.

A recitation died mid-surah with data arriving as a string where a list
of editions was expected. The interesting part is that it was an HTTP
200: a 4xx or 5xx would have raised and been handled, so whatever came
back was a successful response carrying something else.

Two jobs, because the failure turned out to depend on which reciter was
chosen rather than on how fast verses were asked for.

    python tools/quran_api_probe.py                 # surah 18, 40 verses
    python tools/quran_api_probe.py --surah 2 --count 120
    python tools/quran_api_probe.py --reciter ar.husary

    python tools/quran_api_probe.py --audit         # every reciter in
                                                    # the HUD dropdown

The audit asks for a single verse from each edition the dropdown offers,
and prints the ones that cannot answer along with whatever they said
instead. It also prints each edition's type and format, since if the
working and failing sets differ by a field, that field is the fix.

No keys, no JARVIS imports, nothing cached and nothing written, so it can
be run while JARVIS is closed or from any machine at all.
"""

import argparse
import json
import time
import urllib.error
import urllib.request


API = "https://api.alquran.cloud/v1"

RECITER = "ar.alafasy"
TRANSLATION = "en.sahih"

TIMEOUT = 20


def get(url):
    """(status, body). status is None when the request never landed."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", errors="replace")

    except urllib.error.HTTPError as error:
        body = ""

        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            pass

        return error.code, body

    except (urllib.error.URLError, OSError) as error:
        return None, f"{type(error).__name__}: {error}"


def ask(surah, ayah, reciter, translation):
    """One verse. Returns (status, body, editions-or-None)."""
    status, body = get(
        f"{API}/ayah/{surah}:{ayah}/editions/{reciter},{translation}"
    )

    try:
        payload = json.loads(body)
    except ValueError:
        return status, body, None

    data = payload.get("data") if isinstance(payload, dict) else None

    return status, body, data if isinstance(data, list) else None


def dropdown_reciters():
    """The editions JARVIS offers, filtered exactly as quran.py does."""
    status, body = get(f"{API}/edition/format/audio")

    try:
        editions = json.loads(body).get("data")
    except (ValueError, AttributeError):
        editions = None

    if not isinstance(editions, list):
        print(f"Could not list editions (HTTP {status}):")
        print(body[:600])

        return []

    return [
        edition
        for edition in editions
        if isinstance(edition, dict)
        and edition.get("language") == "ar"
        and edition.get("identifier")
    ]


def reachable(url):
    """Whether an mp3 is actually there. (ok, detail)."""
    if not url:
        return False, "no url"

    request = urllib.request.Request(url, method="HEAD")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            length = response.headers.get("Content-Length")

            if response.status != 200:
                return False, f"HTTP {response.status}"

            if length is not None and int(length) < 1024:
                return False, f"only {length} bytes"

            return True, f"{int(length) // 1024} KB" if length else "ok"

    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"

    except (urllib.error.URLError, OSError, ValueError) as error:
        return False, type(error).__name__


def audit(surah, ayah, translation):
    """Can each reciter in the dropdown actually deliver this verse?

    Text and audio are separate questions. Every edition answers the text
    endpoint; what differs is whether the mp3 it points at exists. That
    is the half that decides whether a recitation plays or stops, so it
    is the half this checks.
    """
    editions = dropdown_reciters()

    if not editions:
        return 1

    print(f"{len(editions)} reciter(s) in the dropdown. "
          f"Checking the audio for {surah}:{ayah}.")
    print()

    good = []
    primary_dead = []
    no_audio = []

    for edition in editions:
        identifier = edition["identifier"]
        _, body, found = ask(surah, ayah, identifier, translation)

        audio = ""
        backups = []

        for entry in found or []:
            if not isinstance(entry, dict):
                continue

            if (entry.get("edition") or {}).get("identifier") != identifier:
                continue

            audio = entry.get("audio") or ""
            backups = [u for u in (entry.get("audioSecondary") or []) if u]

        if not audio:
            no_audio.append((identifier, len(backups)))
            continue

        ok, detail = reachable(audio)

        if ok:
            good.append(identifier)
            continue

        # The primary is dead. Is anything else offered, and does it work?
        rescued = ""

        for backup in backups:
            if reachable(backup)[0]:
                rescued = backup
                break

        primary_dead.append((identifier, detail, len(backups), bool(rescued)))

    print(f"{len(good)} play, {len(primary_dead)} have a dead primary url, "
          f"{len(no_audio)} offer no audio at all.")
    print()

    if primary_dead:
        print(f"{'identifier':<30} {'primary':<12} {'backups':<9} rescued?")
        print("-" * 68)

        for identifier, detail, count, rescued in primary_dead:
            print(f"{identifier:<30} {detail:<12} {count:<9} "
                  f"{'YES' if rescued else 'no'}")

        rescuable = sum(1 for row in primary_dead if row[3])

        print()
        print(f"{rescuable} of {len(primary_dead)} would play if the "
              f"fallback urls were used.")

    if no_audio:
        print()
        print("No audio url at all:")

        for identifier, count in no_audio:
            print(f"  {identifier}  ({count} backup url(s))")

    return 0 if not (primary_dead or no_audio) else 1


def sweep(args):
    """The original job: many verses from one reciter, in order."""
    print(f"{args.surah}:{args.start} onwards, {args.count} verses")
    print(f"reciter {args.reciter}, translation {args.translation}")
    print()

    good = 0
    started = time.time()

    for offset in range(args.count):
        ayah = args.start + offset
        status, body, editions = ask(
            args.surah, ayah, args.reciter, args.translation
        )

        if editions is not None:
            good += 1

            if args.delay:
                time.sleep(args.delay)

            continue

        print(f"--- {args.surah}:{ayah} ---")
        print(f"after {good} good answer(s) in {time.time() - started:.1f}s")
        print(f"HTTP status: {status}")
        print("body:")
        print(body[:1200] if body else "(empty)")
        print()

        if not args.keep_going:
            print("Stopped at the first bad answer. Re-run with "
                  "--keep-going to see whether it recovers.")

            return 1

    print(f"{good}/{args.count} verses answered normally in "
          f"{time.time() - started:.1f}s. Nothing to report.")

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surah", type=int, default=18)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--reciter", default=RECITER)
    parser.add_argument("--translation", default=TRANSLATION)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds between requests (default 0, to provoke a limit)",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="carry on past the first bad answer instead of stopping",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="ask one verse of every reciter the dropdown offers",
    )
    args = parser.parse_args()

    if args.audit:
        return audit(args.surah, args.start, args.translation)

    return sweep(args)


if __name__ == "__main__":
    raise SystemExit(main())
