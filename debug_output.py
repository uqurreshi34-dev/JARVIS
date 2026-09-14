import queue
import sys
import threading
import time


_QUEUE = queue.SimpleQueue()
_INSTALLED = False
_WORKER = None


class _AsyncOutput:
    """Queue text immediately and render it from a background thread."""

    def __init__(self, target):
        self._target = target

    def write(self, text):
        if text:
            _QUEUE.put((self._target, text))

        return len(text)

    def flush(self):
        # Deliberately non-blocking.
        # Existing print(..., flush=True) remains compatible,
        # but does not force the calling thread to render console output.
        return None

    def isatty(self):
        try:
            return self._target.isatty()
        except (AttributeError, OSError):
            return False

    def fileno(self):
        return self._target.fileno()

    @property
    def encoding(self):
        return getattr(self._target, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._target, "errors", "strict")


def _writer():
    """Render queued output away from JARVIS's command thread."""
    while True:
        target, text = _QUEUE.get()

        try:
            target.write(text)

            while not _QUEUE.empty():
                next_target, next_text = _QUEUE.get()
                next_target.write(next_text)

            target.flush()

        except (AttributeError, OSError):
            pass


def install():
    """Install asynchronous stdout/stderr without changing existing prints."""
    global _INSTALLED, _WORKER

    if _INSTALLED:
        return

    stdout = sys.stdout
    stderr = sys.stderr

    if stdout is None and stderr is None:
        return

    sys.stdout = _AsyncOutput(stdout) if stdout is not None else None
    sys.stderr = _AsyncOutput(stderr) if stderr is not None else None

    _WORKER = threading.Thread(
        target=_writer,
        name="jarvis-debug-output",
        daemon=True,
    )
    _WORKER.start()

    _INSTALLED = True
