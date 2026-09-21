"""One recitation at a time, and what it is doing right now.

quran.py knows where verses come from and speech.py knows how to play
them. This is the bit in between: which verse is sounding, what comes
next, and what the HUD should be showing while it does.

A session owns a thread. That thread does one thing -- play a verse,
decide what follows -- and every control is a flag it reads between
verses rather than an interruption of it. Stopping, jumping and turning
auto-continue on or off therefore cannot land halfway through a verse
and leave two playing at once.

Announcements are handled by bracketing the whole session in JARVIS's
existing interaction gate. Anything that falls due while reciting is
queued by the machinery that already queues announcements during a
follow-up, and released when the session ends. Nothing here has to
know about market alerts or reminders at all.
"""

import threading

import speech
from actions import quran


# How long the loop waits between polls while paused. Short enough that
# resuming feels immediate, long enough to cost nothing.
_TICK = 0.05


class Session:
    """A surah being recited, verse by verse."""

    def __init__(
        self,
        surah,
        ayah=1,
        auto=False,
        reciter=quran.DEFAULT_RECITER,
        on_verse=None,
        on_end=None,
        on_error=None,
        on_begin=None,
    ):
        self.surah = surah
        self.ayah = max(1, int(ayah or 1))
        self.total = quran.verse_count(surah)
        self.reciter = reciter

        self._auto = threading.Event()

        if auto:
            self._auto.set()

        self._stop = threading.Event()
        self._paused = threading.Event()
        self._jump = None
        self._lock = threading.Lock()
        self._thread = None
        self._playing = False

        self._on_verse = on_verse
        self._on_end = on_end
        self._on_error = on_error
        self._on_begin = on_begin

    # ---- what the HUD reads -------------------------------------------

    @property
    def auto(self):
        return self._auto.is_set()

    @property
    def paused(self):
        return self._paused.is_set()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def state(self):
        """Everything the HUD needs, in one read."""
        entry = quran.surah(self.surah) or {}

        return {
            "surah": self.surah,
            "name": entry.get("englishName") or f"Surah {self.surah}",
            "translation": entry.get("englishNameTranslation") or "",
            "ayah": self.ayah,
            "total": self.total,
            "auto": self.auto,
            "paused": self.paused,
            "running": self.running,
            "reciter": self.reciter,
        }

    # ---- controls ------------------------------------------------------

    def start(self):
        """Begin reciting. Returns False if it is already running."""
        if self.running:
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        return True

    def stop(self):
        """End the session. The current verse is cut off immediately."""
        self._stop.set()
        self._paused.clear()

        if self._playing:
            speech.stop_speaking()

    def pause(self):
        """Hold it where it is, mid-verse.

        The flag stops the loop starting another verse; the call into
        speech stops the one already sounding. Both are needed: without
        the first, pausing between verses would be ignored, and without
        the second, pause would mean "after this verse", which is not
        what a pause button means.
        """
        self._paused.set()
        speech.pause_speaking()

    def resume(self):
        speech.resume_speaking()
        self._paused.clear()

    def set_auto(self, auto):
        """Turn auto-continue on or off.

        Turning it on mid-verse means the next verse follows this one;
        it does not restart anything.
        """
        if auto:
            self._auto.set()
        else:
            self._auto.clear()

    def jump(self, ayah):
        """Go to a verse. Returns None, or why it cannot be done.

        Checked against the surah's real length, so a number out of
        range is refused with something worth saying rather than a
        failed fetch.
        """
        try:
            ayah = int(ayah)
        except (TypeError, ValueError):
            return "That is not a verse number, sir."

        refusal = quran.bounds_message(self.surah, ayah)

        if refusal:
            return refusal

        with self._lock:
            self._jump = ayah

        # Cut the current verse short. The loop sees the pending jump
        # before it sees the stop and carries on from the new place.
        if self._playing:
            speech.stop_speaking()

        elif not self.running:
            self.ayah = ayah
            self.start()

        return None

    # ---- the loop ------------------------------------------------------

    def _notify(self, listener, *args):
        if not listener:
            return

        try:
            listener(*args)
        except Exception as error:
            print(f"[JARVIS] recitation listener failed: {error}")

    def _take_jump(self):
        with self._lock:
            ayah = self._jump
            self._jump = None

        return ayah

    def _wait_while_paused(self):
        while self._paused.is_set() and not self._stop.is_set():
            self._stop.wait(_TICK)

    def _run(self):
        self._notify(self._on_begin, self.state())

        reason = "finished"

        try:
            while not self._stop.is_set():
                self._wait_while_paused()

                if self._stop.is_set():
                    reason = "stopped"
                    break

                verse = quran.fetch_verse(
                    self.surah, self.ayah, reciter=self.reciter
                )

                if not verse:
                    reason = "unavailable"
                    self._notify(
                        self._on_error,
                        f"I could not fetch verse {self.ayah}, sir.",
                    )
                    break

                self._notify(self._on_verse, verse, self.state())

                # Fetched while this verse plays, so the next one starts
                # without a pause for the network. Only worth doing when
                # there is going to be a next one.
                if self.auto and self.ayah < self.total:
                    self._prefetch(self.ayah + 1)

                self._playing = True

                try:
                    finished = speech.play_file(verse["audio"])
                finally:
                    self._playing = False

                jumped = self._take_jump()

                if self._stop.is_set():
                    reason = "stopped"
                    break

                if jumped is not None:
                    self.ayah = jumped
                    continue

                if not finished:
                    # Cut off by the stop button rather than by us.
                    reason = "stopped"
                    break

                if not self.auto:
                    reason = "verse"
                    break

                if self.ayah >= self.total:
                    reason = "finished"
                    break

                self.ayah += 1

        finally:
            self._playing = False
            self._notify(self._on_end, reason, self.state())

    def _prefetch(self, ayah):
        def fetch():
            try:
                quran.fetch_verse(self.surah, ayah, reciter=self.reciter)
            except Exception as error:
                print(f"[JARVIS] could not prefetch {ayah}: {error}")

        threading.Thread(target=fetch, daemon=True).start()


_current = None
_current_lock = threading.Lock()

# Set once at startup by whoever owns the HUD and the announcement gate.
# Kept here rather than passed through every caller so commands.py can
# start a recitation without knowing anything about main.py.
_listeners = {}


def set_listeners(**listeners):
    """Register the default on_verse, on_begin, on_end and on_error."""
    _listeners.update(
        {name: call for name, call in listeners.items() if call}
    )


def current():
    """The session in progress, or None."""
    with _current_lock:
        if _current and _current.running:
            return _current

        return None


def begin(surah, ayah=1, auto=False, **listeners):
    """Start a recitation, ending any already in progress.

    Returns the session, or a string saying why it could not start.
    """
    global _current

    total = quran.verse_count(surah)

    if not total:
        return "I could not look that surah up, sir."

    refusal = quran.bounds_message(surah, ayah)

    if refusal:
        return refusal

    wanted = dict(_listeners)
    wanted.update(listeners)

    with _current_lock:
        if _current and _current.running:
            _current.stop()

        _current = Session(surah, ayah=ayah, auto=auto, **wanted)

    _current.start()

    return _current


def stop():
    """End whatever is being recited. True if something was."""
    session = current()

    if not session:
        return False

    session.stop()

    return True
