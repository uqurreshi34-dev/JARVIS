"""Three-image Unsplash search and selection state for JARVIS."""

from actions import images

_choices = []
_listener = None


def set_listener(listener):
    global _listener
    _listener = listener


def active():
    return bool(_choices)


def _notify():
    if _listener:
        try:
            _listener(list(_choices))
        except Exception as error:
            print(f"[JARVIS] could not update image choices: {error}")


def search(query):
    choices = images.search_choices(query, count=3)
    if not choices:
        return False
    _choices[:] = choices
    _notify()
    return True


def select(number):
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
    _choices.clear()
    _notify()
