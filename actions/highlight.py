"""Boxes drawn over a camera's live view around what its board recognises: "highlight the bottle".

The house cam (flashed as s3detect) finds things on its own board and says where each is, 0 to 1
across and down the picture (actions/detections.py). While its live view is on screen it sends each
look's boxes down the same stream as the frames, so here they are drawn over the picture on the HUD
and on the phone, and follow what is there, look by look (about every 2.5 seconds):

    highlight the bottle / highlight bottles / outline the laptop and the keyboard
    highlight what you see / highlight everything / box them all
    highlight it                      (what JARVIS last said came into view)
    highlight someone                 (the model calls a person "person")
    stop highlighting / clear the highlights / highlights off

Things are named as the model names them: what a camera has reported is matched first, plurals
and all ("two bottles", "the cell phones"); a thing not in view yet is still highlighted when it
comes. The view is opened if it is not showing; closing it ends the highlighting. Free: no model
call, nothing stored but the boxes in memory.
"""

import re
import threading

from actions import cameras, detections


_lock = threading.Lock()
_on = {}            # camera -> frozenset of labels, or None for everything it sees
_listener = None    # called with a camera's name when what to draw over it changes


# "highlight ..." -- what to highlight, then optionally on which camera.
_VERBS = r"(?:highlight|outline|box|circle|point\s+out)"
_ON = re.compile(rf"^(?:(?:jarvis|please|can\s+you|could\s+you|would\s+you|now)\s+)*{_VERBS}\s+(?P<what>.+?)"
                 r"(?:\s+(?:is|are))?(?:\s+please)?$")
_OFF = re.compile(r"^(?:(?:jarvis|please|now)\s+)*(?:(?:stop|cancel|end|clear|remove|hide|drop|turn\s+off|switch\s+off)"
                  r"\s+(?:the\s+|all\s+the\s+|those\s+|these\s+)?(?:highlighting|highlights?|boxes|outlines?)"
                  r"|unhighlight(?:\s+.+)?|no\s+more\s+(?:highlighting|highlights|boxes)"
                  r"|(?:highlights?|highlighting|boxes)\s+off)(?:\s+please)?$")

# What the whole view is called.
_EVERYTHING = re.compile(r"^(?:(?:what|whatever|everything|all|anything)\s+(?:that\s+)?(?:you|it|the\s+\w+(?:\s+\w+)?)"
                         r"\s+(?:can\s+)?(?:sees?|see|has\s+(?:seen|detected|found|spotted)|detects?|finds?|spots?"
                         r"|recognises?|recognizes?)|everything|all|all\s+of\s+(?:it|them)|them\s+all|the\s+lot"
                         r"|all\s+the\s+things|every\s*thing\s+(?:in\s+view|there)|what(?:s|\s+is)\s+there"
                         r"|what(?:s|\s+is)\s+in\s+view|things)$")

# What JARVIS last said came into view.
_LAST = ("it", "that", "them", "those", "these", "this", "that one", "this one")

# A person, as people say it; the model's own word is "person" (detections.arrival_words says it back
# as "someone").
_PERSON = ("someone", "somebody", "anyone", "anybody", "people", "persons", "person", "a person", "the person",
           "me", "him", "her", "the people")

_FILLER = ("the", "a", "an", "my", "your", "that", "those", "these", "this", "any", "all", "some")
_NUMBER_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def set_listener(listener):
    """listener(camera): what is drawn over [camera]'s view has changed (boxes(camera) has it)."""
    global _listener
    _listener = listener


def _tell(camera):
    listener = _listener

    if listener is not None:
        try:
            listener(camera)
        except Exception as error:   # noqa: BLE001 - a panel failing must not stop a report
            print(f"[JARVIS] could not draw the highlights: {error}")


def _words(text):
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold().replace("'", "")).split())


def _camera_named(words):
    """(camera, words without it) when [words] ends "on/in the <camera>", else (None, words)."""
    known = cameras.cameras()

    for camera in sorted(known, key=lambda camera: -len(camera["name"])):
        for target in sorted(cameras._targets(camera, len(known) == 1), key=len, reverse=True):
            match = re.search(rf"\s+(?:on|in|from|through)\s+{re.escape(target)}$", words)

            if match:
                return camera, words[:match.start()]

    return None, words


def asked(text):
    """What [text] asks to highlight: {"on": True, "what": labels | None | "last", "camera": camera or None,
    "known": bool}, {"on": False, "camera": camera or None}, or None when it asks nothing of the kind.
    labels is a tuple of the model's names; None is everything in view; "last" what was last said came
    into view. "known" is False when a name is not one any camera has reported -- "highlight the second
    story" is the news panel's, unless a camera is named or on screen (commands.py decides)."""
    words = _words(text)

    if not words:
        return None

    if _OFF.match(words):
        camera, _ = _camera_named(words)
        return {"on": False, "camera": camera}

    match = _ON.match(words)

    if not match:
        return None

    camera, what = _camera_named(match.group("what"))
    what = what.strip()

    if not what:
        return None

    if what in _LAST:
        return {"on": True, "what": "last", "camera": camera, "known": True}

    if _EVERYTHING.match(what):
        return {"on": True, "what": None, "camera": camera, "known": True}

    labels = _labels(what, camera)
    known = _known_labels(camera) | {"person"}
    return ({"on": True, "what": labels, "camera": camera, "known": all(label in known for label in labels)}
            if labels else None)


def _known_labels(camera):
    """The model's names [camera] (or any camera) has reported: what spoken things are matched against."""
    names = set()
    named = [camera["name"]] if camera else [entry["name"] for entry in cameras.cameras()]

    for name in named:
        for report in (detections.latest(name), detections.marks(name)):
            for item in ((report or {}).get("objects") or []) + ((report or {}).get("now") or []):
                names.add(item["label"])

    return names


def _plural(label):
    return detections._IRREGULAR.get(label) or (label + "es" if label.endswith(("s", "x", "ch", "sh"))
                                                 else label + "s")


def _labels(what, camera):
    """The model's names in [what] ("the bottle and the two laptops"), as a tuple; [] if none."""
    found = []
    rest = f" {what} "

    if any(re.search(rf"\b{re.escape(person)}\b", what) for person in _PERSON):
        found.append("person")

        for person in sorted(_PERSON, key=len, reverse=True):
            rest = re.sub(rf"\b{re.escape(person)}\b", " ", rest)

    # Names already reported first, longest first, so "cell phone" is not taken for "phone".
    for label in sorted(_known_labels(camera), key=len, reverse=True):
        for said in (_plural(label), label):
            pattern = rf"\b{re.escape(said)}\b"

            if re.search(pattern, rest):
                if label not in found:
                    found.append(label)

                rest = re.sub(pattern, " ", rest)

    # Whatever is left, a thing not reported yet: made singular, and highlighted when it comes.
    for part in re.split(r"\s+(?:and|or|plus)\s+|\s*,\s*", rest.strip()):
        kept = [word for word in part.split() if word not in _FILLER and word not in _NUMBER_WORDS
                and not word.isdigit()]

        if not kept:
            continue

        name = " ".join(kept)
        irregular = {plural: single for single, plural in detections._IRREGULAR.items()}

        if name in irregular:
            name = irregular[name]
        elif name.endswith(("ches", "shes", "sses", "xes")):
            name = name[:-2]
        elif name.endswith("s") and not name.endswith("ss"):
            name = name[:-1]

        if name and name not in found:
            found.append(name)

    return tuple(found)


def start(camera, what):
    """Highlight [what] (labels, None for everything) on [camera] ({"name", ...}) from now on."""
    name = cameras.normal_name(camera["name"])

    if what == "last":
        what = tuple(detections.last_said(name)) or None

    with _lock:
        _on[name] = frozenset(what) if what else None

    _tell(name)
    return said_on(camera, what)


def stop(camera=None):
    """Stop highlighting on [camera], or on every camera. Returns what to say."""
    with _lock:
        names = [cameras.normal_name(camera["name"])] if camera else list(_on)
        was = [name for name in names if name in _on]

        for name in was:
            del _on[name]

    for name in was:
        _tell(name)

    return "Highlights off, sir." if was else "Nothing's highlighted, sir."


def active_any():
    """Whether anything is being highlighted on any camera."""
    with _lock:
        return bool(_on)


def active(name):
    """Whether anything is being highlighted on camera [name]."""
    with _lock:
        return cameras.normal_name(name) in _on


def boxes(name):
    """[{"label", "score", "box"}] to draw over camera [name]'s view now: what is highlighted and in view,
    from its newest boxes, or [] (nothing asked, or the boxes are stale)."""
    name = cameras.normal_name(name)

    with _lock:
        if name not in _on:
            return []

        wanted = _on[name]

    kept = detections.marks(name)

    if kept is None or kept["ago"] > detections.MARKS_FRESH_SECONDS:
        return []

    return [dict(item) for item in kept["objects"] if wanted is None or item["label"] in wanted]


def said_on(camera, what):
    """What JARVIS says on starting: what is boxed now, and what is waited for."""
    name = cameras.normal_name(camera["name"])
    who = detections._yours(name)
    kept = detections.marks(name)
    present = kept["objects"] if kept and kept["ago"] <= detections.MARKS_FRESH_SECONDS else []
    counts = {}

    for item in present:
        if what is None or item["label"] in what:
            counts[item["label"]] = counts.get(item["label"], 0) + 1

    def spoken(label, count):
        if label == "person":
            return "someone" if count == 1 else f"{detections._NUMBERS.get(count, count)} people"

        return detections._counted(label, count)

    if what is None:
        if not counts:
            return f"Nothing's in view of {who} yet, sir. I'll box things as they come into view."

        return f"Highlighting what {who} sees, sir: {detections._listed([spoken(l, n) for l, n in counts.items()])}."

    missing = [label for label in what if label not in counts]
    said = []

    if counts:
        said.append(f"Highlighting {detections._listed([spoken(l, n) for l, n in counts.items()])}, sir.")

    if missing:
        things = detections._listed([spoken(label, 1) for label in missing])
        one = len(missing) == 1
        said.append(f"I can't see {things} on {who} right now{'' if counts else ', sir'}."
                    f" I'll box {'it' if one else 'them'} as soon as {'it shows' if one else 'they show'} up.")

    return " ".join(said)


def forget():
    """Drop every highlight. For tests."""
    with _lock:
        _on.clear()


def _marks_changed(camera):
    if active(camera):
        _tell(camera)


detections.set_marks_listener(_marks_changed)
