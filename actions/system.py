from datetime import datetime


_ORDINALS = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}


def _ordinal(day):
    return f"{day}{_ORDINALS.get(day, 'th')}"


def describe_time(now=None):
    """A spoken-friendly description of the current time and date."""
    now = now or datetime.now()

    clock = now.strftime("%I:%M %p").lstrip("0")
    weekday = now.strftime("%A")
    month = now.strftime("%B")

    return (
        f"It's {clock} on {weekday}, "
        f"the {_ordinal(now.day)} of {month}."
    )
