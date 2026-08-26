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


def _commit_photo(photo, text, image_bytes, trigger=True):
    """Make a fetched photo the current one — rotation/scale reset, its
    metadata recorded, and (unless told not to) Unsplash told it was
    used. Shared by search() and select_choice() so this exists in
    exactly one place; trigger=False lets the 3-preview path in
    search_choices() skip the download-tracking ping for photos that
    were only shown, not actually chosen — see search_choices()'s
    docstring for why that distinction matters."""
    with _lock:
        _current["original"] = image_bytes
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

    if trigger and _current["download_location"]:
        _trigger_download(_current["download_location"])


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

    _commit_photo(photo, text, original)

    return True


def search_choices(query, count=3):
    """Find up to `count` candidate photos, as lightweight previews.

    None of these become the "current" image — that only happens once
    one is actually picked, via select_choice(). Deliberately fetches
    the small preview size rather than the full "regular" resolution
    for all `count` results: only one of them, at most, will ever
    actually be used, so downloading full-size copies of the other two
    would just be wasted bandwidth and time.

    For the same reason, this does NOT ping Unsplash's download-tracking
    endpoint for any of these three — that signal means a photo was
    used, and being one of three options briefly shown in a picker is
    not that. select_choice() below is where that ping actually happens,
    for the one photo that really was chosen.

    Returns a list of dicts — each with "data" (preview PNG bytes),
    "title" (for the picker card), and "photo" (the raw Unsplash record,
    which select_choice() needs to finish the job) — or an empty list if
    nothing was found or the search failed.
    """
    if not _ACCESS_KEY:
        print("[JARVIS] no UNSPLASH_ACCESS_KEY set")
        return []

    text = (query or "").strip()

    if not text:
        return []

    try:
        response = _session.get(
            _SEARCH_URL,
            params={
                "query": text,
                "per_page": max(1, min(10, count)),
                "orientation": "landscape",
            },
            headers={"Authorization": f"Client-ID {_ACCESS_KEY}"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

    except (requests.RequestException, ValueError) as error:
        print(f"[JARVIS] Unsplash search failed: {error}")
        return []

    results = (payload.get("results") or [])[:count]

    choices = []

    for photo in results:
        urls = photo.get("urls") or {}
        preview_url = urls.get("small") or urls.get(
            "thumb") or urls.get("regular")

        if not preview_url:
            continue

        try:
            preview_response = _session.get(preview_url, timeout=TIMEOUT)
            preview_response.raise_for_status()
            preview_bytes = preview_response.content

        except requests.RequestException as error:
            print(f"[JARVIS] could not fetch a preview: {error}")
            continue

        title = (
            photo.get("description")
            or photo.get("alt_description")
            or text
        )

        choices.append({
            "data": preview_bytes,
            "title": title.title() if title else text.title(),
            "photo": photo,
            "query": text,
        })

    return choices


def select_choice(choice):
    """Commit one of search_choices()'s results as the current image,
    downloading it at full resolution now that it is actually wanted.
    Returns True on success."""
    if not choice:
        return False

    photo = choice.get("photo") or {}
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

    _commit_photo(photo, choice.get("query") or "image", original)

    return True


def _rotated_original():
    """The pristine original, decoded and rotated but not yet scaled.
    Shared by _render() (which also applies scale, for saving) and
    current_display() (which reports scale separately, for showing) so
    the load-and-rotate step exists in exactly one place. Caller must
    hold _lock."""
    original = _current["original"]

    if not original:
        return None

    image = Image.open(io.BytesIO(original)).convert("RGB")

    rotation = _current["rotation"]

    if rotation in _TRANSPOSE:
        image = image.transpose(_TRANSPOSE[rotation])

    return image


def _render():
    """The current photo as PNG bytes, with rotation and scale applied.

    Always rebuilt from the pristine original rather than the last
    render (see _current's docstring), so quality never degrades no
    matter how the image has been turned or resized. Caller must hold
    _lock.
    """
    image = _rotated_original()

    if image is None:
        return None

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
    """The current photo as PNG bytes, rotation and scale both applied —
    exactly what save() would write right now. Kept as a general-purpose
    accessor; current_display() below is what the live panel actually
    uses, for the reason explained there."""
    with _lock:
        return _render()


def current_display():
    """(rotation-only PNG bytes, scale) for the panel to show.

    Deliberately not the same render save() uses. The panel always fits
    whatever image it is given to its own fixed window — that is what
    makes "make it bigger" invisible if the bytes handed over are
    already resized: an enlarged image, fit straight back down to the
    same viewing window, comes out the same size on screen as it started.
    Sending rotation only, with the scale reported as a separate number,
    lets the panel apply that zoom on top of its own fit-to-window sizing
    instead of underneath it — which is what actually makes zooming
    visible. save() is unaffected: it still renders rotation and scale
    together, so the saved file's real pixel dimensions reflect the zoom
    regardless of how the live preview draws it.
    """
    with _lock:
        image = _rotated_original()

        if image is None:
            return None, 1.0

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        return buffer.getvalue(), _current["scale"]


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
    """Back to how it was fetched — undoes both rotation and zoom.

    Originally this reset only the zoom, reasoning that "restore to
    original size" sounded size-specific. On reflection that reading was
    too narrow: after rotating and resizing, "restore the image" is a
    request to put it back, not a request to undo one specific edit
    while quietly keeping the other.
    """
    with _lock:
        if not _current["original"]:
            return None

        _current["rotation"] = 0
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
