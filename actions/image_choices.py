"""Three-image Unsplash search and selection state for JARVIS.

Sits between commands.py and actions.images: search() fetches three
candidates and holds them here without committing to any of them; select()
commits whichever one was chosen, via images.select_choice(). Kept
separate from images.py itself so images.py doesn't need to know anything
about "three of them, pick one" — it only ever deals with one current
image at a time, exactly as it did before this existed.
"""

from actions import images


_choices = []
_listener = None


def set_listener(listener):
    """Register a callable taking the current list of choices, or an
    empty list to mean the picker should close."""
    global _listener

    _listener = listener


def active():
    """True while a choice is outstanding — the picker is on screen and
    waiting for a pick."""
    return bool(_choices)


def _notify():
    if _listener:
        try:
            _listener(list(_choices))
        except Exception as error:
            print(f"[JARVIS] could not update image choices: {error}")


def search(query):
    """Fetch three candidates and show them. Returns True if any were
    found."""
    choices = images.search_choices(query, count=3)

    if not choices:
        return False

    _choices[:] = choices
    _notify()

    return True


def select(number):
    """Commit the numbered choice (1-based) as the current image, and
    close the picker. Returns True on success.

    Takes a plain integer — parsing "the second one" or "two" into that
    integer is commands.py's job, same as every other piece of spoken
    text gets turned into a plain value before it reaches here.
    """
    try:
        index = int(number) - 1
    except (TypeError, ValueError):
        return False

    if not 0 <= index < len(_choices):
        return False

    if not images.select_choice(_choices[index]):
        return False

    _choices.clear()
    _notify()

    return True


def cancel():
    """Close the picker without choosing anything."""
    _choices.clear()
    _notify()
