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
    save the highlight / save the highlights / save it   (each thing cut out, no background, as a PNG in
                                                          JARVIS's images folder)

Things are named as the model names them: what a camera has reported is matched first, plurals
and all ("two bottles", "the cell phones"); a thing not in view yet is still highlighted when it
comes. The view is opened if it is not showing; closing it ends the highlighting. Free: no model
call, nothing stored but the boxes in memory.

With the outline model set up (actions/outline.py), each thing's exact shape is cut out of the picture
on the PC after each look, and the line drawn hugs it; saving keeps the thing alone. Without it the
highlights are boxes, and saving keeps what is in the box.
"""

import os
import re
import threading
import time

from actions import cameras, detections, files, outline


_lock = threading.Lock()
_on = {}            # camera -> frozenset of labels, or None for everything it sees
_listener = None    # called with a camera's name when what to draw over it changes
_shapes = {}        # camera -> {"rgb": picture, "items": [{"label", "box", "outline", "mask"}], "at"}
_jobs = {}          # camera -> (jpeg, [items]): the newest look still to be outlined
_work = threading.Condition()
_worker = None

# The images folder in JARVIS's own folder, as pictures the PC's camera keeps (actions/camera.py).
IMAGES_FOLDER = "images"


# "highlight ..." -- what to highlight, then optionally on which camera.
_VERBS = r"(?:highlight|outline|box|circle|point\s+out)"
_ON = re.compile(rf"^(?:(?:jarvis|please|can\s+you|could\s+you|would\s+you|now)\s+)*{_VERBS}\s+(?P<what>.+?)"
                 r"(?:\s+(?:is|are))?(?:\s+please)?$")
_SAVE = re.compile(r"^(?:(?:jarvis|please|now|can\s+you|could\s+you)\s+)*(?:save|keep|store|capture|grab)\s+"
                   r"(?:(?:the|that|those|this|these|my|your)\s+)?(?P<what>highlights?|highlighted(?:\s+\w+)*"
                   r"|cut\s*outs?|cutouts?|outlines?|boxes|it|that|them|those)(?:\s+please)?$")
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

    saving = _SAVE.match(words)

    if saving:
        camera, _ = _camera_named(words)
        return {"save": True, "on": True, "what": "save", "camera": camera,
                "known": saving.group("what") not in ("it", "that", "them", "those")}

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
        _shapes.pop(name, None)

    _tell(name)
    _queue(name)
    return said_on(camera, what)


def stop(camera=None):
    """Stop highlighting on [camera], or on every camera. Returns what to say."""
    with _lock:
        names = [cameras.normal_name(camera["name"])] if camera else list(_on)
        was = [name for name in names if name in _on]

        for name in was:
            del _on[name]
            _shapes.pop(name, None)

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

    shown = [dict(item) for item in kept["objects"] if wanted is None or item["label"] in wanted]

    # Each thing's exact shape, once the outline model has made it from a picture of this look.
    with _lock:
        made = [shape for shape in (_shapes.get(name) or {}).get("items", []) if shape.get("outline")]

    for item in shown:
        shape = _same(item, made)

        if shape:
            item["outline"] = shape["outline"]

    return shown


def _overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])

    if w <= 0 or h <= 0:
        return 0.0

    either = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h
    return w * h / either if either > 0 else 0.0


# A box this like the one an outline was made for is the same thing, hardly moved: its outline is kept
# until the next is made, rather than the line falling back to a box between looks.
SAME_THING = 0.6


def _same(item, shapes):
    """The shape in [shapes] made for [item]'s thing: the same label, the most overlapping box."""
    best, best_overlap = None, SAME_THING

    for shape in shapes:
        if shape["label"] == item["label"]:
            overlap = _overlap(shape["box"], item["box"])

            if overlap >= best_overlap:
                best, best_overlap = shape, overlap

    return best


# ---- outlines: made after each look, off the thread that took it ---------------------------------------------

def _queue(name):
    """Outline what is highlighted on camera [name] from its newest picture, when the model is there."""
    if not outline.present():
        return

    with _lock:
        wanted = _on.get(cameras.normal_name(name), ())

    kept = detections.marks(name)

    if wanted == () or kept is None or kept["ago"] > detections.MARKS_FRESH_SECONDS:
        return

    # What this look saw, where it saw it (what the board is sure of may carry a box from a look before),
    # cut from the picture the look was made from: sent when the look ended, its time (ms) before.
    seen = kept["now"] if kept["now"] is not None else kept["objects"]
    items = [{"label": item["label"], "box": list(item["box"])} for item in seen
             if wanted is None or item["label"] in wanted]
    report = detections.latest(name)
    took = (report or {}).get("ms")
    ended = time.monotonic() - kept["ago"]
    frame = (cameras.frame_at(name, ended - took / 1000.0) if took else None) or cameras.latest(name)

    if frame is None or not items:
        return

    global _worker

    with _work:
        _jobs[name] = (frame, items)

        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_outline_loop, name="jarvis-outline", daemon=True)
            _worker.start()

        _work.notify_all()


def _outline_loop():
    while True:
        with _work:
            while not _jobs:
                if not _work.wait(30):
                    return

            name, (frame, items) = _jobs.popitem()

        try:
            made = _outline(frame, items)
        except Exception as error:   # noqa: BLE001 - a picture that cannot be outlined keeps its boxes
            print(f"[JARVIS] could not outline the highlights: {error}")
            continue

        if made is None:
            continue

        with _lock:
            if name not in _on:
                continue

            _shapes[name] = made

        _tell(name)


def _outline(frame, items):
    """{"rgb", "items", "at"}: each of [items] with its outline and mask, from JPEG [frame]."""
    rgb = outline.picture(frame)
    view = outline.embed(rgb) if rgb is not None else None

    if view is None:
        return None

    made = []

    for item in items:
        found, steady, fits = outline.scored(view, item["box"])
        made.append(dict(item, mask=found, outline=outline.outline(found) if found is not None else []))
        _say_verdict(item["label"], found is not None, steady, fits)

    return {"rgb": rgb, "items": made, "at": time.monotonic()}


_verdicts = {}   # label -> whether its last outline was kept, so the log says when that changes


def _say_verdict(label, kept, steady, fits):
    """On the terminal, when a thing's outline starts or stops being good enough: SAM's two measures
    (actions/outline.py), to tune STEADY_AT_LEAST and FITS_BOX_AT_LEAST from this camera's own pictures."""
    if _verdicts.get(label) == kept:
        return

    _verdicts[label] = kept
    print(f"[JARVIS] outline of the {label}: steady {steady:.2f}, fits its box {fits:.2f} -> "
          f"{'its shape' if kept else 'its box'}", flush=True)


# ---- saving -------------------------------------------------------------------------------------------------

def save(camera):
    """Each thing highlighted on [camera] ({"name", ...}), cut out with no background, as a PNG in JARVIS's
    images folder (made if it is not there). Without the outline model, what is in its box. Returns what
    to say."""
    name = cameras.normal_name(camera["name"])
    shown = boxes(name)

    if not shown:
        return ("Nothing's highlighted to save, sir." if not active(name)
                else f"Nothing highlighted is in view of {detections._yours(name)} right now, sir.")

    base = files.root()

    if not base:
        return "I couldn't find my folder to save them in, sir."

    folder = os.path.join(base, IMAGES_FOLDER)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not create the images folder: {error}")
        return "I couldn't make the images folder, sir."

    with _lock:
        shapes = dict(_shapes.get(name) or {})

    made = [shape for shape in shapes.get("items", []) if shape.get("mask") is not None]
    rgb = shapes.get("rgb")

    if rgb is None:
        frame = cameras.latest(name)
        rgb = outline.picture(frame) if frame is not None else None

    if rgb is None:
        return "I haven't a picture from the camera to cut them from, sir."

    from PIL import Image

    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    saved, whole = [], 0

    for number, item in enumerate(shown, start=1):
        shape = _same(item, made)
        found = shape["mask"] if shape else None

        if found is not None and found.any():
            picture = outline.cutout(rgb, found)
            whole += 1
        else:
            height, width = rgb.shape[:2]
            x1, y1, x2, y2 = item["box"]
            box = (int(x1 * width), int(y1 * height), max(int(x2 * width), int(x1 * width) + 1),
                   max(int(y2 * height), int(y1 * height) + 1))
            picture = Image.fromarray(rgb).crop(box)

        slug = re.sub(r"[^a-z0-9]+", "-", f"{name} {item['label']}").strip("-")
        path = os.path.join(folder, f"{slug}-{stamp}-{number}.png")

        try:
            picture.save(path)
            saved.append(item["label"])
        except OSError as error:
            print(f"[JARVIS] could not save {path}: {error}")

    if not saved:
        return "I couldn't save them, sir."

    counts = {}

    for label in saved:
        counts[label] = counts.get(label, 0) + 1

    things = detections._listed([("someone" if label == "person" and n == 1 else detections._counted(label, n))
                                 for label, n in counts.items()])
    how = "cut out" if whole == len(saved) else "in their boxes" if not whole else "cut out where I could"
    return f"Saved {things} to your images folder, {how}, sir."


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
        _shapes.clear()
        _verdicts.clear()

    with _work:
        _jobs.clear()


def _marks_changed(camera):
    if active(camera):
        _tell(camera)
        _queue(camera)


detections.set_marks_listener(_marks_changed)
