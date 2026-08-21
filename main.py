import sys
import threading
import time
from datetime import datetime

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from actions.battery import battery_monitor
from commands import handle_command, reminder_manager
from hud import IDLE, LISTENING, SPEAKING, THINKING, Hud
from speech import prewarm, set_amplitude_listener, speak
from voice import (
    arm_follow_up,
    listen,
    set_level_listener,
    set_status_listener,
    set_wake_listener,
)


# Set True to print how long each stage takes. Also enable speech.TIMING.
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
    """Good morning, afternoon, or evening, depending on the actual hour."""
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
                self._say("Done, sir.")
        elif result["intent"] in ("close_application", "close_project"):
            self._say("I couldn't close that, sir.")
        elif result["intent"] in ("open_application", "open_website", "open_project"):
            self._say("I couldn't open that, sir.")
        else:
            self._say("That didn't work, sir.")

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
            self._say("I couldn't find that out, sir.")

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
        self._say("Yes, sir?")
        self._state(LISTENING)

    def run(self):
        set_wake_listener(self._on_wake)
        set_status_listener(self._on_status)
        reminder_manager.set_alert_listener(self._on_alert)

        # Battery warnings share the reminder announcer, so they queue
        # behind whatever JARVIS is already saying.
        battery_monitor.set_alert_listener(self._on_alert)
        battery_monitor.start()

        self._say(_greeting())

        # Warm the cache for stock replies while the greeting plays, so the
        # first "Done, sir." does not wait on a network round trip.
        threading.Thread(target=prewarm, daemon=True).start()

        while not self._stop.is_set():
            # The HUD state is driven by voice.set_status_listener, which
            # fires when the follow-up window actually opens or expires.
            self._heard("")
            self._reply("")

            try:
                command = listen()
            except Exception as error:
                print(f"[JARVIS] listener error: {error}")
                continue

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
                self._say("Something went wrong, sir.")
                continue

            if not result:
                self._say("I don't know how to do that yet.")
                continue

            if result.get("kind") == "query":
                self._run_query(result)
            else:
                self._run_action(result)

            # Stay listening briefly so a follow-up needs no wake word.
            if FOLLOW_UP:
                arm_follow_up()

            self._state(IDLE)

        self._state(IDLE)
        reminder_manager.cancel_all()
        battery_monitor.stop()
        self._hud.shutdown.emit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    hud = Hud()
    hud.show()

    # Feed the voice envelope to the ring. Emitting a signal is thread-safe,
    # which matters because playback runs on the worker/audio thread.
    set_amplitude_listener(hud.amplitude_changed.emit)

    # Microphone levels drive the waveform when JARVIS is not talking.
    set_level_listener(hud.level_changed.emit)

    assistant = Assistant(hud)

    # Reminders fire on their own thread and speak through the assistant.

    hud.shutdown.connect(lambda: QTimer.singleShot(400, app.quit))
    hud.closed.connect(assistant.stop)

    worker = threading.Thread(target=assistant.run, daemon=True)
    worker.start()

    exit_code = app.exec()

    assistant.stop()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
