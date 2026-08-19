import sys
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from commands import handle_command
from hud import IDLE, LISTENING, SPEAKING, THINKING, Hud
from speech import set_amplitude_listener, speak
from voice import listen


class Assistant:
    """Runs the JARVIS loop on a worker thread and reports state to the HUD."""

    def __init__(self, hud):
        self._hud = hud
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _state(self, state):
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
        """Commands that do something: confirm, act, then report."""
        self._say(result["response"])
        self._state(THINKING)

        try:
            success = result["action"]()
        except Exception as error:
            print(f"[JARVIS] action error: {error}")
            success = False

        if success:
            self._say("Done, sir.")
        elif result["intent"] == "close_application":
            self._say("I couldn't close the application, sir.")
        else:
            self._say("I couldn't open that, sir.")

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

    def run(self):
        self._say("Good evening. JARVIS is online.")

        while not self._stop.is_set():
            self._state(LISTENING)
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
                result = handle_command(command)
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

            self._state(IDLE)

        self._state(IDLE)
        self._hud.shutdown.emit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    hud = Hud()
    hud.show()

    # Feed the voice envelope to the ring. Emitting a signal is thread-safe,
    # which matters because playback runs on the worker/audio thread.
    set_amplitude_listener(hud.amplitude_changed.emit)

    assistant = Assistant(hud)

    hud.shutdown.connect(lambda: QTimer.singleShot(400, app.quit))
    hud.closed.connect(assistant.stop)

    worker = threading.Thread(target=assistant.run, daemon=True)
    worker.start()

    exit_code = app.exec()

    assistant.stop()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
