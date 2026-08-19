import threading
import time
from dataclasses import dataclass, field


# Refuse anything absurd, so a misheard number cannot schedule for next year.
_MIN_SECONDS = 1
_MAX_SECONDS = 24 * 60 * 60

_UNIT_SECONDS = {
    "second": 1,
    "seconds": 1,
    "sec": 1,
    "secs": 1,
    "minute": 60,
    "minutes": 60,
    "min": 60,
    "mins": 60,
    "hour": 3600,
    "hours": 3600,
    "hr": 3600,
    "hrs": 3600,
}

# The prompt asks for the total already expressed in seconds, so a missing
# unit means seconds rather than anything else.
_DEFAULT_UNIT = 1


def _plural(value, noun):
    return f"{value} {noun}" if value == 1 else f"{value} {noun}s"


def describe_duration(seconds):
    """A spoken-friendly duration, e.g. '5 minutes', '1 hour and 30 minutes'."""
    seconds = int(round(seconds))

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []

    if hours:
        parts.append(_plural(hours, "hour"))

    if minutes:
        parts.append(_plural(minutes, "minute"))

    # Only mention seconds when the total is short, or it sounds fussy.
    if secs and not hours:
        parts.append(_plural(secs, "second"))

    if not parts:
        return "no time at all"

    if len(parts) == 1:
        return parts[0]

    return " and ".join(parts)


def to_seconds(amount, unit=None):
    """Convert an LLM-supplied amount and unit into seconds, or None."""
    if amount is None:
        return None

    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None

    if value <= 0:
        return None

    key = str(unit or "").strip().casefold()
    multiplier = _UNIT_SECONDS.get(key, _DEFAULT_UNIT)

    seconds = value * multiplier

    if not _MIN_SECONDS <= seconds <= _MAX_SECONDS:
        print(f"[JARVIS] refusing a reminder of {seconds:.0f} seconds")
        return None

    return seconds


@dataclass
class Reminder:
    identifier: int
    message: str
    due: float
    timer: threading.Timer = field(repr=False)

    @property
    def remaining(self):
        return max(0.0, self.due - time.monotonic())


class ReminderManager:
    """Schedules spoken reminders without colliding with JARVIS's voice.

    A fired reminder is handed to the alert listener supplied by the app. The
    speech layer serialises utterances and the listener resets itself after
    JARVIS speaks, so an alert can safely arrive at any moment.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reminders = {}
        self._next_id = 1
        self._listener = None

    def set_alert_listener(self, listener):
        """Register the callable used to announce a fired reminder."""
        self._listener = listener

    def add(self, seconds, message=None):
        """Schedule a reminder. Returns it, or None if the delay is invalid."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return None

        if not _MIN_SECONDS <= seconds <= _MAX_SECONDS:
            return None

        text = (message or "").strip()

        with self._lock:
            identifier = self._next_id
            self._next_id += 1

            timer = threading.Timer(seconds, self._fire, args=(identifier,))
            timer.daemon = True

            reminder = Reminder(
                identifier=identifier,
                message=text,
                due=time.monotonic() + seconds,
                timer=timer,
            )

            self._reminders[identifier] = reminder

        timer.start()

        return reminder

    def _fire(self, identifier):
        with self._lock:
            reminder = self._reminders.pop(identifier, None)

        if reminder is None:
            return

        if reminder.message:
            spoken = f"Reminder, sir. {reminder.message}."
        else:
            spoken = "Your timer has finished, sir."

        listener = self._listener

        if listener is None:
            print(f"[JARVIS] {spoken}")
            return

        try:
            listener(spoken)
        except Exception as error:
            print(f"[JARVIS] reminder announcement failed: {error}")

    def active(self):
        with self._lock:
            return tuple(sorted(self._reminders.values(), key=lambda r: r.due))

    def describe(self):
        """Spoken summary of what is pending."""
        pending = self.active()

        if not pending:
            return "You have nothing pending, sir."

        if len(pending) == 1:
            reminder = pending[0]
            when = describe_duration(reminder.remaining)

            if reminder.message:
                return f"One reminder in {when}, sir: {reminder.message}."

            return f"One timer, finishing in {when}, sir."

        described = [
            f"{reminder.message or 'a timer'} in "
            f"{describe_duration(reminder.remaining)}"
            for reminder in pending
        ]

        listed = ", ".join(described[:-1]) + f", and {described[-1]}"

        return f"You have {len(pending)} pending, sir: {listed}."

    def cancel_all(self):
        """Cancel everything pending. Returns how many were cancelled."""
        with self._lock:
            pending = list(self._reminders.values())
            self._reminders.clear()

        for reminder in pending:
            reminder.timer.cancel()

        return len(pending)
