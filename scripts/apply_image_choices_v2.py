from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def edit(path, fn):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    new = fn(text)
    if new == text:
        raise SystemExit(f"No change made to {path}; patch anchor may already be applied.")
    p.write_text(new, encoding="utf-8")


def once(text, old, new, path):
    if text.count(old) != 1:
        raise SystemExit(f"Expected exactly one anchor in {path}, found {text.count(old)}: {old[:100]!r}")
    return text.replace(old, new, 1)


# Add the image-choice API without disturbing any existing image transforms.
edit("actions/images.py", lambda t: once(t, "\ndef _render():\n", r'''\ndef search_choices(query, count=3):\n    """Search Unsplash and download up to count candidates without changing the current image."""\n    if not _ACCESS_KEY:\n        print("[JARVIS] no UNSPLASH_ACCESS_KEY set")\n        return []\n    text = (query or "").strip()\n    if not text:\n        return []\n    try:\n        response = _session.get(\n            _SEARCH_URL,\n            params={"query": text, "per_page": max(1, min(int(count), 3)), "orientation": "landscape"},\n            headers={"Authorization": f"Client-ID {_ACCESS_KEY}"},\n            timeout=TIMEOUT,\n        )\n        response.raise_for_status()\n        payload = response.json()\n    except (requests.RequestException, ValueError) as error:\n        print(f"[JARVIS] Unsplash choice search failed: {error}")\n        return []\n    choices = []\n    for photo in payload.get("results") or []:\n        image_url = (photo.get("urls") or {}).get("regular")\n        if not image_url:\n            continue\n        try:\n            image_response = _session.get(image_url, timeout=TIMEOUT)\n            image_response.raise_for_status()\n        except requests.RequestException as error:\n            print(f"[JARVIS] could not download a choice: {error}")\n            continue\n        user = photo.get("user") or {}\n        links = photo.get("links") or {}\n        user_links = user.get("links") or {}\n        choices.append({\n            "data": image_response.content,\n            "title": text.title(),\n            "query": text,\n            "photographer": user.get("name"),\n            "photographer_link": user_links.get("html"),\n            "photo_link": links.get("html"),\n            "download_location": links.get("download_location"),\n        })\n    return choices\n\n\ndef select_choice(choice):\n    """Promote a previously downloaded choice into the normal current image."""\n    if not choice or not choice.get("data"):\n        return False\n    with _lock:\n        _current["original"] = choice["data"]\n        _current["rotation"] = 0\n        _current["scale"] = 1.0\n        _current["query"] = choice.get("query")\n        _current["photographer"] = choice.get("photographer")\n        _current["photographer_link"] = choice.get("photographer_link")\n        _current["photo_link"] = choice.get("photo_link")\n        _current["download_location"] = choice.get("download_location")\n    if _current["download_location"]:\n        _trigger_download(_current["download_location"])\n    return True\n\n\ndef _render():\n'''.replace(r'\n','\n'), "actions/images.py"))

# Give the choice module a safe accessor for the Qt side.
edit("actions/image_choices.py", lambda t: once(t, "def active():\n    return bool(_choices)\n", "def active():\n    return bool(_choices)\n\n\ndef get_choices():\n    return list(_choices)\n", "actions/image_choices.py"))

# commands.py imports and listener state.
def commands_import(t):
    t = once(t, "    screen_control,\n)", "    screen_control,\n    image_choices,\n)", "commands.py")
    t = once(t, "_interpreter = CommandInterpreter()\n", '''_interpreter = CommandInterpreter()\n\n_image_choices_listener = None\n\n\ndef set_image_choices_listener(listener):\n    global _image_choices_listener\n    _image_choices_listener = listener\n\n\ndef _push_image_choices():\n    if _image_choices_listener:\n        _image_choices_listener(image_choices.get_choices())\n\n\ndef _select_image_choice(number):\n    if not image_choices.select(number):\n        return False\n    _push_image_choices()\n    _push_image()\n    return True\n\n''', "commands.py")
    return t
edit("commands.py", commands_import)

# Replace only the existing _show_image implementation by locating its full function.
def show_image(t):
    pattern = re.compile(r'def _show_image\(query\):\n.*?(?=\n\ndef _hide_image\(\):)', re.S)
    replacement = '''def _show_image(query):\n    """Search Unsplash and present three candidates."""\n    if not images.available():\n        return (\n            "I don't have an Unsplash key set up, sir. Set "\n            "UNSPLASH_ACCESS_KEY and I'll be able to."\n        )\n    if not image_choices.search(query):\n        return f"I couldn't find a picture of {query}, sir."\n    _push_image_choices()\n    return f"I found three pictures of {query}, sir. Choose 1, 2, or 3."\n'''
    if not pattern.search(t):
        raise SystemExit("Could not locate _show_image in commands.py")
    return pattern.sub(replacement, t, count=1)
edit("commands.py", show_image)

# Selection is handled immediately after pronoun resolution, before awaiting
# or pending yes/no logic. This makes a spoken 1/2/3 an unambiguous chooser.
def handle_entry(t):
    marker = "def handle_command(*command):\n    command = _resolve_pronouns(*command)\n"
    insert = marker + '''    if image_choices.active():\n        choice = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}.get(_normalise(command))\n        if choice is not None:\n            return _action(\n                "select_image",\n                f"Selecting image {choice}, sir.",\n                lambda: _select_image_choice(choice),\n            )\n        # A different instruction replaces the chooser rather than being swallowed.\n        image_choices.cancel()\n        _push_image_choices()\n'''
    return once(t, marker, insert, "commands.py")
edit("commands.py", handle_entry)

# main.py wires the new panel alongside the existing ImagePanel.
def main_patch(t):
    t = once(t, "from image_panel import ImagePanel\n", "from image_panel import ImagePanel\nfrom image_choices_panel import ImageChoicesPanel\n", "main.py")
    t = once(t, "    set_image_listener,\n", "    set_image_listener,\n    set_image_choices_listener,\n", "main.py")
    block = '''    photo_beam = Beam(photo, hud)\n'''
    addition = block + '''\n    choices = ImageChoicesPanel()\n    choices.set_anchor(hud)\n    choices_beam = Beam(choices, hud)\n\n    def choices_update(items):\n        if items:\n            choices.show_choices.emit(items)\n            choices_beam.shown.emit()\n        else:\n            choices.hide_choices.emit()\n            choices_beam.hidden.emit()\n\n    set_image_choices_listener(choices_update)\n\n    def choose_from_click(number):\n        from commands import _select_image_choice\n        _select_image_choice(number)\n\n    choices.choice_clicked.connect(choose_from_click)\n'''
    t = once(t, block, addition, "main.py")
    t = once(t, "    hud.shutdown.connect(photo_beam.hidden.emit)\n", "    hud.shutdown.connect(photo_beam.hidden.emit)\n    hud.shutdown.connect(choices.hide_choices.emit)\n    hud.shutdown.connect(choices_beam.hidden.emit)\n", "main.py")
    return t
edit("main.py", main_patch)

print("Image-choice integration patch applied successfully.")
