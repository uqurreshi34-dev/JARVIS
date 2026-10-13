"""Spawns the MEV scanner in its own venv, and streams its output.

Never passes the parent environment through: the scanner reads its own
.env from disk. The stdout goes to listeners; the panel sanitises it.
"""
import os
import subprocess
import sys
import threading
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VENV_DIR = _PROJECT_ROOT / ".venv-mev"

if sys.platform == "win32":
    _VENV_PY = _VENV_DIR / "Scripts" / "python.exe"
else:
    _VENV_PY = _VENV_DIR / "bin" / "python"

_process = None
_lock = threading.Lock()
_listeners = []


def set_listener(fn):
    """Register a callable taking (kind, text).

    kind is "line" for a stdout line, "status" for a state change.
    """
    _listeners.append(fn)


def _emit(kind, text):
    for fn in list(_listeners):
        try:
            fn(kind, text)
        except Exception as e:
            print(f"[mev_runner] listener error: {e}")


def is_running():
    with _lock:
        return _process is not None and _process.poll() is None


def _safe_env():
    """A minimal environment for the subprocess.

    No inheritance: the scanner reads its own .env from disk, so it does
    not need anything from the parent's environment. On Windows the
    runtime needs SYSTEMROOT to start; PATH and TEMP for general use.
    """
    keys = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE")
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def arm():
    """Start the MEV subprocess. Returns (ok, message)."""
    global _process

    with _lock:
        if _process is not None and _process.poll() is None:
            return False, "already running"

        if not _VENV_PY.exists():
            _emit("line", f"[mev_runner] venv not found at {_VENV_PY}")
            _emit("line", "[mev_runner] run: py -3.12 -m venv .venv-mev")
            return False, "missing venv"

        env_file = _PROJECT_ROOT / ".env"
        if not env_file.exists():
            _emit("line", f"[mev_runner] .env not found at {env_file}")
            return False, "missing .env"

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        _emit("status", "ARMING")
        try:
            _process = subprocess.Popen(
                [str(_VENV_PY), "-u", "-m", "actions.mev.main"],
                cwd=str(_PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=_safe_env(),
                creationflags=creationflags,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            _process = None
            _emit("line", f"[mev_runner] could not start: {e}")
            _emit("status", "IDLE")
            return False, str(e)

        _emit("status", "ARMED")
        threading.Thread(target=_drain, daemon=True).start()
        return True, "armed"


def disarm():
    """Terminate the subprocess. Returns (ok, message)."""
    global _process

    with _lock:
        p = _process
        if p is None or p.poll() is not None:
            _process = None
            _emit("status", "STOPPED")
            return False, "not running"
        _emit("status", "STOPPING")

    try:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
    except Exception as e:
        _emit("line", f"[mev_runner] disarm error: {e}")

    with _lock:
        _process = None
    _emit("status", "STOPPED")
    return True, "stopped"


def _drain():
    global _process

    with _lock:
        p = _process

    if p is None or p.stdout is None:
        return

    try:
        for line in p.stdout:
            _emit("line", line.rstrip("\n"))
    except Exception:
        pass

    with _lock:
        _process = None
    _emit("status", "STOPPED")
