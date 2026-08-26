"""Fetch a photo from Unsplash and let it be turned, resized, and saved.

Unsplash provides the picture — this is the one thing here that costs a
network call, exactly like the camera's "look" does. Everything after
that — rotating, resizing, restoring, saving — is pure local pixel
manipulation with Pillow, no network involved at all.

Needs an Unsplash access key, set once as an environment variable:
    setx UNSPLASH_ACCESS_KEY "your-access-key"
Demo tier gives 50 requests/hour, which a single-user assistant will never
come close to — there is no need to apply for production.

Two of Unsplash's API guidelines are honoured here, since they apply at
demo tier too, not only to an approved production app:
- the /photos/:id/download endpoint is pinged once per photo shown, which
  is Unsplash's own documented "this was used" signal, separate from
  actually saving a copy to disk
- attribution (photographer name, linked, "on Unsplash") is available via
  attribution() for the panel to display — shown, never spoken, so asking
  to see a photo does not turn into a spoken credit line every time
"""

import io
import os
import re
import threading
import time

import requests
from PIL import Image

from actions import files


_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")

_SEARCH_URL = "https://api.unsplash.com/search/photos"
TIMEOUT = 6

_session = requests.Session()

# PIL's ROTATE_90 constant is, confusingly, counter-clockwise — verified
# directly with a corner-marked test image rather than assumed, since the
# name suggests the opposite. This maps a plain "rotate 90 degrees"
# (clockwise, the everyday default) to the transpose that actually
# achieves it.
_TRANSPOSE = {
    90: Image.ROTATE_270,
    180: Image.ROTATE_180,
    270: Image.ROTATE_90,
}

# How much each "bigger" / "smaller" step changes the size, and how far
# that can go. A render too small to see or too large to be fast is not
# useful either way.
ZOOM_STEP = 1.25
MIN_SCALE = 0.25
MAX_SCALE = 3.0

_lock = threading.Lock()

# Everything about the photo currently on screen. Kept as the pristine
# fetched bytes plus a rotation/scale to apply, rather than repeatedly
# transforming an already-transformed image — every render starts fresh
# from the original, so ten rotations and ten resizes in a row look
# exactly as sharp as the first one did.
_current = {
    "original": None,          # pristine bytes as fetched from Unsplash
    "rotation": 0,              # 0, 90, 180 or 270 — clockwise from original
    "scale": 1.0,
    "query": None,               # what was searched for, for the heading
    "photographer": None,
    "photographer_link": None,
    "photo_link": None,
    "download_location": None,
}


def available():
    return bool(_ACCESS_KEY)


def _trigger_download(download_location):
    """Tell Unsplash this photo was used. Best effort: never makes the
    person wait on a slow connection just to see their picture."""
    def fire():
        try:
            _session.get(
                download_location,
                params={"client_id": _ACCESS_KEY},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            print(
                f"[JARVIS] could not register the Unsplash download: {error}")

    threading.Thread(target=fire, daemon=True).start()


def search(query):
    """Find a photo and fetch it. Returns True on success.

    The photo becomes the new "current" image, replacing whatever was
    there before, with rotation and scale reset — a fresh search is a
    fresh start, not a transform stacked onto the last picture.
    """
    if not _ACCESS_KEY:
        print("[JARVIS] no UNSPLASH_ACCESS_KEY set")
        return False

    text = (query or "").strip()

    if not text:
        return False

    try:
        response = _session.get(
            _SEARCH_URL,
            params={
                "query": text, "per_page": 1, "orientation": "landscape",
            },
            headers={"Authorization": f"Client-ID {_ACCESS_KEY}"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

    except (requests.RequestException, ValueError) as error:
        print(f"[JARVIS] Unsplash search failed: {error}")
        return False

    results = payload.get("results") or []

    if not results:
        return False

    photo = results[0]
    image_url = (photo.get("urls") or {}).get("regular")

    if not image_url:
        return False

    try:
        image_response = _session.get(image_url, timeout=TIMEOUT)
        image_response.raise_for_status()
        original = image_response.content

    except requests.RequestException as error:
        print(f"[JARVIS] could not download the photo: {error}")
        return False

    with _lock:
        _current["original"] = original
        _current["rotation"] = 0
        _current["scale"] = 1.0
        _current["query"] = text
        _current["photographer"] = (photo.get("user") or {}).get("name")
        _current["photographer_link"] = (
            (photo.get("user") or {}).get("links") or {}
        ).get("html")
        _current["photo_link"] = (photo.get("links") or {}).get("html")
        _current["download_location"] = (
            photo.get("links") or {}
        ).get("download_location")

    if _current["download_location"]:
        _trigger_download(_current["download_location"])

    return True


def _render():
    """The current photo as PNG bytes, with rotation and scale applied.

    Always rebuilt from the pristine original rather than the last
    render (see _current's docstring), so quality never degrades no
    matter how the image has been turned or resized. Caller must hold
    _lock.
    """
    original = _current["original"]

    if not original:
        return None

    image = Image.open(io.BytesIO(original)).convert("RGB")

    rotation = _current["rotation"]

    if rotation in _TRANSPOSE:
        image = image.transpose(_TRANSPOSE[rotation])

    scale = _current["scale"]

    if scale != 1.0:
        width = max(1, round(image.width * scale))
        height = max(1, round(image.height * scale))
        image = image.resize((width, height), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    return buffer.getvalue()


def has_image():
    return _current["original"] is not None


def current_bytes():
    """The current photo as PNG bytes, exactly as it would be shown or
    saved right now — for a caller (the panel update) that needs to
    display the current state without changing it. Properly holds the
    lock itself, unlike _render(), which expects the caller already has
    it and exists only for the transform functions below that do."""
    with _lock:
        return _render()


def current_title():
    """What the panel heading should say."""
    query = _current.get("query")

    return query.title() if query else "Image"


def attribution():
    """(photographer, photographer_link, photo_link), any of which may be
    None if nothing is showing or Unsplash did not supply them."""
    return (
        _current.get("photographer"),
        _current.get("photographer_link"),
        _current.get("photo_link"),
    )


def rotate(degrees):
    """Turn the image by a multiple of 90, clockwise for a positive
    number. Returns the rendered bytes, or None if nothing is showing."""
    with _lock:
        if not _current["original"]:
            return None

        _current["rotation"] = (_current["rotation"] + round(degrees)) % 360

        return _render()


def enlarge():
    with _lock:
        if not _current["original"]:
            return None

        _current["scale"] = min(MAX_SCALE, _current["scale"] * ZOOM_STEP)

        return _render()


def shrink():
    with _lock:
        if not _current["original"]:
            return None

        _current["scale"] = max(MIN_SCALE, _current["scale"] / ZOOM_STEP)

        return _render()


def restore():
    """Back to the size it was fetched at. Rotation is left as it is —
    only an earlier request to be bigger or smaller is undone."""
    with _lock:
        if not _current["original"]:
            return None

        _current["scale"] = 1.0

        return _render()


_IMAGES_FOLDER = "images"


def save():
    """Write the image exactly as it currently looks — whatever rotation
    and zoom are in effect — into the JARVIS images folder, creating that
    folder if it does not exist yet. Returns the path, or None."""
    with _lock:
        data = _render()
        query = _current.get("query") or "image"

    if not data:
        return None

    base = files.root()

    if not base:
        return None

    folder = os.path.join(base, _IMAGES_FOLDER)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        print(f"[JARVIS] could not create the images folder: {error}")
        return None

    stem = re.sub(r"[^\w\s-]", "", query).strip().replace(" ", "-").lower()
    stem = stem or "image"
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(folder, f"{stem}-{stamp}.png")

    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError as error:
        print(f"[JARVIS] could not save the image: {error}")
        return None

    print(f"[JARVIS] image saved to {path}")

    return path


def hide():
    with _lock:
        _current["original"] = None
        _current["rotation"] = 0
        _current["scale"] = 1.0
        _current["query"] = None
        _current["photographer"] = None
        _current["photographer_link"] = None
        _current["photo_link"] = None
        _current["download_location"] = None
