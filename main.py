import sys
import threading
import time
from datetime import datetime

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from actions import camera, diary, memory
from actions.battery import battery_monitor
from actions.watch import watcher, catch_up
import phrases
from actions.markets import market_monitor
from beam import Beam
from brain_panel import BrainPanel
from camera_panel import CameraPanel
from chart_panel import ChartPanel
from image_panel import ImagePanel
from commands import (
    handle_command,
    reminder_manager,
    set_brain_listener,
    set_camera_listener,
    set_chart_listener,
    set_highlight_listener,
    set_image_listener,
    set_news_listener,
    set_picture_listener,
    toggle_brain_view,
)
from hud import IDLE, LISTENING, SPEAKING, THINKING, Hud
from news_panel import NewsPanel
from speech import prewarm, set_amplitude_listener, speak
from voice import (
    arm_follow_up,
    listen,
    set_level_listener,
    set_status_listener,
    set_wake_listener,
)


# Set True to print how long each stage takes. Also enable the TIMING flags
# in speech.py, voice.py and transcriber.py.
TIMING = False

# Saying "Done, sir." after every action roughly doubles the talking. The
# window opening is its own confirmation, so this is off by default.
CONFIRM_SUCCESS = False

# How long to wait for an action before reporting on it. Closing a stubborn
# application can take a few seconds.
ACTION_TIMEOUT = 8.0

# Keep listening for a short follow-up after each command, so you need only
# say "Jarvis" once for a run of instructions. Set False to require the wake
# word every time.
FOLLOW_UP = True


def _greeting():
    """Good morning, afternoon, or evening, using your name if it is known.

    The wording is built in actions.memory so the name comes from the same
    place everything else about you is kept.
    """
    try:
        return memory.greeting()

    except Exception as error:
        print(f"[JARVIS] could not read memory: {error}")

        hour = datetime.now().hour

        if hour < 12:
            return "Good morning. JARVIS is online."

        if hour < 18:
            return "Good afternoon. JARVIS is online."

        return "Good evening. JARVIS is online."


class Assistant:
    """Runs the JARVIS loop on a worker thread and reports state to the HUD."""

    def __init__(self, hud):
        self._hud = hud
        self._stop = threading.Event()
        self._current_state = IDLE

    def stop(self):
        self._stop.set()

    def _state(self, state):
        self._current_state = state
        self._hud.state_changed.emit(state)

    def _heard(self, text):
        self._hud.heard_changed.emit(text)

    def _reply(self, text):
        self._hud.reply_changed.emit(text)

    def _say(self, text):
        self._reply(text)
        self._state(SPEAKING)
        speak(text)

    def _run_action(self, result):
        """Commands that do something: act while the confirmation plays."""
        outcome = {}

        def perform():
            try:
                outcome["success"] = result["action"]()
            except Exception as error:
                print(f"[JARVIS] action error: {error}")
                outcome["success"] = False

        # The action runs while JARVIS is still talking, so the window appears
        # as he finishes rather than a second afterwards.
        worker = threading.Thread(target=perform, daemon=True)
        worker.start()

        self._say(result["response"])

        worker.join(timeout=ACTION_TIMEOUT)

        success = outcome.get("success", False)

        if success:
            if CONFIRM_SUCCESS:
                self._say(phrases.pick("done"))
        elif result["intent"] in ("close_application", "close_project"):
            self._say(phrases.pick("cannot_close"))
        elif result["intent"] in ("open_application", "open_website", "open_project"):
            self._say(phrases.pick("cannot_open"))
        else:
            self._say(phrases.pick("failed"))

    def _run_query(self, result):
        """Commands that find something out: the action returns what to say."""
        self._state(THINKING)

        try:
            answer = result["action"]()
        except Exception as error:
            print(f"[JARVIS] query error: {error}")
            answer = None

        if answer:
            self._say(answer)
        else:
            self._say(phrases.pick("cannot_find"))

    def _on_alert(self, text):
        """Called from a reminder's own thread when one falls due.

        speech.speak() holds a lock, so this cannot talk over a reply in
        progress, and the listener discards audio while JARVIS speaks.
        """
        previous = self._current_state

        self._reply(text)
        self._state(SPEAKING)
        speak(text)
        self._state(previous)

    def _on_status(self, status):
        """Called by the listener when it starts or stops accepting a
        follow-up, including when the window expires mid-wait."""
        self._state(LISTENING if status == "listening" else IDLE)

    def _on_wake(self):
        """Called when JARVIS hears his name with no command attached."""
        self._state(LISTENING)
        self._heard("")
        self._say(phrases.pick("wake"))
        self._state(LISTENING)

    def run(self):
        set_wake_listener(self._on_wake)
        set_status_listener(self._on_status)
        reminder_manager.set_alert_listener(self._on_alert)

        # Battery warnings share the reminder announcer, so they queue
        # behind whatever JARVIS is already saying.
        battery_monitor.set_alert_listener(self._on_alert)
        battery_monitor.start()

        # Observations share the same announcer, so they queue behind
        # whatever JARVIS is already saying rather than talking over him.
        watcher.set_listener(self._on_alert)
        watcher.start()

        self._say(_greeting())

        # Anything noticed while JARVIS was closed is mentioned now, rather
        # than having been said to an empty room.
        try:
            missed = catch_up()

            if missed:
                self._say(missed)

        except Exception as error:
            print(f"[JARVIS] could not catch up: {error}")

        # The calendar is JARVIS's own, so it is read back whether or not
        # any calendar application exists on the machine.
        try:
            ahead = diary.briefing()

            if ahead:
                self._say(ahead)

        except Exception as error:
            print(f"[JARVIS] could not read the diary: {error}")

        # Warm the cache for stock replies while the greeting plays, so the
        # first "Done, sir." does not wait on a network round trip.
        threading.Thread(target=prewarm, daemon=True).start()

        while not self._stop.is_set():
            # The HUD state is driven by voice.set_status_listener, which
            # fires when the follow-up window actually opens or expires.
            self._heard("")
            self._reply("")

            _listen_started = time.monotonic()

            try:
                command = listen()
            except Exception as error:
                print(f"[JARVIS] listener error: {error}")
                continue

            _heard_at = time.monotonic()

            if self._stop.is_set():
                break

            if not command:
                continue

            self._heard(command)

            if command.lower().strip() == "quit":
                self._say("Shutting down.")
                break

            self._state(THINKING)

            try:
                started = time.monotonic()
                result = handle_command(command)

                if TIMING:
                    print(
                        f"[timing] interpret {time.monotonic() - started:.2f}s"
                    )
            except Exception as error:
                print(f"[JARVIS] command error: {error}")
                self._say(phrases.pick("wrong"))
                continue

            if not result:
                self._say(phrases.pick("unknown"))
                continue

            _handled_at = time.monotonic()

            if result.get("kind") == "query":
                self._run_query(result)
            else:
                self._run_action(result)

            if TIMING:
                _done_at = time.monotonic()
                print(
                    "[turn] listen+transcribe "
                    f"{_heard_at - _listen_started:.2f}s | "
                    f"decide {_handled_at - _heard_at:.2f}s | "
                    f"respond {_done_at - _handled_at:.2f}s | "
                    f"total after speaking {_done_at - _heard_at:.2f}s"
                )

            # Stay listening briefly so a follow-up needs no wake word.
            if FOLLOW_UP:
                arm_follow_up()

            self._state(IDLE)

        self._state(IDLE)
        reminder_manager.cancel_all()
        battery_monitor.stop()
        watcher.stop()
        camera.release()
        market_monitor.stop()
        self._hud.shutdown.emit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    hud = Hud()
    hud.show()

    # The news panel lives on the main thread with the HUD. The worker only
    # ever emits signals to it, which is the one thread-safe way to drive a
    # Qt widget from elsewhere.
    panel = NewsPanel()
    panel.set_anchor(hud)

    # A ray of light joining the two panels, in its own window because
    # nothing can be drawn in the gap between them otherwise.
    beam = Beam(panel, hud)

    # Charts get their own panel, projected the same way.
    chart = ChartPanel()
    chart.set_anchor(hud)
    chart_beam = Beam(chart, hud)

    def chart_update(data, title):
        if data:
            chart.show_chart.emit(data, title)
            chart_beam.shown.emit()
        else:
            chart.hide_chart.emit()
            chart_beam.hidden.emit()

    set_chart_listener(chart_update)

    # The camera view gets its own panel, projected like the others.
    view = CameraPanel()
    view.set_anchor(hud)
    view_beam = Beam(view, hud)

    def camera_update(data, caption):
        if data:
            view.show_view.emit(data, caption)
            view_beam.shown.emit()
        else:
            view.hide_view.emit()
            view_beam.hidden.emit()

    set_camera_listener(camera_update)

    # The fetched-image panel gets its own beam too, same as camera/chart.
    # Named "photo" rather than "picture" to avoid reading like the news
    # panel's unrelated picture.emit signal a few lines below.
    photo = ImagePanel()
    photo.set_anchor(hud)
    photo_beam = Beam(photo, hud)

    def image_update(data, title, caption, caption_link, scale):
        if data:
            photo.show_view.emit(data, title, caption, caption_link, scale)
            photo_beam.shown.emit()
        else:
            photo.hide_view.emit()
            photo_beam.hidden.emit()

    set_image_listener(image_update)

    # The mind view gets its own panel, projected like the others — but
    # unlike camera/chart it never receives a one-off image. It's fed the
    # HUD's own state/amplitude/level signals directly, so its pulses come
    # from the same real activity the ring itself reacts to.
    brain = BrainPanel()
    brain.set_anchor(hud)
    brain_beam = Beam(brain, hud)

    def brain_update(visible):
        if visible:
            brain.show_brain.emit()
            brain_beam.shown.emit()
        else:
            brain.hide_brain.emit()
            brain_beam.hidden.emit()

    set_brain_listener(brain_update)

    hud.state_changed.connect(brain.state_changed.emit)
    hud.amplitude_changed.connect(brain.amplitude_changed.emit)
    hud.level_changed.connect(brain.level_changed.emit)

    # Clicking the reactor core toggles the mind view. This runs on the Qt
    # main thread (a direct signal from the HUD's own click), so it can
    # call straight into commands rather than needing a worker thread.
    hud.core_clicked.connect(toggle_brain_view)

    def news_update(region, items):
        if region is None:
            panel.hide_news.emit()
            beam.hidden.emit()
        else:
            panel.show_news.emit(region, items or [])
            beam.shown.emit()

    set_news_listener(news_update)
    set_highlight_listener(panel.highlight.emit)
    set_picture_listener(panel.picture.emit)

    # Prices refresh in the background and appear along the foot of the news
    # panel, so the HUD itself stays uncluttered.
    market_monitor.set_listener(panel.markets.emit)
    market_monitor.start()

    # Feed the voice envelope to the ring. Emitting a signal is thread-safe,
    # which matters because playback runs on the worker/audio thread.
    set_amplitude_listener(hud.amplitude_changed.emit)

    # Microphone levels drive the waveform when JARVIS is not talking.
    set_level_listener(hud.level_changed.emit)

    assistant = Assistant(hud)

    hud.shutdown.connect(panel.hide_news.emit)
    hud.shutdown.connect(beam.hidden.emit)
    hud.shutdown.connect(chart.hide_chart.emit)
    hud.shutdown.connect(chart_beam.hidden.emit)
    hud.shutdown.connect(view.hide_view.emit)
    hud.shutdown.connect(view_beam.hidden.emit)
    hud.shutdown.connect(photo.hide_view.emit)
    hud.shutdown.connect(photo_beam.hidden.emit)
    hud.shutdown.connect(brain.hide_brain.emit)
    hud.shutdown.connect(brain_beam.hidden.emit)
    hud.shutdown.connect(lambda: QTimer.singleShot(400, app.quit))
    hud.closed.connect(assistant.stop)

    worker = threading.Thread(target=assistant.run, daemon=True)
    worker.start()

    exit_code = app.exec()

    assistant.stop()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
