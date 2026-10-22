"""Sightings asked about in plain words (actions/sighting_search.py): when, and whether it was you.

    python tools/test_sighting_search.py

Checked in a sandboxed JARVIS folder, with records made here as sightings write them:

- times: today, yesterday, last night, this morning, this week, last week, on a weekday, the last hours;
- what counts as a question about the sightings kept, and what is left alone: what the camera sees now
  ("is anyone on the house cam"), its live view, what it detected, news, the last sighting's own words;
- who: you, anyone not recognised as you, anyone; "show me" is not "me";
- answers: when you were last seen, strangers or none, how many, a list (the day not said again within
  one day), the newest one's picture; sightings never checked for your face are neither, and said so;
- without your face known, asked about you or strangers, it says how to teach it;
- a picture is only ever a sighting folder's own best.jpg;
- through commands (shown on the panel) and the phone (its picture by folder), when they can be imported.
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

home = sandbox.activate()

for _name in ("win32api", "win32con", "win32gui", "win32process", "win32event", "pythoncom", "sounddevice",
              "soundfile", "edge_tts", "pywebpush"):
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = MagicMock()

from actions import files, sighting_search, sightings  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


# Thursday 22 October 2026, 20:10.
NOW = datetime.fromisoformat("2026-10-22T20:10:00+01:00")

# ---- times ---------------------------------------------------------------------------------------------------


def span(words):
    start, end, spoken = sighting_search.when(words, NOW)
    return (start and start.strftime("%a %d %H:%M"), end and end.strftime("%a %d %H:%M"), spoken)


check(span("today") == ("Thu 22 00:00", "Thu 22 20:10", "today"), f"today ({span('today')})")
check(span("yesterday") == ("Wed 21 00:00", "Thu 22 00:00", "yesterday"), "yesterday")
check(span("last night") == ("Wed 21 18:00", "Thu 22 06:00", "last night"), "last night: 6 pm to 6 am")
check(span("this morning") == ("Thu 22 05:00", "Thu 22 12:00", "this morning"), "this morning")
check(span("this week") == ("Mon 19 00:00", "Thu 22 20:10", "this week"), "this week: from Monday")
check(span("last week") == ("Mon 12 00:00", "Mon 19 00:00", "last week"), "last week")
check(span("on monday") == ("Mon 19 00:00", "Tue 20 00:00", "on Monday"), "on Monday: the last one")
check(span("on thursday")[0] == "Thu 15 00:00", "on Thursday, asked on a Thursday: a week ago")
check(span("in the last 3 hours") == ("Thu 22 17:10", "Thu 22 20:10", "in the last 3 hours")
      and span("in the last hour")[2] == "in the last hour" and span("in the past twenty minutes")[0] == "Thu 22 19:50",
      "the last hours and minutes, in figures or words")
check(span("ever") == (None, None, ""), "no time: all of them")

# ---- what is asked -------------------------------------------------------------------------------------------

ASKED = {
    "when was I last on the house cam": ("you", "last"),
    "when did you last see me": ("you", "last"),
    "show me when I was last on the house cam": ("you", "last"),
    "how many times was I seen today": ("you", "count"),
    "show me sightings of me this week": ("you", "list"),
    "any strangers this week": ("others", "list"),
    "any intruders in the last 3 hours": ("others", "list"),
    "when did a stranger last come": ("others", "last"),
    "show me the last stranger": ("others", "last"),
    "has anyone else been on the house cam today": ("others", "list"),
    "show me sightings from last night": ("any", "list"),
    "show me sightings": ("any", "list"),
    "was anyone on the house cam yesterday": ("any", "list"),
    "Jarvis, who was on the house cam on Monday?": ("any", "list"),
}

for text, (who, mode) in ASKED.items():
    found = sighting_search.asked(text)
    check(found and (found["who"], found["mode"]) == (who, mode), f"{text!r}: {who}, {mode} ({found and found['who']},"
                                                                     f" {found and found['mode']})")

for text in ("is anyone on the house cam", "show me the house cam", "what has the house cam detected",
             "what's the news last night", "who is on the house cam", "show me my cameras", "learn my face",
             "what did I do last night"):
    check(sighting_search.asked(text) is None, f"left alone: {text!r}")

check(sightings.asked("show me the last sighting") == "show" and sightings.asked("when was I last on the house cam")
      == "search" and sightings.asked("keep the last sighting") == "keep",
      "the last sighting's own words come first; a question about them is a search")

# ---- answers ---------------------------------------------------------------------------------------------------

base = Path(files.root()) / sightings.FOLDER
base.mkdir(parents=True, exist_ok=True)


def sighting(name, began, seconds, who="unchecked", best=True):
    path = base / name
    path.mkdir()
    record = {"camera": "house cam", "began": began, "seconds": seconds, "frames": []}

    if who != "unchecked":
        record["recognised"] = {"name": "you", "pictures": 9, "alike": 0.8} if who == "you" else None

    (path / sightings.RECORD_FILE).write_text(json.dumps(record), encoding="utf-8")

    if best:
        (path / "best.jpg").write_bytes(b"\xff\xd8 a picture")

    return path


sighting("house-cam-2026-10-21-224107", "2026-10-21T22:41:07+01:00", 29.8, "stranger")
sighting("house-cam-2026-10-21-230019", "2026-10-21T23:00:19+01:00", 29.8, "unchecked")
sighting("house-cam-2026-10-22-151102", "2026-10-22T15:11:02+01:00", 10.1, "stranger", best=False)
sighting("house-cam-2026-10-22-190428", "2026-10-22T19:04:29+01:00", 30.0, "you")
newest_you = sighting("house-cam-2026-10-22-195552", "2026-10-22T19:55:52+01:00", 26.0, "you")


def answer(text):
    return sighting_search.answer(text, now=NOW)


real_known = sighting_search.known_faces.anyone_known
sighting_search.known_faces.anyone_known = lambda: False
said, picture, _ = answer("any strangers this week")
check(said.startswith("I don't know your face yet") and "learn my face" in said and picture is None,
      "your face not known: asked about strangers, it says how to teach it")
said, _, _ = answer("show me sightings from last night")
check(said == "2 sightings last night, sir: 23:00; 22:41.", f"and a list is times only ({said!r})")
sighting_search.known_faces.anyone_known = lambda: True

try:
    said, picture, title = answer("when was I last on the house cam")
    check(said == "You were last on your house cam at 19:55, for 26 seconds, sir."
          and picture == str(newest_you / "best.jpg") and title == "SIGHTING HOUSE CAM",
          f"when you were last seen, with its picture ({said!r})")
    said, picture, _ = answer("any strangers this week")
    check(said.startswith("2 sightings of anyone not recognised as you this week, sir: 15:11, someone; "
                          "22:41 yesterday, someone.") and "1 sighting hasn't been checked for your face yet" in said
          and picture is None,
          f"strangers: not recognised as you, and the one never checked said apart; no picture without one ({said!r})")
    said, _, _ = answer("show me sightings from last night")
    check(said == "2 sightings last night, sir: 23:00, someone (not checked yet); 22:41, someone.",
          f"a list within one day: times, and who ({said!r})")
    said, _, _ = answer("how many times was I seen today")
    check(said == "You were seen twice today, sir.", f"how many ({said!r})")
    said, _, _ = answer("when did a stranger last come")
    check(said.startswith("Someone not recognised as you was last on your house cam at 15:11, for 10 seconds, sir."),
          f"the last stranger ({said!r})")
    said, picture, _ = answer("any intruders in the last hour")
    check(said == "I've seen no one but you in the last hour, sir." and picture is None, f"none ({said!r})")
    said, _, _ = answer("who was on the house cam on Monday")
    check(said == "I've seen no one on Monday, sir.", f"no one at all ({said!r})")

    for number in range(7):
        sighting(f"house-cam-2026-10-22-0{number}0000", f"2026-10-22T0{number}:00:00+01:00", 5, "stranger")
    said, _, _ = answer("any strangers today")
    check(said.startswith("8 sightings") and said.endswith(", and 3 more."), f"a long list cut short ({said!r})")
finally:
    sighting_search.known_faces.anyone_known = real_known

check(sighting_search.picture_of("house-cam-2026-10-22-195552") == str(newest_you / "best.jpg")
      and sighting_search.picture_of("../faces") is None and sighting_search.picture_of("house-cam-2026-10-22-151102")
      is None and sighting_search.picture_of(None) is None,
      "a picture is only a sighting folder's own best.jpg, named by the folder alone")

# ---- through commands and the phone ---------------------------------------------------------------------------

sighting_search.known_faces.anyone_known = lambda: True

try:
    try:
        import commands
    except Exception as error:
        print(f"SKIP commands (could not import commands: {error})")
        commands = None

    if commands is not None:
        shown = []
        commands.set_camera_listener(lambda image, title: shown.append((image, title)))
        result = commands.handle_command("when was I last on the house cam")
        said = result["action"]()
        check(result["intent"] == "camera_sighting" and said.startswith("You were last on your house cam at 19:55")
              and shown and shown[-1] == (b"\xff\xd8 a picture", "SIGHTING HOUSE CAM"),
              f"through commands: said, and its picture on the panel ({said!r})")

    try:
        import phone
    except Exception as error:
        print(f"SKIP the phone (could not import phone: {error})")
        phone = None

    if phone is not None:
        server = phone.PhoneServer(token="search-test")
        client = server._app.test_client()
        reply = server._camera_or_grid("when was I last on the house cam")
        check(reply.get("sighting") == {"title": "SIGHTING HOUSE CAM",
                                        "url": "/sighting/picture?name=house-cam-2026-10-22-195552"},
              f"on the phone: its picture, by its folder ({reply.get('sighting')})")
        got = client.get("/sighting/picture?name=house-cam-2026-10-22-195552&t=search-test")
        check(got.status_code == 200 and got.data == b"\xff\xd8 a picture", "served with the token")
        check(client.get("/sighting/picture?name=..%2Ffaces&t=search-test").status_code == 404
              and client.get("/sighting/picture?name=house-cam-2026-10-22-195552").status_code == 403,
              "nothing outside a sighting's folder, and nothing without the token")
        check('sighting.url.includes("?")' in phone.PAGE, "the page adds its token after a folder's name")
finally:
    sighting_search.known_faces.anyone_known = real_known

shutil.rmtree(base, ignore_errors=True)
failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
