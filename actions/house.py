"""The house hologram: a floor plan of where the sensors are, beside the HUD.

"Show me the house" projects your house as a 3D plan, floors stacked, each
room tinted by its temperature, a pin where each board sits, a ripple
where one has just seen movement, and a cone where a camera looks out.
Nothing here fetches anything: the readings are sensors.known(), and the
plan is house.json in the JARVIS folder, which is yours to edit.

house.json holds one or more places, so the boards can travel:

    {
      "active": "home",
      "places": {
        "home": {
          "aliases": ["my house"],
          "rooms": [
            {"name": "my room", "aliases": ["bedroom"], "floor": 1,
             "x": 0, "y": 0, "w": 3.6, "h": 3.2,
             "boards": [{"name": "room", "at": [0.8, 0.3]}],
             "features": [{"wall": "north", "at": 0.5, "width": 1.4, "kind": "window"},
                          {"wall": "east", "at": 0.75, "width": 0.9, "kind": "door"}],
             "cameras": [{"name": "cam", "wall": "north", "at": 0.5, "looks": "street"}]}
          ]
        },
        "mums house": {"aliases": ["mum's house", "mums"], "rooms": [...]}
      }
    }

Floors count up from 0; "floor_names" (for example {"0": "1st floor"})
labels them as the house is spoken of. Sizes are metres, x east and y
south from the north-west corner; "at" on
a board is where in the room it sits (0 to 1 across, 0 to 1 down), and on
a wall feature or a camera how far along that wall. Boards report by
name, so a board carried to another place is placed by that place's
plan: "show me mum's house" shows it and makes it the place in use,
remembered in .jarvis-house.json. A board named after a room (SENSOR_NAME
"kitchen") is in that room without being listed at all, and a camera named
after one ("my room cam") looks out through that room's window; a window
can say what it looks at ({"kind": "window", "looks": "street"}).

Each space has a "kind": "room" (the default), "kitchen", "landing", or
"passage" for a corridor or stairs, drawn faintly so the plan hangs
together but never counted, listed or named. There is no bathroom kind:
a bathroom is left off the plan altogether.

Said while nothing else claims it, and never a model call:

    show me the house / show me mum's house / close the house
    show me my room / what's happening in the kitchen / how's the landing
    switch the house to mum's house
"""

import copy
import json
import os
import re
import threading
import time

import phrases
from actions import files, sensors


CONFIG_NAME = "house.json"
STATE_NAME = ".jarvis-house.json"

# As the sensor panel counts them: a board reports every 30 seconds, past
# 75 it has missed some, past 120 it is gone.
STALE_SECONDS = 75.0
GONE_SECONDS = 120.0

# A plan to begin from, written the first time the house is asked for, so
# there is something to see and a file to change. Only "my room" is known;
# the rest are a guess to be measured and renamed. No bathroom: it is not a
# place for a sensor or a map, and a house.json never needs one.
STARTER = {
    "active": "home",
    "places": {
        "home": {
            "aliases": ["my house", "the house", "home"],
            "rooms": [
                {"name": "my room", "aliases": ["bedroom", "room"], "floor": 1,
                 "x": 0, "y": 0, "w": 3.6, "h": 3.4,
                 "boards": [{"name": "room", "at": [0.82, 0.3]}],
                 "features": [{"wall": "north", "at": 0.5, "width": 1.5, "kind": "window"},
                              {"wall": "east", "at": 0.8, "width": 0.9, "kind": "door"}],
                 "cameras": [{"name": "cam", "wall": "north", "at": 0.5, "looks": "street"}]},
                {"name": "landing", "kind": "landing", "floor": 1, "x": 3.6, "y": 0, "w": 1.6, "h": 6.8,
                 "features": [{"wall": "south", "at": 0.5, "width": 1.0, "kind": "door"}]},
                {"name": "front room", "aliases": ["living room", "lounge"], "floor": 0,
                 "x": 0, "y": 0, "w": 3.6, "h": 4.0,
                 "features": [{"wall": "north", "at": 0.5, "width": 1.8, "kind": "window"},
                              {"wall": "east", "at": 0.6, "width": 0.9, "kind": "door"}]},
                {"name": "hall", "aliases": ["hallway"], "kind": "passage", "floor": 0,
                 "x": 3.6, "y": 0, "w": 1.6, "h": 6.8,
                 "features": [{"wall": "north", "at": 0.5, "width": 0.9, "kind": "door"}]},
                {"name": "kitchen", "kind": "kitchen", "floor": 0, "x": 0, "y": 4.0, "w": 3.6, "h": 2.8,
                 "features": [{"wall": "east", "at": 0.4, "width": 0.9, "kind": "door"},
                              {"wall": "south", "at": 0.5, "width": 1.2, "kind": "window"}]},
            ],
        },
    },
}

_WALLS = ("north", "east", "south", "west")

# What a space on the plan is. Rooms, the kitchen and a landing are counted,
# listed and named; a passage (a corridor, a flight of stairs) is drawn,
# faintly, so the plan hangs together, and is never counted or listed.
KINDS = ("room", "kitchen", "landing", "passage")
_COUNTED = ("room", "kitchen", "landing")

_lock = threading.Lock()
_view = None                # {"place": key, "focus": room name or None} while showing
_show_listener = None
_hide_listener = None


def set_listeners(on_view=None, on_hide=None):
    """Who draws it: on_view(view) with each change, on_hide() when it goes."""
    global _show_listener, _hide_listener
    _show_listener, _hide_listener = on_view, on_hide


def showing():
    with _lock:
        return _view is not None


# ---- house.json ------------------------------------------------------------------------

def _path(name):
    root = files.root()
    return os.path.join(root, name) if root else None


def _load(name):
    path = _path(name)

    if not path or not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"[JARVIS] could not read {name}: {error}")
        return None

    return data if isinstance(data, dict) else None


def _exists(name):
    path = _path(name)
    return bool(path and os.path.exists(path))


def config(create=False):
    """house.json, or the starter plan while there is none; with [create], written out.

    A house.json that is there but cannot be read (a stray comma) gives
    nothing, never the starter: yours is not written over or hidden.
    """
    data = _load(CONFIG_NAME)

    if data is not None:
        return data

    if _exists(CONFIG_NAME):
        return {}

    if create:
        path = _path(CONFIG_NAME)

        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(STARTER, handle, indent=2)
            print(f"[JARVIS] wrote a starter {CONFIG_NAME}; change it to match your house")
        except (OSError, TypeError) as error:
            print(f"[JARVIS] could not write {CONFIG_NAME}: {error}")

    return copy.deepcopy(STARTER)


def _key(text):
    """A name as compared: lower case, no apostrophes, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold().replace("'", "")).split())


def places(data=None):
    """{key: place} from house.json, each with a list of rooms."""
    data = config() if data is None else data
    listed = data.get("places")

    if not isinstance(listed, dict):
        return {}

    return {_key(key): value for key, value in listed.items()
            if _key(key) and isinstance(value, dict) and isinstance(value.get("rooms"), list)}


def _place_names(key, place):
    names = {key}

    for alias in place.get("aliases") or []:
        if _key(alias):
            names.add(_key(alias))

    return names


def active_place(data=None):
    """The place in use: the last one shown, else house.json's "active", else the first."""
    data = config() if data is None else data
    known = places(data)

    if not known:
        return None

    state = _load(STATE_NAME) or {}

    for wanted in (state.get("active"), data.get("active")):
        if _key(wanted) in known:
            return _key(wanted)

    return next(iter(known))


def _remember(key):
    path = _path(STATE_NAME)

    if not path:
        return

    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"active": key}, handle, indent=2)
    except OSError as error:
        print(f"[JARVIS] could not save {STATE_NAME}: {error}")


def _number(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _rooms(place):
    """The rooms of a place, checked: a room with no size is left out, not guessed."""
    rooms = []

    for raw in place.get("rooms") or []:
        if not isinstance(raw, dict) or not _key(raw.get("name")):
            continue

        width, depth = _number(raw.get("w")), _number(raw.get("h"))

        if width <= 0 or depth <= 0:
            print(f"[JARVIS] house.json: {raw.get('name')} has no size (w and h, in metres); left off the plan")
            continue

        boards = []

        for board in raw.get("boards") or []:
            name = board.get("name") if isinstance(board, dict) else board
            at = board.get("at") if isinstance(board, dict) else None
            at = at if isinstance(at, list) and len(at) == 2 else [0.5, 0.5]

            if _key(name):
                boards.append({"name": str(name).strip().casefold(),
                               "at": [min(1.0, max(0.0, _number(at[0], 0.5))),
                                      min(1.0, max(0.0, _number(at[1], 0.5)))]})

        features = [
            {"wall": feature["wall"], "at": min(1.0, max(0.0, _number(feature.get("at"), 0.5))),
             "width": max(0.2, _number(feature.get("width"), 0.9)),
             "kind": "window" if feature.get("kind") == "window" else "door",
             "looks": str(feature.get("looks") or "").strip()}
            for feature in raw.get("features") or []
            if isinstance(feature, dict) and feature.get("wall") in _WALLS
        ]

        cameras = [
            {"name": str(camera["name"]).strip().casefold(), "wall": camera.get("wall"),
             "at": min(1.0, max(0.0, _number(camera.get("at"), 0.5))),
             "looks": str(camera.get("looks") or "").strip()}
            for camera in raw.get("cameras") or []
            if isinstance(camera, dict) and _key(camera.get("name")) and camera.get("wall") in _WALLS
        ]

        kind = str(raw.get("kind") or "room").strip().casefold()

        rooms.append({
            "kind": kind if kind in KINDS else "room",
            "name": str(raw["name"]).strip(),
            "aliases": [str(alias) for alias in raw.get("aliases") or [] if _key(alias)],
            "floor": int(_number(raw.get("floor"), 0)),
            "x": _number(raw.get("x")), "y": _number(raw.get("y")), "w": width, "h": depth,
            "boards": boards, "features": features, "cameras": cameras,
        })

    return rooms


def _room_names(room):
    return {_key(room["name"])} | {_key(alias) for alias in room["aliases"]}


# ---- what is drawn ------------------------------------------------------------------------

def _state(board, now_seen):
    if board is None:
        return "waiting"

    if now_seen >= GONE_SECONDS:
        return "offline"

    return "quiet" if now_seen >= STALE_SECONDS else "live"


def render(place_key=None, focus=None):
    """The view the panel draws, for [place_key] (the place in use by default)."""
    data = config()
    known_places = places(data)
    key = place_key if place_key in known_places else active_place(data)

    if key is None:
        return None

    place = known_places[key]
    rooms = _rooms(place)
    heard = sensors.known()
    now = time.monotonic()
    placed = set()
    boards = {}

    for room in rooms:
        for board in room["boards"] + [{"name": camera["name"], "at": None} for camera in room["cameras"]]:
            name = board["name"]
            placed.add(name)
            report = heard.get(name)

            boards[name] = {
                "room": room["name"],
                "camera": board["at"] is None,
                "state": _state(report, report["seen_ago"] if report else 0.0),
                "seen_at": now - report["seen_ago"] if report else None,
                "moved_at": now - report["moved_ago"] if report and report["moved_ago"] is not None else None,
                "readings": dict(report["readings"]) if report else {},
            }

    # A board named after a room ("kitchen", SENSOR_NAME in its sketch) is in
    # that room, by its far wall, with nothing to add to house.json. A camera
    # named after one ("my room cam") looks out through that room's window.
    spaces = [room for room in rooms if room["kind"] != "passage"]

    for name, report in heard.items():
        if name in placed:
            continue

        camera_of = _camera_room(name, spaces)
        room = camera_of or _find_room(name, spaces)

        if room is None:
            continue

        if camera_of:
            room["cameras"].append(_camera_through_window(name, room))
        else:
            room["boards"].append({"name": name, "at": [0.78, 0.28]})

        placed.add(name)
        boards[name] = {
            "room": room["name"],
            "camera": bool(camera_of),
            "state": _state(report, report["seen_ago"]),
            "seen_at": now - report["seen_ago"],
            "moved_at": now - report["moved_ago"] if report["moved_ago"] is not None else None,
            "readings": dict(report["readings"]),
        }

    unplaced = sorted(name for name in heard if name not in placed)

    return {
        "place": key,
        "title": _title(key),
        "rooms": rooms,
        "boards": boards,
        "unplaced": [{"name": name, "state": _state(heard[name], heard[name]["seen_ago"]),
                      "readings": dict(heard[name]["readings"])} for name in unplaced],
        "focus": focus,
        "floors": sorted({room["floor"] for room in rooms}),
        "floor_names": _floor_names(place),
        "counts": {kind: sum(1 for room in rooms if room["kind"] == kind) for kind in _COUNTED},
    }


_CAMERA_WORDS = ("cam", "camera", "webcam")


def _camera_room(name, rooms):
    """The room a camera is named after ("my room cam"), or None for a board that is not a camera."""
    words = _key(name).split()

    if len(words) < 2 or words[-1] not in _CAMERA_WORDS:
        return None

    return _find_room(" ".join(words[:-1]), rooms)


def _camera_through_window(name, room):
    """A camera looking out of the room: through the window that says what it sees, else any window."""
    windows = [feature for feature in room["features"] if feature["kind"] == "window"]
    window = next((feature for feature in windows if feature["looks"]), windows[0] if windows else None)

    if window is None:
        # No window on the plan: looking out through the north wall, as
        # good a guess as any and plain to see on the map to be put right.
        return {"name": name, "wall": "north", "at": 0.5, "looks": ""}

    return {"name": name, "wall": window["wall"], "at": window["at"], "looks": window["looks"]}


def room_of(board_name):
    """The house plan's name for the room a board is in, or None. For sensors.set_namer."""
    drawn = render()

    if not drawn:
        return None

    board = drawn["boards"].get(str(board_name or "").strip().casefold())
    return board["room"] if board else None


def _to_you(name):
    """How JARVIS says a room to you: the plan's "my room" is "your room"."""
    return "your " + name[3:] if name.casefold().startswith("my ") else name


def _said(name):
    """A room as a sentence names it: "your room", "room 4", "the kitchen"."""
    named = _to_you(name)
    lowered = named.casefold()

    if lowered.startswith(("your ", "the ")) or re.search(r"\d", lowered) or re.match(r"^room\s+\S+$", lowered):
        return named

    return "the " + named


# ---- sensor questions about the plan's rooms -----------------------------------------------

def question(text):
    """A sensor question naming a room on the plan, or None: (what, place key, room name).

    "is anyone in the kitchen", "how warm is my bedroom": asked the way
    sensors.question asks, about a room the plan knows by name or alias,
    whether or not a board in it has reported, so a room with no sensor
    says so rather than going to the model.
    """
    # Only with a plan of your own: the starter is there to be shown and
    # changed, not to answer for rooms you may not have.
    if not _exists(CONFIG_NAME):
        return None

    words = _strip_polite(_words(text))

    if not words or words[0] not in sensors._QUESTION_STARTS:
        return None

    asked = [what for what, cues in sensors._ASKING.items() if what != "everything" and any(w in cues for w in words)]

    if not asked:
        return None

    key = active_place()

    with _lock:
        if _view is not None:
            key = _view["place"]

    known = places()

    if key not in known:
        return None

    said = " " + " ".join(words) + " "
    best = None

    for room in _rooms(known[key]):
        if room["kind"] == "passage":
            continue

        for name in _room_names(room):
            if f" {name} " in said and (best is None or len(name) > len(best[1])):
                best = (room["name"], name)

    if best is None:
        return None

    for what in ("presence", "humidity", "temperature"):
        if what in asked:
            return what, key, best[0]

    return None


def answer_question(text):
    """What the room's boards say to question(text), or that it has no sensor. None if not one."""
    asked = question(text)

    if asked is None:
        return None

    what, key, room_name = asked
    drawn = render(key, room_name)
    room = next((room for room in drawn["rooms"] if room["name"] == room_name), None) if drawn else None

    if room is None:
        return None

    follow([board["name"] for board in room["boards"]])
    names = [board["name"] for board in room["boards"]]

    if not names:
        return f"{_first_up(_said(room_name))} has no sensor yet, sir."

    return sensors.answer((what, names)) or f"No reading from {_said(room_name)} yet, sir."


def spoken_spaces(counts):
    """"seven rooms, a kitchen and a landing": what a house has, as it is said."""
    parts = []

    if counts.get("room"):
        parts.append(_spoken_count(counts["room"], "room"))

    for kind in ("kitchen", "landing"):
        if counts.get(kind) == 1:
            parts.append(f"a {kind}")
        elif counts.get(kind):
            parts.append(_spoken_count(counts[kind], kind))

    if not parts:
        return "no rooms"

    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _floor_names(place):
    """{floor: "3RD FLOOR"} from a place's own "floor_names", as its house calls them."""
    named = place.get("floor_names")

    if not isinstance(named, dict):
        return {}

    names = {}

    for key, value in named.items():
        try:
            names[int(key)] = str(value).strip().upper()
        except (TypeError, ValueError):
            continue

    return {floor: name for floor, name in names.items() if name}


def _title(key):
    return key.upper()


def _publish():
    with _lock:
        view = dict(_view) if _view else None

    if view is None or not _show_listener:
        return

    drawn = render(view["place"], view.get("focus"))

    if drawn is not None:
        try:
            _show_listener(drawn)
        except Exception as error:
            print(f"[JARVIS] could not show the house: {error}")


def refresh(_known=None):
    """A board reported: redraw, if the house is showing. For sensors.add_listener."""
    if showing():
        _publish()


# ---- what was said --------------------------------------------------------------------------

def _words(text):
    return _key(text).split()


_POLITE = ("jarvis", "please", "ok", "okay", "hey", "so", "and", "can", "you", "could", "would")

_HOUSE_WORDS = ("house", "home", "floor plan", "floorplan", "house hologram", "map of the house", "house map",
                "the house hologram")

_SHOW_VERBS = ("show", "display", "bring up", "pull up", "open", "project")
_CLOSE_VERBS = ("close", "closed", "closes", "hide", "put away", "dismiss", "clear")

# Asking after a room: the verb, then the room and nothing else, so "tell me
# about the kitchen sink" is not the kitchen.
_ROOM_ASKS = ("show me", "show", "zoom in on", "zoom into", "zoom in to", "zoom to", "focus on", "go to",
              "whats happening in", "what is happening in", "whats going on in", "what is going on in",
              "hows", "how is", "how are things in", "tell me about", "check on", "check", "look at",
              "whats up in", "anything happening in", "is anything happening in")


def _strip_polite(words):
    while words and words[0] in _POLITE:
        words = words[1:]

    while words and words[-1] in ("please", "jarvis", "now"):
        words = words[:-1]

    return words


def _after(said, starts):
    """The rest of [said] after the longest of [starts] it begins with, or None."""
    for start in sorted(starts, key=len, reverse=True):
        if said == start:
            return ""

        if said.startswith(start + " "):
            return said[len(start) + 1:]

    return None


def _strip_the(rest):
    for lead in ("me ", "the ", "my ", "our ", "your "):
        while rest.startswith(lead):
            rest = rest[len(lead):]

    return rest


def _find_place(spoken, known):
    spoken = _key(spoken)

    for key, place in known.items():
        if spoken in _place_names(key, place):
            return key

    return None


def _find_room(spoken, rooms):
    spoken = _key(spoken)

    for room in rooms:
        if spoken in _room_names(room) or ("my " + spoken) in _room_names(room):
            return room

    return None


def asked(text):
    """What a command asks of the house hologram, or None.

    ("show", place key or None), ("close", None), ("switch", place key),
    ("room", (place key, room name)). Rooms and places are house.json's own,
    so a new room is askable once it is in the file.
    """
    words = _strip_polite(_words(text))
    said = " ".join(words)

    if not said:
        return None

    data = config()
    known = places(data)
    is_showing = showing()

    # "close the house", "closed the house" (as speech recognition writes it).
    rest = _after(said, _CLOSE_VERBS)

    if rest is not None:
        thing = _strip_the(rest)

        if thing in _HOUSE_WORDS or thing == "house hologram":
            return "close", None

        if is_showing and thing in ("", "it", "that", "the map", "map", "plan", "floor plan"):
            return "close", None

    # "switch the house to mum's house", "use mum's house".
    rest = _after(said, ("switch", "change", "set", "move"))

    if rest is not None:
        match = re.match(r"^(?:the\s+)?(?:house|home|place)\s+to\s+(.+)$", rest)

        if match:
            key = _find_place(_strip_the(match.group(1)), known)
            return ("switch", key) if key else None

    # "show me the house", "show me mum's house".
    rest = _after(said, _SHOW_VERBS)

    if rest is not None:
        thing = _strip_the(rest)

        if thing in _HOUSE_WORDS:
            return "show", None

        key = _find_place(thing, known)

        if key:
            return "show", key

        # "show me the house hologram", "show me the plan of mum's house".
        for tail in (" hologram", " floor plan", " map", " plan"):
            if thing.endswith(tail):
                key = _find_place(thing[: -len(tail)], known)

                if key:
                    return "show", key

    # A room: "show me my room", "what's happening in the kitchen". Rooms of
    # the place in view, or in use when nothing is showing.
    with _lock:
        in_view = _view["place"] if _view else None

    key = in_view or active_place(data)

    if key is None:
        return None

    rest = _after(said, _ROOM_ASKS)

    if rest is None:
        return None

    room = _find_room(_strip_the(rest), _rooms(known[key]))

    if room is None:
        return None

    return "room", (key, room["name"])


# ---- doing it ----------------------------------------------------------------------------------

def _spoken_count(count, one, many=None):
    if count == 0:
        return f"no {many or one + 's'}"

    return f"{phrases.number(count)} {one if count == 1 else (many or one + 's')}"


def _live(boards):
    return sum(1 for board in boards.values() if board["state"] in ("live", "quiet"))


def show(place_key=None, focus=None):
    """Project a place (the place in use by default). Returns what to say."""
    global _view

    data = config(create=True)
    known = places(data)

    if not known:
        if _exists(CONFIG_NAME) and _load(CONFIG_NAME) is None:
            return f"I can't read {CONFIG_NAME}, sir; there may be a typo in it."

        return f"There's no house in {CONFIG_NAME} yet, sir."

    key = place_key if place_key in known else active_place(data)
    _remember(key)

    with _lock:
        _view = {"place": key, "focus": focus}

    _publish()

    drawn = render(key, focus)
    boards = [board for board in drawn["boards"].values() if not board["camera"]]
    online = sum(1 for board in boards if board["state"] in ("live", "quiet"))

    name = "your house" if key == "home" else _spoken_place(key)
    floors = len(drawn["floors"])
    said = (f"{_first_up(name)}, sir: {spoken_spaces(drawn['counts'])}"
            + (f" over {_spoken_count(floors, 'floor')}" if floors > 1 else "")
            + f", {_spoken_count(online, 'sensor')} online.")

    if drawn["unplaced"]:
        names = ", ".join(item["name"] for item in drawn["unplaced"])
        said += f" The {names} sensor{'s are' if len(drawn['unplaced']) > 1 else ' is'} not on the plan yet; " \
                f"name {'them' if len(drawn['unplaced']) > 1 else 'it'} after a room, or add " \
                f"{'them' if len(drawn['unplaced']) > 1 else 'it'} to {CONFIG_NAME}."

    return said


def _spoken_place(key):
    return key.replace("mums", "mum's").replace("dads", "dad's")


def _first_up(text):
    return text[:1].upper() + text[1:]


def close():
    global _view

    with _lock:
        was = _view is not None
        _view = None

    if _hide_listener:
        try:
            _hide_listener()
        except Exception as error:
            print(f"[JARVIS] could not hide the house: {error}")

    return "House closed, sir." if was else "The house isn't showing, sir."


def switch(place_key):
    """Make [place_key] the place in use. Returns what to say."""
    known = places()

    if place_key not in known:
        return "I don't know that place, sir."

    _remember(place_key)

    if showing():
        show(place_key)

    return f"The sensors now belong to {_spoken_place(place_key)}, sir."


def _ago(seconds):
    minutes = int(seconds // 60)

    if minutes < 1:
        return "just now"

    if minutes < 60:
        return f"{_spoken_count(minutes, 'minute')} ago"

    return f"{_spoken_count(minutes // 60, 'hour')} ago"


def describe_room(place_key, room_name):
    """What a room's boards say, as a sentence. The room comes into focus if showing."""
    drawn = render(place_key, room_name)

    if drawn is None:
        return None

    room = next((room for room in drawn["rooms"] if room["name"] == room_name), None)

    if room is None:
        return None

    title = _first_up(_said(room["name"]))
    boards = [drawn["boards"][board["name"]] for board in room["boards"] if board["name"] in drawn["boards"]]
    cameras = [(camera, drawn["boards"].get(camera["name"])) for camera in room["cameras"]]
    now = time.monotonic()
    parts = []

    for board in boards:
        readings = board["readings"]

        if board["state"] == "waiting":
            parts.append("its sensor hasn't reported yet")
            continue

        if "temperature" in readings:
            parts.append(f"{readings['temperature']:.1f} degrees")

        if "humidity" in readings:
            parts.append(f"{readings['humidity']:.0f} percent humidity")

        if board["moved_at"] is not None:
            moved = now - board["moved_at"]

            if moved < sensors.PRESENT_SECONDS:
                parts.append(f"someone there, movement {_ago(moved)}")
            else:
                parts.append(f"quiet, no movement for {_ago(moved).replace(' ago', '')}")

        if board["state"] == "offline":
            parts.append(f"though its sensor last reported {_ago(now - board['seen_at'])}")

    for camera, board in cameras:
        called = f"the {camera['looks']} camera" if camera["looks"] else "its camera"

        if board and board["state"] in ("live", "quiet"):
            parts.append(f"{called} is online")
        else:
            parts.append(f"{called} isn't connected yet")

    if not parts:
        said = f"{title} has no sensor yet, sir."
    else:
        said = f"{title}: " + ", ".join(parts) + ", sir."

    return said


def focus(place_key, room_name):
    """Bring a room into focus, showing the house if it is not up. Returns what to say."""
    with _lock:
        up = _view is not None and _view["place"] == place_key

    if up:
        with _lock:
            _view["focus"] = room_name
        _publish()
    else:
        show(place_key, focus=room_name)

    return describe_room(place_key, room_name)


def tapped(room_name):
    """A room tapped on the hologram: into focus, quietly; tapped again, out of it."""
    with _lock:
        if _view is None:
            return None

        _view["focus"] = None if _view.get("focus") == room_name else room_name

    _publish()
    return None


def follow(board_names):
    """A sensor question was answered: light the room it was about, if the house is up."""
    with _lock:
        if _view is None:
            return

        key = _view["place"]

    drawn = render(key)
    rooms = drawn["rooms"] if drawn else []
    wanted = {str(name).casefold() for name in board_names or []}

    for room in rooms:
        if wanted & {board["name"] for board in room["boards"]}:
            with _lock:
                if _view is not None:
                    _view["focus"] = room["name"]
            _publish()
            return


def answer(text):
    """Do what [text] asks of the house hologram. Returns what to say, or None."""
    request = asked(text)

    if request is None:
        return None

    kind, value = request

    if kind == "show":
        return show(value)

    if kind == "close":
        return close()

    if kind == "switch":
        return switch(value)

    if kind == "room":
        return focus(*value)

    return None
