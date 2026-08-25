"""Things JARVIS notices on his own, and things he holds until you return.

Two ideas live here.

*Watching*: a background loop checks a handful of conditions and speaks only
when one genuinely warrants it. Quiet by default -- an assistant that cries
wolf gets muted, and a muted assistant is useless.

*Holding*: anything noticed while JARVIS was closed is kept and mentioned
when he next starts. A notice fired into a void that nobody heard is worse
than no notice at all.
"""

import os
import threading
import time
from datetime import datetime

import psutil

from actions import files, journal


PENDING_FILE = "jarvis-pending.txt"

# How often the conditions are checked.
POLL_SECONDS = 120

# Nothing non-urgent is spoken outside these hours.
QUIET_BEFORE = 8
QUIET_AFTER = 22

# Each observation is made at most once in this many hours, so a lasting
# condition is not repeated every two minutes.
REPEAT_HOURS = 6

# Thresholds worth mentioning.
DISK_PERCENT_FREE = 10
MEMORY_PERCENT = 92

# A coin is not mentioned again for this long after a move, so a market
# drifting past the threshold cannot nag every two minutes.
MARKET_QUIET_HOURS = 2

# Prices are compared against a mark that resets after this long, so
# "moved 2 percent" means recently rather than since the app started days
# ago.
MARK_HOURS = 12

# Memory has to stay high for this many checks before it is worth saying:
# a single spike while something compiles is not news.
MEMORY_SUSTAINED = 3

_lock = threading.Lock()


def _path():
    base = files.root()

    return os.path.join(base, PENDING_FILE) if base else None


def hold(text):
    """Keep a notice for when you are next here."""
    path = _path()

    if not path or not text:
        return False

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        with _lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp}\t{text}\n")

        return True

    except OSError as error:
        print(f"[JARVIS] could not hold a notice: {error}")
        return False


def held():
    """Notices waiting to be mentioned, as (when, text)."""
    path = _path()

    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            entries = []

            for line in handle:
                if not line.strip():
                    continue

                when, _, text = line.partition("\t")
                entries.append((when.strip(), text.strip()))

            return entries

    except OSError:
        return []


def clear_held():
    """Forget the held notices, once they have been mentioned."""
    path = _path()

    if not path or not os.path.exists(path):
        return

    try:
        with _lock:
            os.remove(path)

    except OSError as error:
        print(f"[JARVIS] could not clear held notices: {error}")


def _at(when):
    """The time part of a held notice's stamp, spoken as a clock time."""
    stamp = (when or "").strip()

    # Stored as "2026-08-25 03:00"; only the time is worth saying.
    if " " in stamp:
        return stamp.split(" ", 1)[1]

    return stamp


def catch_up():
    """What to say about anything missed, or None.

    Each notice carries the time it happened, so a market move that
    occurred at three in the morning is reported as such rather than as
    though it were news now.
    """
    entries = held()

    if not entries:
        return None

    clear_held()

    if len(entries) == 1:
        when, text = entries[0]

        return f"While you were away, sir, at {_at(when)}: {text}"

    parts = [f"at {_at(when)}, {text}" for when, text in entries[-4:]]

    listed = " ".join(parts)

    return (
        f"{len(entries)} things happened while you were away, sir. "
        f"{listed}"
    )


def _quiet_hours():
    hour = datetime.now().hour

    return hour < QUIET_BEFORE or hour >= QUIET_AFTER


class Watcher:
    """Checks a few conditions and speaks only when one matters."""

    def __init__(self):
        self._listener = None
        self._stop = threading.Event()
        self._thread = None

        # When each observation was last made, so it is not repeated.
        self._last = {}

        self._memory_high = 0

        # The price each coin is measured against, and when that mark was
        # taken: {coin: (price, when)}.
        self._marks = {}

    def set_listener(self, listener):
        """Register a callable taking the sentence to say."""
        self._listener = listener

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _due(self, key, hours=None):
        """True when this observation has not been made recently.

        An observation never made is always due. Comparing against zero
        would not do: time.monotonic() starts near zero in a fresh process,
        so nothing would ever be mentioned until JARVIS had been running
        for six hours.
        """
        last = self._last.get(key)

        if last is None:
            return True

        window = REPEAT_HOURS if hours is None else hours

        return time.monotonic() - last > window * 3600

    def _mention(self, key, text, urgent=False):
        """Say something, or hold it if now is not the time."""
        self._last[key] = time.monotonic()

        journal.alert(text)

        if not urgent and _quiet_hours():
            hold(text)
            return

        listener = self._listener

        if listener is None:
            # Nobody is listening, so it waits rather than vanishing.
            hold(text)
            return

        try:
            listener(text)
        except Exception as error:
            print(f"[JARVIS] could not speak an observation: {error}")
            hold(text)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as error:
                print(f"[JARVIS] observation failed: {error}")

            self._stop.wait(POLL_SECONDS)

    def _check(self):
        self._check_disk()
        self._check_memory()
        self._check_markets()

    def _check_markets(self):
        """Speak up when a coin has moved further than you asked about."""
        try:
            from actions import markets

            prices = markets.crypto()

        except Exception as error:
            print(f"[JARVIS] could not read the markets: {error}")
            return

        if not prices:
            return

        now = time.monotonic()

        for name, (price, _) in prices.items():
            if price is None:
                continue

            mark = self._marks.get(name)

            # First sighting, or the mark has gone stale, so start afresh.
            if mark is None or now - mark[1] > MARK_HOURS * 3600:
                self._marks[name] = (price, now)
                continue

            was, _when = mark

            if not was:
                self._marks[name] = (price, now)
                continue

            move = (price - was) / was * 100.0
            limit = markets.threshold_for(name)

            if abs(move) < limit:
                continue

            key = f"market:{name}"

            if not self._due(key, MARKET_QUIET_HOURS):
                continue

            spoken = markets.COINS.get(name, {}).get("spoken", name)
            direction = "up" if move > 0 else "down"

            self._mention(
                key,
                (
                    f"{spoken} is {direction} "
                    f"{abs(move):.1f} percent, sir, "
                    f"at {markets.spoken_price(price)}."
                ),
            )

            # Measuring from here on, so the next alert is about the next
            # move rather than the same one all over again.
            self._marks[name] = (price, now)

    def _check_disk(self):
        if not self._due("disk"):
            return

        try:
            usage = psutil.disk_usage(os.path.expanduser("~"))
        except Exception:
            return

        free_percent = 100.0 - usage.percent

        if free_percent >= DISK_PERCENT_FREE:
            return

        free_gb = usage.free / (1024 ** 3)

        self._mention(
            "disk",
            (
                f"Your disk is nearly full, sir. "
                f"{free_gb:.0f} gigabytes left, about "
                f"{free_percent:.0f} percent."
            ),
        )

    def _check_memory(self):
        try:
            percent = psutil.virtual_memory().percent
        except Exception:
            return

        if percent < MEMORY_PERCENT:
            self._memory_high = 0
            return

        self._memory_high += 1

        # A single spike while something builds is not worth mentioning.
        if self._memory_high < MEMORY_SUSTAINED or not self._due("memory"):
            return

        self._mention(
            "memory",
            (
                f"Memory has been above {percent:.0f} percent for a while, "
                "sir. Something may be worth closing."
            ),
        )


watcher = Watcher()
