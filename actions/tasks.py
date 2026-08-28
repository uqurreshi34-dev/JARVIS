"""Named commands you defined, run on request.

The important property here is what this is NOT: it is not a shell. It
cannot run anything you did not already write down yourself, and nothing
spoken ever becomes part of a command.

The only thing speech does is pick a name out of `tasks.txt`. The command
that name maps to was written by hand, by you, in a file you can read.
That matters because transcription is imperfect -- "delete the old
branch" is one mishearing away from something unrecoverable -- and this
design means a mishearing can only ever pick the wrong task from your own
list, or no task at all. It can never compose a new command.

Tasks are defined the same way patterns are, one field per line:

    task: tests
    run: pytest -q
    confirm: no

    task: deploy
    run: scripts/deploy.bat staging
    confirm: yes

`confirm` defaults to yes when missing, so a task you forgot to think
about asks before running rather than just going.
"""

import os
import re
import subprocess
import threading
from datetime import datetime

from actions import files, safety


FILENAME = "tasks.txt"

# How long a task may run before it's given up on. Generous, because a
# build or a test suite legitimately takes minutes -- but not unbounded,
# because a task waiting on input nobody will ever type would otherwise
# hang forever.
TIMEOUT_SECONDS = 600

# How much of the output to keep in the spoken summary. The full output
# always goes to a file regardless; this is only what gets read aloud.
SUMMARY_LINES = 3

_LINE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.+)$", re.IGNORECASE)

_lock = threading.Lock()
_listener = None


def set_listener(listener):
    """Register a callable taking (name, ok, spoken) when a task ends."""
    global _listener

    _listener = listener


def _path():
    base = files.root()

    return os.path.join(base, FILENAME) if base else None


def _read_lines():
    path = _path()

    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return [
                line.rstrip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError as error:
        print(f"[JARVIS] could not read tasks: {error}")
        return []


def tasks():
    """Every defined task, as a list of dicts with name/run/confirm."""
    found = []
    current = None

    for line in _read_lines():
        match = _LINE.match(line)

        if not match:
            continue

        key = match.group(1).strip().casefold()
        value = match.group(2).strip()

        if key == "task":
            if current and current.get("run"):
                found.append(current)

            current = {"name": value.casefold(), "spoken": value}
        elif current is not None:
            current[key] = value

    if current and current.get("run"):
        found.append(current)

    return found


def names():
    """Just the task names, for matching against speech."""
    return [task["name"] for task in tasks()]


def find(spoken):
    """The task a spoken phrase refers to, or None.

    Matched loosely against the names you defined -- "run the tests"
    should find a task called "tests" -- but only ever against that
    list. A phrase matching nothing returns None rather than guessing.
    """
    wanted = (spoken or "").strip().casefold()

    if not wanted:
        return None

    defined = tasks()

    for task in defined:
        if task["name"] == wanted:
            return task

    # Then containment either way, longest name first so "deploy
    # staging" wins over "deploy" when both exist.
    for task in sorted(defined, key=lambda t: -len(t["name"])):
        if task["name"] in wanted or wanted in task["name"]:
            return task

    return None


def needs_confirmation(task):
    """True unless the task explicitly said it doesn't need asking.

    Defaults to asking. A task you added without thinking about this
    should be the cautious one, not the silent one.
    """
    setting = (task.get("confirm") or "yes").strip().casefold()

    return setting not in ("no", "false", "never", "0")


def _output_path(name):
    base = files.root()

    if not base:
        return None

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "task"

    return os.path.join(base, f"task-{safe}-{stamp}.txt")


def _write_output(name, command, result, path):
    """Everything the task printed, kept in full."""
    if not path:
        return None

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"Task: {name}\n")
            handle.write(f"Command: {command}\n")
            handle.write(
                f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            handle.write(f"Exit code: {result.returncode}\n")
            handle.write("=" * 60 + "\n\n")

            if result.stdout:
                handle.write(result.stdout)

            if result.stderr:
                handle.write("\n--- errors ---\n")
                handle.write(result.stderr)

        return path
    except OSError as error:
        print(f"[JARVIS] could not write the task output: {error}")
        return None


def _summarise(task, result, output_path):
    """What to say when a task finishes."""
    name = task["spoken"]
    ok = result.returncode == 0

    where = ""

    if output_path:
        where = " The full output is in your JARVIS folder."

    if ok:
        tail = _last_useful_lines(result.stdout)

        if tail:
            return f"{name} finished, sir. {tail}{where}"

        return f"{name} finished, sir.{where}"

    tail = _last_useful_lines(result.stderr or result.stdout)

    if tail:
        return f"{name} failed, sir. {tail}{where}"

    return (
        f"{name} failed with exit code {result.returncode}, sir.{where}"
    )


def _last_useful_lines(text):
    """The last few meaningful lines, cleaned up for speech.

    Build tools put the part you care about at the end -- the summary
    line, the error, the count -- so the tail is far more useful than
    the head.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line and not line.startswith("=")]

    if not lines:
        return ""

    tail = safety.clean(" ".join(lines[-SUMMARY_LINES:]), 200)

    if not tail:
        return ""

    # Ends a sentence, so what follows doesn't run into it when spoken.
    return tail if tail.endswith((".", "!", "?")) else f"{tail}."


def run(task):
    """Run one task to completion. Returns (ok, spoken).

    Blocking: the caller decides whether to run this on its own thread.
    """
    command = task.get("run")

    if not command:
        return False, f"{task['spoken']} has no command, sir."

    base = files.root()
    working_directory = task.get("in") or base

    if working_directory and not os.path.isdir(working_directory):
        working_directory = base

    try:
        # shell=True is deliberate, and safe here for one specific
        # reason: this string came out of a file you wrote by hand, and
        # nothing spoken is ever interpolated into it. Speech only ever
        # selects a name from your own list. Without a shell, ordinary
        # things people put in a task file -- npm scripts on Windows,
        # pipes, && -- simply would not work.
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"{task['spoken']} was still running after "
            f"{TIMEOUT_SECONDS // 60} minutes, sir. I stopped waiting."
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{task['spoken']} couldn't start, sir. {error}"

    path = _output_path(task["name"])
    written = _write_output(task["name"], command, result, path)

    return result.returncode == 0, _summarise(task, result, written)


def run_in_background(task):
    """Start a task and report when it finishes, via the listener.

    Builds and test suites take minutes; blocking the assistant for
    that long would make it useless meanwhile.
    """
    def worker():
        ok, spoken = run(task)

        if _listener:
            try:
                _listener(task["spoken"], ok, spoken)
            except Exception as error:
                print(f"[JARVIS] task listener failed: {error}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    return thread


def describe():
    """A spoken list of what tasks are defined."""
    defined = tasks()

    if not defined:
        return (
            "You haven't defined any tasks yet, sir. Add them to "
            "tasks.txt in your JARVIS folder."
        )

    spoken = [task["spoken"] for task in defined]

    if len(spoken) == 1:
        return f"One task, sir: {spoken[0]}."

    listed = ", ".join(spoken[:-1]) + f", and {spoken[-1]}"

    return f"{len(spoken)} tasks, sir: {listed}."
