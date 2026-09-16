"""Redirect the JARVIS folder to a temporary one, for the whole test run.

Every suite that touches memory, collections or subjects must import this
before anything else. Without it, a test writes to the real
C:\\Users\\<you>\\JARVIS -- and on 16 September 2026 one did exactly that,
replacing a real cars collection with fixture values and emptying the
history.

Mocking individual functions is not enough and was what failed. A suite
patched memory_collections._ensure_data and assumed nothing else reached
disk; a save further down the call chain wrote the fixture dict over the
real file. Redirecting the folder itself is the only version of this that
cannot be got round, because every path in the codebase is built from
files.root().

Usage, as the first import after sys.path is set up:

    from tools import sandbox  # noqa: E402  (must precede actions imports)

    sandbox.activate()

activate() is idempotent and lasts for the life of the process. The
directory is removed when it exits.
"""

import atexit
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_directory = None
_real_root = None


def directory():
    """The sandbox folder, or None when activate() has not been called."""
    return _directory


def activate():
    """Point files.root() at a temporary folder for this process.

    Returns the folder. Safe to call more than once.
    """
    global _directory, _real_root

    if _directory is not None:
        return _directory

    from actions import files

    holder = tempfile.mkdtemp(prefix="jarvis-test-")

    _real_root = files.root
    _directory = holder

    files.root = lambda: holder

    # Anything that imported root by name rather than calling through the
    # module keeps its own reference, so those are redirected too.
    for name, module in list(sys.modules.items()):
        if not name.startswith("actions."):
            continue

        if getattr(module, "root", None) is _real_root:
            module.root = files.root

    atexit.register(_cleanup)

    return holder


def seed(name, text):
    """Write a file into the sandbox, e.g. a memory.txt a suite expects."""
    if _directory is None:
        raise RuntimeError("sandbox.activate() has not been called")

    path = os.path.join(_directory, name)

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)

    return path


def assert_clean():
    """Fail loudly if the real JARVIS folder was written to anyway.

    Cheap insurance. A suite that still reaches the real folder through a
    path this module has not covered shows up here rather than in a user's
    lost collection.
    """
    if _real_root is None:
        return True

    real = _real_root()

    if not real or not os.path.isdir(real):
        return True

    marker = os.path.join(real, ".jarvis-test-wrote-here")

    if os.path.exists(marker):
        print(
            f"a test wrote to the real JARVIS folder at {real}; "
            "the sandbox did not cover every path"
        )

        return False

    return True


def _cleanup():
    global _directory

    if _directory is None:
        return

    import shutil

    try:
        shutil.rmtree(_directory, ignore_errors=True)
    except Exception:
        pass

    _directory = None
