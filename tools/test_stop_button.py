"""The HUD stop button silences JARVIS, sends him to standby, and touches nothing else.

Three layers, each tested for real:

  - speech.py, with a simulated audio device that plays at real speed
  - main.Assistant's turn handling, with speech and listening simulated
  - hud.Hud, drawn off-screen and clicked

No sound is made and no microphone is opened.
"""

import os
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()


# --- a simulated audio device ------------------------------------------------

class CallbackStop(Exception):
    pass


class FakeOutputStream:
    """Calls the callback in blocks, at roughly real playback speed."""

    played = 0

    def __init__(self, samplerate, channels, blocksize, dtype, callback):
        self._callback = callback
        self._blocksize = blocksize or 1024
        self._interval = self._blocksize / float(samplerate)
        self.active = False
        self._thread = None

    def _run(self):
        out = np.zeros((self._blocksize, 1), dtype=np.float32)

        while self.active:
            FakeOutputStream.played += self._blocksize

            try:
                self._callback(out, self._blocksize, None, None)
            except CallbackStop:
                break

            time.sleep(self._interval)

        self.active = False

    def __enter__(self):
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.active = False
        self._thread.join(timeout=2)


def _install_fake_audio():
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.CallbackStop = CallbackStop
    fake_sd.OutputStream = FakeOutputStream
    fake_sd.RawInputStream = MagicMock()
    fake_sd.sleep = lambda ms: time.sleep(ms / 1000.0)
    sys.modules["sounddevice"] = fake_sd

    for name in ("edge_tts", "soundfile"):
        sys.modules.setdefault(name, MagicMock())


class FakeEngine:
    """pyttsx3 as far as speech.py uses it: words fire callbacks."""

    last = None

    def __init__(self):
        self._callbacks = []
        self._text = ""
        self.stopped = False
        self.words_spoken = 0
        FakeEngine.last = self

    def setProperty(self, *_args):
        pass

    def connect(self, name, callback):
        self._callbacks.append(callback)

    def say(self, text):
        self._text = text

    def runAndWait(self):
        for index, _word in enumerate(self._text.split()):
            if self.stopped:
                return

            for callback in self._callbacks:
                callback("utterance", index, 1)

            if self.stopped:
                return

            self.words_spoken += 1
            time.sleep(0.02)

    def stop(self):
        self.stopped = True


def _check_speech(failures):
    _install_fake_audio()

    fake_pyttsx3 = types.ModuleType("pyttsx3")
    fake_pyttsx3.init = FakeEngine
    sys.modules["pyttsx3"] = fake_pyttsx3

    import speech

    engine = speech.speech
    rate = 24000
    long_reply = " ".join(
        f"Sentence number {index} about the C drive and its folders."
        for index in range(40)
    )

    def two_seconds(text):
        return np.zeros(rate * 2, dtype=np.float32), rate, []

    reports = []
    speech.set_speaking_listener(reports.append)

    pressed_at = {}

    def press_after(seconds):
        def press():
            time.sleep(seconds)
            pressed_at["played"] = FakeOutputStream.played
            reports.append(("pressed", speech.stop_speaking()))

        threading.Thread(target=press, daemon=True).start()

    # Nothing to stop.
    if speech.stop_speaking():
        failures.append("stop_speaking claimed to stop silence")

    # A long chunked reply stops within a block or two.
    failed_before = speech._neural_failed_at

    with patch.object(engine, "_audio_for", side_effect=two_seconds), \
         patch.object(engine, "_is_cached", return_value=True), \
         patch.object(engine, "_speak_fallback") as fallback, \
         patch.object(engine, "_warm_quietly"):
        if len(speech._split_for_speech(long_reply)) < 2:
            failures.append("the test reply was not long enough to chunk")

        FakeOutputStream.played = 0
        started = time.monotonic()
        press_after(0.3)
        engine.speak(long_reply)
        took = time.monotonic() - started

        if took > 1.0:
            failures.append(f"a stopped reply kept going for {took:.2f}s")

        after = FakeOutputStream.played - pressed_at.get("played", 0)

        if after > 3 * speech._BLOCK:
            failures.append(
                f"{after} samples played after the stop, over three blocks"
            )

        if fallback.called:
            failures.append("the fallback voice finished a stopped reply")

        if speech._neural_failed_at != failed_before:
            failures.append("a stop was recorded as a neural voice failure")

        if speech.is_speaking():
            failures.append("still marked as speaking after a stop")

        if not speech.stopped():
            failures.append("stopped() did not report the stop")

        flags = [report for report in reports if isinstance(report, bool)]

        if flags[:2] != [True, False]:
            failures.append(f"the HUD was not told start and end: {reports}")

        # The next utterance is not affected by the last stop.
        FakeOutputStream.played = 0
        engine.speak("Done, sir.")

        if FakeOutputStream.played < rate * 2:
            failures.append(
                f"the utterance after a stop was cut short: "
                f"{FakeOutputStream.played} of {rate * 2} samples"
            )

        if speech.stopped():
            failures.append("the stop flag outlived its utterance")

    # A stop while the audio is still being made returns at once.
    def slow(text):
        time.sleep(3)
        return np.zeros(rate, dtype=np.float32), rate, []

    speech._neural_busy.clear()

    with patch.object(engine, "_audio_for", side_effect=slow), \
         patch.object(engine, "_is_cached", return_value=True), \
         patch.object(engine, "_speak_fallback") as fallback:
        started = time.monotonic()
        press_after(0.2)
        engine.speak("A short reply.")
        took = time.monotonic() - started

        if took > 1.0:
            failures.append(f"a stop during synthesis waited {took:.2f}s")

        if fallback.called:
            failures.append("a stop during synthesis fell back to SAPI")

    # Let the abandoned synthesis finish before the next section.
    deadline = time.monotonic() + 5

    while speech._neural_busy.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)

    # The fallback voice stops at the next word.
    with patch.object(engine, "_speak_neural", side_effect=RuntimeError("offline")):
        press_after(0.15)
        engine.speak(long_reply)

        spoken = FakeEngine.last.words_spoken if FakeEngine.last else 0

        if not FakeEngine.last or not FakeEngine.last.stopped:
            failures.append("the fallback voice was not stopped")

        if spoken >= len(long_reply.split()):
            failures.append("the fallback voice read the whole reply")

    speech.set_speaking_listener(None)
    speech._neural_failed_at = 0.0


# --- the turn -----------------------------------------------------------------

def _load_main():
    """Import main.py with its heavy neighbours replaced."""
    for name in (
        "commands", "news_panel", "phone", "camera_panel", "chart_panel",
        "image_panel", "image_choices_panel", "brain_panel", "beam",
        "transcriber", "agent", "outlook",
    ):
        sys.modules.setdefault(name, MagicMock())

    voice = types.ModuleType("voice")

    for name in (
        "arm_follow_up", "consume_follow_up_answer", "disarm", "listen",
        "set_follow_up_expired_listener", "set_level_listener",
        "set_status_listener", "set_wake_listener",
    ):
        setattr(voice, name, MagicMock())

    sys.modules["voice"] = voice

    sys.modules.setdefault("actions.phone_server", MagicMock())

    try:
        import main
    except Exception as error:
        return None, f"main.py could not be imported for testing: {error!r}"

    return main, None


class FakeHud:
    def __init__(self):
        for name in (
            "state_changed", "heard_changed", "reply_changed", "shutdown",
        ):
            setattr(self, name, MagicMock())


def _check_turn(failures):
    main, problem = _load_main()

    if main is None:
        failures.append(problem)
        return

    said = []
    events = {}

    def fake_speak(text):
        said.append(text)

        # The long reply is where the button gets pressed.
        if text.startswith("Your C drive"):
            events["stop_result"] = assistant.stop_speaking()

    def run_turn(result, queued=()):
        said.clear()
        commands_heard = iter(["inspect my c drive"])

        def fake_listen():
            try:
                return next(commands_heard)
            except StopIteration:
                assistant._stop.set()
                return None

        assistant._queued_alerts[:] = list(queued)

        with patch.object(main, "speak", side_effect=fake_speak), \
             patch.object(main, "stop_speech", return_value=True), \
             patch.object(main, "listen", side_effect=fake_listen), \
             patch.object(main, "consume_follow_up_answer", return_value=False), \
             patch.object(main, "handle_command", return_value=result), \
             patch.object(main, "disarm") as disarm, \
             patch.object(main, "arm_follow_up") as arm, \
             patch.object(main, "FOLLOW_UP", True), \
             patch.object(main, "prewarm"), \
             patch.object(main, "phone_server", MagicMock()), \
             patch.object(main, "_greeting", return_value="Hello."), \
             patch.object(main, "catch_up", return_value=None):
            _run_loop_only(main, assistant)

        return disarm, arm

    assistant = main.Assistant(FakeHud())

    # A query: the answer is ready before speaking, and the stop ends it.
    query = {
        "kind": "query",
        "intent": "inspect_drive",
        "action": lambda: "Your C drive has 400 gigabytes free. " * 20,
    }

    disarm, arm = run_turn(query, queued=["Bitcoin is up 0.2 percent, sir."])

    if events.get("stop_result") is not None:
        failures.append("stop_speaking returned a value it should not")

    if not assistant._turn_stopped.is_set() and not disarm.called:
        failures.append("the stop was not recorded against the turn")

    if not disarm.called:
        failures.append("a stopped turn did not return to standby")

    if arm.called:
        failures.append("a stopped turn opened a follow-up window")

    if "Bitcoin is up 0.2 percent, sir." not in said:
        failures.append(f"a queued announcement was not read out: {said}")

    # An action: it carries on; only what would have been said is dropped.
    finished = threading.Event()

    def slow_action():
        time.sleep(0.3)
        finished.set()
        return True

    action = {
        "kind": "action",
        "intent": "organise_folder",
        "action": slow_action,
        "response": "Your C drive is being tidied, sir. " * 10,
        "success_response": "All tidy, sir.",
        "timeout": 5,
    }

    run_turn(action)

    if not finished.is_set():
        failures.append("the action did not run to completion after a stop")

    if "All tidy, sir." in said:
        failures.append("the success line was spoken after a stop")

    # Outside a turn, a stop only cuts that announcement off.
    assistant._in_turn = False
    assistant._turn_stopped.clear()

    with patch.object(main, "stop_speech", return_value=True):
        assistant.stop_speaking()

    if assistant._turn_stopped.is_set():
        failures.append("a stop outside a turn silenced the next turn")

    # With nothing being said, the button does nothing at all.
    assistant._in_turn = True

    with patch.object(main, "stop_speech", return_value=False):
        assistant.stop_speaking()

    if assistant._turn_stopped.is_set():
        failures.append("a stop with nothing playing ended the turn")

    assistant._in_turn = False


def _run_loop_only(main, assistant):
    """Run Assistant.run with its start-up work and monitors stubbed."""
    source = main.Assistant.run

    with patch.object(main, "set_wake_listener"), \
         patch.object(main, "set_status_listener"), \
         patch.object(main, "set_follow_up_expired_listener"), \
         patch.object(main, "watcher", MagicMock()), \
         patch.object(main, "battery_monitor", MagicMock(), create=True), \
         patch.object(main, "pattern_monitor", MagicMock(), create=True), \
         patch.object(main, "reminder_manager", MagicMock()), \
         patch.object(main, "folder_guard", MagicMock(), create=True), \
         patch.object(main, "camera", MagicMock(), create=True), \
         patch.object(main, "market_monitor", MagicMock(), create=True):
        assistant._stop.clear()
        source(assistant)


# --- the HUD ------------------------------------------------------------------

def _check_hud(failures):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtGui import QImage
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    import hud

    app = QApplication.instance() or QApplication([])
    widget = hud.Hud()

    stops = []
    cores = []
    widget.stop_clicked.connect(lambda: stops.append(True))
    widget.core_clicked.connect(lambda: cores.append(True))

    centre = hud._STOP_RECT.center().toPoint()

    def red_at_button():
        image = QImage(widget.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        widget.render(image)
        colour = image.pixelColor(centre)
        return colour.red() > 180 and colour.green() < 140

    # Hidden and inert while he is not speaking.
    widget.speaking_changed.emit(False)
    app.processEvents()

    if red_at_button():
        failures.append("the stop button shows while JARVIS is silent")

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=centre)

    if stops:
        failures.append("a click on the hidden button stopped speech")

    # Shown and working while he is.
    widget.speaking_changed.emit(True)
    app.processEvents()

    if not red_at_button():
        failures.append("the stop button did not appear while speaking")

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=centre)

    if len(stops) != 1:
        failures.append("a click on the stop button did nothing")

    if cores:
        failures.append("the stop button also toggled the mind view")

    # The core still toggles the mind view.
    QTest.mouseClick(
        widget, Qt.MouseButton.LeftButton, pos=hud._CENTRE.toPoint(),
    )

    if len(cores) != 1 or len(stops) != 1:
        failures.append("the core click was affected by the stop button")

    # A drag that starts on the button moves the HUD, it does not stop.
    start = widget.pos()
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=centre)
    QTest.mouseMove(widget, centre + QPoint(40, 30))
    QTest.mouseRelease(
        widget, Qt.MouseButton.LeftButton, pos=centre + QPoint(40, 30),
    )

    if len(stops) != 1:
        failures.append("dragging from the button stopped speech")

    # Clear of the reactor's outer ring.
    corner = hud._STOP_HIT.bottomRight()
    distance = ((corner.x() - hud._CENTRE.x()) ** 2
                + (corner.y() - hud._CENTRE.y()) ** 2) ** 0.5

    if distance <= hud._R_OUTER:
        failures.append("the stop button overlaps the reactor rings")

    widget.close()


def main():
    failures = []

    for check in (_check_speech, _check_turn, _check_hud):
        try:
            check(failures)
        except Exception as error:
            import traceback

            traceback.print_exc()
            failures.append(f"{check.__name__} crashed: {error!r}")

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: the stop button cuts speech off at once, only while he is "
        "speaking, returns him to standby, leaves actions running, and "
        "still reads out queued announcements."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
