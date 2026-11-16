"""Runs the read-only arbitrage scanner in its own subprocess, using the Rust executable."""
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_lock = threading.Lock()
_process = None


def arm(log_callback, status_callback):
    """Start the scanner; its output lines go to [log_callback]."""
    global _process
    with _lock:
        if _process is not None:
            return

        # Point to your new Rust executable!
        exe_path = Path(r"C:\rust_arb\target\release\rust_arb.exe")

        if not exe_path.exists():
            log_callback(
                f"[ARB] Rust executable not found at {exe_path}. Did you run cargo build?")
            status_callback("STOPPED")
            return

        status_callback("ARMING")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        flags = getattr(subprocess, "CREATE_NO_WINDOW",
                        0) if sys.platform == "win32" else 0

        process = subprocess.Popen(
            [str(exe_path)],  # Run the Rust .exe directly
            cwd=str(ROOT),   # Run from JARVIS folder so it finds the .env file
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=flags
        )
        _process = process

    status_callback("ARMED")

    def read_output():
        global _process
        for line in process.stdout:
            log_callback(line.rstrip())
        code = process.wait()
        with _lock:
            if _process is process:
                _process = None
        if code not in (0, None) and code > 0:
            log_callback(f"[ARB] scanner exited (code {code})")
        status_callback("STOPPED")

    threading.Thread(target=read_output, daemon=True).start()


def disarm(status_callback):
    """Stop the scanner if it is running."""
    global _process
    with _lock:
        process, _process = _process, None
    if process is None:
        return
    status_callback("STOPPING")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    status_callback("STOPPED")
