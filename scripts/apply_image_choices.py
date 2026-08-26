from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(path, replacements):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    original = text
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"Patch anchor not found in {path}: {old[:100]!r}")
        text = text.replace(old, new, 1)
    if text != original:
        p.write_text(text, encoding="utf-8")


# actions/images.py: retain the existing one-image API and add a three-result
# search plus a selector which promotes a candidate into the existing _current
# image state. Nothing in rotate/enlarge/shrink/save changes.
patch("actions/images.py", [(
    "\ndef _render():\n",
    '''\n\ndef search_choices(query, count=3):\n    """Search Unsplash and download up to count candidates without changing the current image."""\n    if not _ACCESS_KEY:\n        print("[JARVIS] no UNSPLASH_ACCESS_KEY set")\n        return []\n\n    text = (query or "").strip()\n    if not text:\n        return []\n\n    try:\n        response = _session.get(\n            _SEARCH_URL,\n            params={"query": text, "per_page": max(1, min(int(count), 3)), "orientation": "landscape"},\n            headers={"Authorization": f"Client-ID {_ACCESS_KEY}"},\n            timeout=TIMEOUT,\n        )\n        response.raise_for_status()\n        payload = response.json()\n    except (requests.RequestException, ValueError) as error:\n        print(f"[JARVIS] Unsplash choice search failed: {error}")\n        return []\n\n    choices = []\n    for photo in payload.get("results") or []:\n        image_url = (photo.get("urls") or {}).get("regular")\n        if not image_url:\n            continue\n        try:\n            image_response = _session.get(image_url, timeout=TIMEOUT)\n            image_response.raise_for_status()\n        except requests.RequestException as error:\n            print(f"[JARVIS] could not download a choice: {error}")\n            continue\n        user = photo.get("user") or {}\n        links = photo.get("links") or {}\n        user_links = user.get("links") or {}\n        choices.append({\n            "data": image_response.content,\n            "title": text.title(),\n            "query": text,\n            "photographer": user.get("name"),\n            "photographer_link": user_links.get("html"),\n            "photo_link": links.get("html"),\n            "download_location": links.get("download_location"),\n        })\n\n    return choices\n\n\ndef select_choice(choice):\n    """Promote a previously downloaded choice into the normal current image."""\n    if not choice or not choice.get("data"):\n        return False\n    with _lock:\n        _current["original"] = choice["data"]\n        _current["rotation"] = 0\n        _current["scale"] = 1.0\n        _current["query"] = choice.get("query")\n        _current["photographer"] = choice.get("photographer")\n        _current["photographer_link"] = choice.get("photographer_link")\n        _current["photo_link"] = choice.get("photo_link")\n        _current["download_location"] = choice.get("download_location")\n    if _current["download_location"]:\n        _trigger_download(_current["download_location"])\n    return True\n\n\ndef _render():\n'''
)])

# commands.py: make image selection a local fast-path state, before pending
# yes/no handling. A normal command therefore still wins once a choice is gone.
patch("commands.py", [(
    "    proofread,\n    notes,\n    safety,\n    screen_control,\n)",
    "    proofread,\n    notes,\n    safety,\n    screen_control,\n    image_choices,\n)"),
(
    "_interpreter = CommandInterpreter()\n",
    "_interpreter = CommandInterpreter()\n\n_image_choices_listener = None\n\n\ndef set_image_choices_listener(listener):\n    global _image_choices_listener\n    _image_choices_listener = listener\n\n\ndef _push_image_choices():\n    if _image_choices_listener:\n        _image_choices_listener(image_choices.active())\n\n\ndef _select_image_choice(number):\n    if not image_choices.select(number):\n        return \"There isn't an image choice for that number, sir.\"\n    _push_image()\n    return \"Selected, sir.\"\n\n"),
(
    "def handle_command(command):\n",
    "def handle_command(command):\n    choice = {\"one\": 1, \"two\": 2, \"three\": 3, \"1\": 1, \"2\": 2, \"3\": 3}.get(_normalise(command))\n    if image_choices.active():\n        if choice is not None:\n            return _action(\"select_image\", f\"Selecting image {choice}, sir.\", lambda: _select_image_choice(choice))\n        if _fast_path(command) is not None:\n            image_choices.cancel()\n            _push_image_choices()\n        else:\n            # A non-number command replaces the chooser, rather than being swallowed.\n            image_choices.cancel()\n            _push_image_choices()\n\n"),
(
    '    if intent == "show_image" and (verbatim_text or text):\n        wanted = verbatim_text or text\n        return _query(intent, lambda: _show_image(wanted))\n',
    '    if intent == "show_image" and (verbatim_text or text):\n        wanted = verbatim_text or text\n        return _query(intent, lambda: _show_image(wanted))\n\n    if intent == "select_image" and amount is not None:\n        return _action(intent, f"Selecting image {int(amount)}, sir.", lambda: _select_image_choice(int(amount)))\n')
])

# Replace the existing _show_image body so image searches open the chooser.
patch("commands.py", [(
    'def _show_image(query):\n    """Search Unsplash and display the result."""\n    if not images.available():\n        return (\n            "I don\'t have an Unsplash key set up, sir. Set "\n            "UNSPLASH_ACCESS_KEY and I\'ll be able to."\n        )\n\n    if not images.search(query):\n        return f"I couldn\'t find a picture of {query}, sir."\n\n    _push_image()\n\n    return f"Here\'s {query}, sir."\n',
    'def _show_image(query):\n    """Search Unsplash and present three candidates."""\n    if not images.available():\n        return (\n            "I don\'t have an Unsplash key set up, sir. Set "\n            "UNSPLASH_ACCESS_KEY and I\'ll be able to."\n        )\n\n    if not image_choices.search(query):\n        return f"I couldn\'t find a picture of {query}, sir."\n\n    _push_image_choices()\n    return f"I found three pictures of {query}, sir. Choose 1, 2, or 3."\n')
])

# main.py: create the chooser beside the existing image panel and wire both
# voice selection and mouse clicks into the same selection function.
patch("main.py", [
    ("from image_panel import ImagePanel\n", "from image_panel import ImagePanel\nfrom image_choices_panel import ImageChoicesPanel\n"),
    ("    set_image_listener,\n", "    set_image_listener,\n    set_image_choices_listener,\n"),
    ("    photo_beam = Beam(photo, hud)\n", "    photo_beam = Beam(photo, hud)\n\n    choices = ImageChoicesPanel()\n    choices.set_anchor(hud)\n    choices_beam = Beam(choices, hud)\n\n    def choices_update(active):\n        if active:\n            choices.show_choices.emit(__import__('actions.image_choices', fromlist=['_choices'])._choices)\n            choices_beam.shown.emit()\n        else:\n            choices.hide_choices.emit()\n            choices_beam.hidden.emit()\n\n    set_image_choices_listener(choices_update)\n\n    def choose_from_click(number):\n        from commands import _select_image_choice\n        _select_image_choice(number)\n\n    choices.choice_clicked.connect(choose_from_click)\n"),
    ("    hud.shutdown.connect(photo_beam.hidden.emit)\n", "    hud.shutdown.connect(photo_beam.hidden.emit)\n    hud.shutdown.connect(choices.hide_choices.emit)\n    hud.shutdown.connect(choices_beam.hidden.emit)\n"),
])
PY
