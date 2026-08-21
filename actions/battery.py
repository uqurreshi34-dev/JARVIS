import threading
import time

import psutil


# Announced while discharging, highest first. Crossing one downward speaks.
DISCHARGE_THRESHOLDS = (50, 30)

# Announced while charging, when the smart-charge ceiling is reached.
CHARGE_CEILING = 80

# How often the battery is checked. Charge moves slowly, so this is gentle.
POLL_SECONDS = 60

# Below this, warn again even if it has already been announced once.
CRITICAL = 10


def _percent_and_state():
    battery = psutil.sensors_battery()

    if battery is None:
        return None, False

    return round(battery.percent), bool(battery.power_plugged)


class BatteryMonitor:
    """Watches the battery and speaks when a threshold is crossed.

    Announcements go through the same listener as reminders, so they queue
    behind whatever JARVIS is already saying rather than talking over it.
    """

    def __init__(self):
        self._listener = None
        self._stop = threading.Event()
        self._thread = None

        # Thresholds already announced for the current charge direction.
        self._announced = set()
        self._was_plugged = None
        self._last_percent = None

    def set_alert_listener(self, listener):
        self._listener = listener

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _announce(self, text):
        listener = self._listener

        if listener is None:
            print(f"[JARVIS] {text}")
            return

        try:
            listener(text)
        except Exception as error:
            print(f"[JARVIS] battery announcement failed: {error}")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as error:
                print(f"[JARVIS] battery check failed: {error}")

            self._stop.wait(POLL_SECONDS)

    def _check(self):
        percent, plugged = _percent_and_state()

        if percent is None:
            # No battery, so nothing to watch. Stop rather than poll forever.
            self._stop.set()
            return

        first_look = self._was_plugged is None

        # Plugging in or unplugging starts a fresh set of announcements.
        if not first_look and plugged != self._was_plugged:
            self._announced.clear()

        self._was_plugged = plugged

        if first_look:
            # On startup, record where the battery already is without
            # saying anything. Only crossing a threshold afterwards speaks.
            for threshold in DISCHARGE_THRESHOLDS:
                if percent <= threshold:
                    self._announced.add(threshold)

            if plugged and percent >= CHARGE_CEILING:
                self._announced.add(CHARGE_CEILING)

            self._last_percent = percent
            return

        message = self._message_for(percent, plugged)

        self._last_percent = percent

        if message:
            self._announce(message)

    def _message_for(self, percent, plugged):
        if plugged:
            if percent >= CHARGE_CEILING and CHARGE_CEILING not in self._announced:
                self._announced.add(CHARGE_CEILING)

                return (
                    f"Battery has reached {percent} percent, sir. "
                    "You can unplug when convenient."
                )

            return None

        # Critical warnings repeat, since the first may have been missed.
        if percent <= CRITICAL:
            if self._last_percent is None or percent < self._last_percent:
                return (
                    f"Battery is critically low at {percent} percent, sir. "
                    "You should plug in now."
                )

            return None

        for threshold in DISCHARGE_THRESHOLDS:
            if percent <= threshold and threshold not in self._announced:
                self._announced.add(threshold)

                if threshold <= 30:
                    return (
                        f"Battery is down to {percent} percent, sir. "
                        "Worth plugging in soon."
                    )

                return f"Battery is at {percent} percent, sir."

        return None


battery_monitor = BatteryMonitor()
