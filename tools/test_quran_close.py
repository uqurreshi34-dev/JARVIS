"""Closing the Quran works by voice and by button, running or finished.

The bug this guards: once a recitation had finished, the page stayed up
and nothing could take it away. The page's Stop button called stop(),
which only ends a session that is still running; the HUD stop button
returned early because nothing was being said; and "close the quran"
had no intent of its own, so the fuzzy table read it as "close the
brain". The only way out was to start a new recitation and stop that.

Four layers, each tested for real:

  - quran.dismissed, against what people say and what they do not mean
  - the fast path, so those phrases reach close_quran and nothing else
  - recitation.dismiss, with and without a session running
  - the page itself, drawn off-screen and its Stop button clicked

No sound is made, no network is used and no microphone is opened.
"""

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()



def _import(name):
    """Import a JARVIS module, standing in for anything this OS lacks.

    On the Windows machine JARVIS runs on, everything imports for real
    and nothing is replaced. Elsewhere, pywin32, the voice libraries and
    friends are missing, and each one that is gets a stand-in so the
    code under test can still be reached.
    """
    for _ in range(40):
        before = set(sys.modules)

        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as error:
            missing = error.name

            if not missing or missing in sys.modules:
                raise

            # Drop JARVIS's own half-imported modules so the retry starts
            # clean. Third-party ones stay: numpy and its kind refuse to
            # be loaded twice in one process.
            for loaded in set(sys.modules) - before:
                path = getattr(sys.modules.get(loaded), "__file__", "") or ""

                if Path(path).resolve().is_relative_to(ROOT):
                    sys.modules.pop(loaded, None)

            sys.modules[missing] = MagicMock()

    raise RuntimeError(f"{name} needs more than can be stood in for")


# Nothing here plays a sound, so the audio device is never wanted -- and
# replacing it means no machine's sound card can fail the test.
sys.modules["sounddevice"] = MagicMock()

quran = _import("actions.quran")
recitation = _import("actions.recitation")


# What people say to put it away, and the shapes those take.
CLOSE = (
    "close the quran",
    "close quran",
    "stop reciting",
    "close recitation",
    "close the recitation",
    "stop the recitation",
    "end the recitation",
    "finish reciting",
    "hide the quran",
    "dismiss the quran",
    "exit the quran",
    "quit the quran",
    "cancel the recitation",
    "close the koran",
    "close the Qur'an.",
    "Close, the Quran!",
    "stop playing the holy quran",
    "stop the surah",
    "close surah",
    "turn the quran off",
    "switch the recitation off",
    "put the quran away",
    "shut the quran down",
    "get rid of the quran",
    "can you close the quran please",
    "jarvis stop reciting now",
)

# Things that mention one half or both and are not asking for anything
# to close.
KEEP = (
    "recite surah 36",
    "play surah 112",
    "close the brain",
    "close the radar",
    "stop",
    "what does the quran say about patience",
    "does the quran say to stop lying",
    "how does the recitation end",
    "the quran is beautiful",
    "stop worrying about what everyone in the world thinks of the quran",
    "",
    None,
)


def check_phrases(failures):
    for said in CLOSE:
        if not quran.dismissed(said):
            failures.append(f"not read as closing: {said!r}")

    for said in KEEP:
        if quran.dismissed(said):
            failures.append(f"wrongly read as closing: {said!r}")


def check_routing(failures):
    try:
        commands = _import("commands")
    except Exception as error:
        failures.append(f"commands.py could not be imported: {error!r}")
        return

    for said in CLOSE:
        result = commands._fast_path(said)
        intent = result and result.get("intent")

        if intent != "close_quran":
            failures.append(f"{said!r} routed to {intent!r}, not close_quran")

    expected = {
        "recite surah 111": "recite_quran",
        "close the brain": "hide_brain",
        "close the radar": "aircraft_hide",
    }

    for said, wanted in expected.items():
        result = commands._fast_path(said)
        intent = result and result.get("intent")

        if intent != wanted:
            failures.append(f"{said!r} routed to {intent!r}, not {wanted}")

    llm = _import("llm")

    source = Path(llm.__file__).read_text(encoding="utf-8")

    if '"close_quran"' not in source:
        failures.append("the model cannot choose close_quran")


class FakeSession:
    def __init__(self):
        self.running = True
        self.stopped = False

    @property
    def active(self):
        # Mirrors Session.active: a running session that is not winding up.
        return self.running

    def stop(self):
        self.stopped = True
        self.running = False


def check_dismiss(failures):
    hides = []
    recitation.set_listeners(on_hide=lambda: hides.append(True))

    # Finished: no session running, the page still up. This is the case
    # that used to do nothing at all.
    finished = FakeSession()
    finished.running = False
    recitation._current = finished

    if recitation.dismiss() is not False:
        failures.append("dismiss claimed to stop a finished recitation")

    if not hides:
        failures.append("dismiss left the page up after a recitation finished")

    # Nothing ever recited.
    recitation._current = None
    hides.clear()

    recitation.dismiss()

    if not hides:
        failures.append("dismiss with nothing recited did not put the page away")

    # Running: stopped, and the page goes too.
    running = FakeSession()
    recitation._current = running
    hides.clear()

    if recitation.dismiss() is not True:
        failures.append("dismiss did not report a running recitation stopped")

    if not running.stopped:
        failures.append("dismiss left a running recitation playing")

    if not hides:
        failures.append("dismiss stopped a recitation but left the page up")

    # on_hide must never leak into a session's own listeners, which
    # Session() would reject.
    if "on_hide" in recitation._listeners:
        failures.append("on_hide was passed through to sessions")

    # A broken listener is reported, not raised into a button handler.
    recitation.set_listeners(on_hide=lambda: 1 / 0)

    try:
        recitation.dismiss()
    except Exception:
        failures.append("a failing hide listener broke dismiss")

    recitation._current = None
    recitation.set_listeners(on_hide=lambda: None)


def check_page(failures):
    from PyQt6.QtWidgets import QApplication, QPushButton

    app = QApplication.instance() or QApplication([])

    quran_panel = _import("quran_panel")

    page = quran_panel.QuranPanel()

    # Wired the way main.py wires it.
    recitation.set_listeners(on_hide=page.dismissed.emit)
    page.stop_requested.connect(lambda: recitation.dismiss())

    finished = FakeSession()
    finished.running = False
    recitation._current = finished

    page.show()
    app.processEvents()

    buttons = [b for b in page.findChildren(QPushButton) if b.text() == "Stop"]

    if not buttons:
        failures.append("the page has no Stop button")
        return

    buttons[0].click()
    app.processEvents()

    if page.isVisible():
        failures.append("Stop did not close the page after a recitation finished")

    # A spoken close from another thread arrives as a queued signal.
    page.show()
    app.processEvents()

    import threading

    worker = threading.Thread(target=recitation.dismiss)
    worker.start()
    worker.join()

    app.processEvents()

    if page.isVisible():
        failures.append("a close from a command thread left the page up")

    # A stopped session still closes it through ended, as before.
    page.show()
    app.processEvents()
    page.ended.emit("stopped")
    app.processEvents()

    if page.isVisible():
        failures.append("a stopped recitation no longer closes the page")

    # A finished one still leaves it up, as before.
    page.show()
    app.processEvents()
    page.ended.emit("finished")
    app.processEvents()

    if not page.isVisible():
        failures.append("finishing a verse took the page away")

    page.hide()
    recitation._current = None
    recitation.set_listeners(on_hide=lambda: None)


def check_hud_stop(failures):
    """The HUD stop button, silent: the recitation page still goes."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("    def stop_speaking(self):")
    end = source.index("\n    def ", start + 1)

    namespace = {"recitation": MagicMock(), "stop_speech": lambda: False,
                 "print": lambda *a, **k: None}
    body = "\n".join(line[4:] for line in source[start:end].splitlines())
    exec(body, namespace)

    turn = types.SimpleNamespace(_in_turn=True, _turn_stopped=MagicMock())
    namespace["stop_speaking"](turn)

    if not namespace["recitation"].dismiss.called:
        failures.append("the HUD stop button ignored a page left up")

    if turn._turn_stopped.set.called:
        failures.append("a silent HUD stop ended the turn")


def main():
    failures = []

    check_phrases(failures)
    check_dismiss(failures)
    check_page(failures)
    check_hud_stop(failures)
    check_routing(failures)

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(f"PASSED: {len(CLOSE)} ways of closing reach close_quran, "
          f"{len(KEEP)} lookalikes do not, and both stop buttons close "
          f"the page whether the recitation is running or finished.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
