import sys
import threading
import time
from datetime import datetime

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from actions import camera, contacts, diary, memory, documents
from actions.battery import battery_monitor
from actions.watch import watcher, catch_up
import phrases
from actions.markets import market_monitor
from actions.patterns import pattern_monitor
from phone import phone_server
from beam import Beam
from brain_panel import BrainPanel
from camera_panel import CameraPanel
from chart_panel import ChartPanel
from image_choices_panel import ImageChoicesPanel
from image_panel import ImagePanel
import commands
from actions import routing_guard

routing_guard.install(commands)

from commands import (
    handle_command,
    look_at_phone_picture,
    reminder_manager,
    select_image_choice,
    set_brain_listener,
    set_camera_listener,
    set_chart_listener,
    set_choices_listener,
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

from pathlib import Path


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

        # Held for the phone as well as said aloud. Every unprompted
        # announcement passes through here -- battery, disk, market
        # alerts, pattern runs, the morning diary -- so this one line
        # covers all of them. Still spoken to the room regardless,
        # since being at the desk is still the normal case.
        phone_server.announce(text)

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

    def _on_pattern_suggestion(self, pattern):
        """Speak a newly detected pattern suggestion once."""
        result = commands.offer_pattern(pattern)

        if result:
            self._run_query(result)

    def _on_pattern_due(self, patterns):
        """Run patterns already confirmed for automatic execution."""
        for pattern in patterns:
            result = commands.run_pattern(pattern)

            if result:
                if result.get("kind") == "query":
                    self._run_query(result)
                else:
                    self._run_action(result)

    def _on_phone_command(self, text):
        """Run a command that came from the phone, and return the words.

        Deliberately does not go through _run_query/_run_action: those
        call _say(), which plays through the PC speakers. Someone
        holding their phone in another room does not want their desk
        talking to an empty room -- the reply belongs to the device
        that asked for it. The HUD is still updated, so the desk shows
        what happened even though it stays quiet.
        """
        self._heard(text)
        self._state(THINKING)

        try:
            result = handle_command(text)
        except Exception as error:
            print(f"[JARVIS] phone command error: {error}")
            self._state(IDLE)

            return phrases.pick("wrong")

        # Calling, WhatsApp and texting can only happen on the device
        # holding the SIM. commands.py cannot know which device asked,
        # so it answers for the desk; here we do know, and replace that
        # with the link the phone should open.

        if result and result.get("intent") == "phone_action":
            prepared = contacts.prepare(
                result.get("application"),
                result.get("text") or "",
                result.get("project"),
            )

            self._reply(prepared["spoken"])
            self._state(IDLE)

            return prepared

        if not result:
            self._state(IDLE)

            return phrases.pick("unknown")

        try:
            if result.get("kind") == "query":
                spoken = result["action"]() or phrases.pick("cannot_find")
            else:
                # An action's confirmation is fixed up front; the work
                # itself still has to run.
                spoken = result.get("response") or phrases.pick("done")
                result["action"]()
        except Exception as error:
            print(f"[JARVIS] phone action error: {error}")
            spoken = phrases.pick("failed")

        self._reply(spoken)
        self._state(IDLE)

        return spoken

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

        pattern_monitor.set_due_listener(self._on_pattern_due)
        pattern_monitor.set_suggestion_listener(self._on_pattern_suggestion)
        pattern_monitor.start()

        # A phone on the same network drives the same assistant. The
        # handler runs on the server's own thread, which is why
        # handle_command serialises itself -- see _command_lock.
        phone_server.set_handler(self._on_phone_command)
        phone_server.set_look_handler(look_at_phone_picture)
        phone_server.start()

        self._say(_greeting())

        # Anything noticed while JARVIS was closed is mentioned now, rather
        # than having been said to an empty room.
        try:
            missed = catch_up()

            if missed:
                # Announced as well as said. These call _say() directly
                # rather than going through _on_alert, so without this
                # they reach the desk and never the phone -- and they
                # are exactly the "what did I miss" content the phone
                # queue exists for. The queue holds them, so opening
                # the phone later still shows what was said at startup.
                phone_server.announce(missed)
                self._say(missed)

        except Exception as error:
            print(f"[JARVIS] could not catch up: {error}")

        # The calendar is JARVIS's own, so it is read back whether or not
        # any calendar application exists on the machine.
        try:
            ahead = diary.briefing()

            if ahead:
                phone_server.announce(ahead)
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
        pattern_monitor.stop()
        phone_server.stop()
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

    # The three-candidate picker gets its own panel and beam too. Its
    # click path runs straight into select_image_choice rather than
    # through handle_command, on the Qt main thread — same shape as the
    # reactor-core click above.
    choices = ImageChoicesPanel()
    choices.set_anchor(hud)
    choices_beam = Beam(choices, hud)

    def choices_update(items):
        if items:
            choices.show_choices.emit(items)
            choices_beam.shown.emit()
        else:
            choices.hide_choices.emit()
            choices_beam.hidden.emit()

    set_choices_listener(choices_update)
    choices.choice_clicked.connect(select_image_choice)

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

    def on_files_dropped(paths):
        def load():
            try:
                result = documents.add_paths(paths)

                if result:
                    hud.reply_changed.emit(result)
            except Exception as exc:
                hud.reply_changed.emit(
                    f"I couldn't load the documents: {exc}"
                )

        threading.Thread(target=load, daemon=True).start()

    hud.files_dropped.connect(on_files_dropped)

    hud.state_changed.connect(brain.state_changed.emit)
    hud.amplitude_changed.connect(brain.amplitude_changed.emit)
    hud.level_changed.connect(brain.level_changed.emit)

    def on_documents_changed():
        hud.documents_changed.emit(documents.count())

    documents.set_listener(on_documents_changed)

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
    hud.shutdown.connect(choices.hide_choices.emit)
    hud.shutdown.connect(choices_beam.hidden.emit)
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
