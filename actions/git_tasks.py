"""Reading a git repository, and committing when told to.

This is the first thing in JARVIS that can change a repository, so it is
deliberately the narrowest useful version of that. Everything here reads,
with exactly one exception: commit, which only ever runs after the user
has heard the message and said yes.

What it will never do, by construction rather than by prompt:

- run `git add`, `push`, `reset`, `checkout`, `rebase`, or anything else
  that moves work around. Staging stays the user's job, which means
  JARVIS can only ever commit exactly what the user already chose.
- build a command from anything spoken. Every git call is a fixed
  argument list; the only value that ever varies is the commit message,
  which is passed as a single argument and never through a shell.
- run in a folder that isn't a git repository, checked before each call.

The commit message itself is drafted by the language model from the
staged diff, so this is one of the few features here that costs a call
-- and the only one that sends your code off the machine. That is worth
knowing plainly rather than discovering later.
"""

import os
import subprocess

from dotenv import load_dotenv

from actions import memory, safety


load_dotenv()

# Where the repository lives. Set in .env as JARVIS_REPO, because a
# Windows path is close to impossible to dictate reliably -- backslashes,
# drive letters and casing are exactly what transcription mangles. Saying
# "remember my repo is ..." still works and takes precedence, for
# switching repositories mid-session without editing a file.
REPO_ENV = "JARVIS_REPO"


# Anything longer than this is almost certainly a diff of generated
# files or a vendored dependency, not something a commit message can
# usefully summarise. Truncated rather than refused, since a partial
# summary of a huge change still beats nothing.
MAX_DIFF_CHARS = 12_000

# How long any single git call may take before it's abandoned. Git is
# local and fast; anything hanging this long is stuck, not working.
TIMEOUT_SECONDS = 15


def _repo():
    """The configured repository folder, or None if unusable.

    Spoken memory wins over the .env setting, so a repository can be
    switched mid-session without editing a file -- but .env is the
    setting that survives, and the one worth having for a path that's
    painful to dictate.
    """
    path = memory.repo_path() or os.getenv(REPO_ENV)

    if not path:
        return None

    path = os.path.expanduser(path.strip().strip('"').strip("'"))

    if not os.path.isdir(path):
        return None

    return path


def configured_path():
    """Whatever is currently configured, valid or not -- for telling the
    user what's actually set when something is wrong."""
    return memory.repo_path() or os.getenv(REPO_ENV)


def _run(args, path):
    """One git call. Returns (ok, output).

    args is always a list -- never a string, and never assembled from
    anything spoken -- so there is no shell for anything to be injected
    into.
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=path,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)

    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "").strip()

    return True, (result.stdout or "").strip()


def is_repository(path=None):
    """True when the folder is actually a git repository."""
    path = path or _repo()

    if not path:
        return False

    ok, output = _run(["rev-parse", "--is-inside-work-tree"], path)

    return ok and output.strip() == "true"


def available():
    """True when there's a usable repository configured."""
    return is_repository()


def staged_files():
    """Names of the currently staged files, or []."""
    path = _repo()

    if not path:
        return []

    ok, output = _run(["diff", "--cached", "--name-only"], path)

    if not ok or not output:
        return []

    return [line.strip() for line in output.splitlines() if line.strip()]


def staged_diff():
    """The staged diff as text, truncated, or "" when nothing is staged."""
    path = _repo()

    if not path:
        return ""

    ok, output = _run(["diff", "--cached"], path)

    if not ok or not output:
        return ""

    if len(output) > MAX_DIFF_CHARS:
        return output[:MAX_DIFF_CHARS] + "\n[diff truncated]"

    return output


def no_repo_message():
    """Why there's no usable repository, specifically.

    "I don't know which folder" is misleading when a path IS set but
    points somewhere wrong -- that's the case most likely to happen,
    and the one where a vague message costs the most time.
    """
    configured = configured_path()

    if not configured:
        return (
            "I don't know which folder is your repository, sir. "
            f"Set {REPO_ENV} in your .env file."
        )

    if not os.path.isdir(os.path.expanduser(configured.strip())):
        spoken = memory.repo_path()
        source = (
            "You told me your repo is"
            if spoken and spoken.strip() == configured.strip()
            else f"{REPO_ENV} is set to"
        )

        return (
            f"That folder doesn't exist, sir. {source} "
            f"{safety.clean(configured, 80)}."
        )

    return "That folder isn't a git repository, sir."


def describe_status():
    """A spoken summary of what's staged and what isn't."""
    path = _repo()

    if not path or not is_repository(path):
        return no_repo_message()

    staged = staged_files()

    ok, unstaged = _run(["diff", "--name-only"], path)
    unstaged_count = len(
        [line for line in (unstaged or "").splitlines() if line.strip()]
    ) if ok else 0

    # Untracked files don't appear in `git diff` at all, so a brand new
    # file would otherwise be invisible here -- which is exactly the
    # thing worth mentioning before a commit.
    ok, untracked = _run(
        ["ls-files", "--others", "--exclude-standard"], path
    )
    untracked_count = len(
        [line for line in (untracked or "").splitlines() if line.strip()]
    ) if ok else 0

    ok, branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], path)
    branch_name = safety.clean(branch, 40) if ok else "an unknown branch"

    if not staged and not unstaged_count and not untracked_count:
        return f"Nothing to commit on {branch_name}, sir."

    parts = []

    if staged:
        word = "file" if len(staged) == 1 else "files"
        parts.append(f"{len(staged)} {word} staged")

    if unstaged_count:
        word = "file" if unstaged_count == 1 else "files"
        parts.append(f"{unstaged_count} {word} changed but not staged")

    if untracked_count:
        word = "file" if untracked_count == 1 else "files"
        parts.append(f"{untracked_count} new {word} not yet tracked")

    return f"On {branch_name}, sir: " + ", and ".join(parts) + "."


def commit(message):
    """Commit the staged changes. Returns (ok, spoken).

    The message is passed as one argument in a list, never interpolated
    into a shell string, so its content cannot become part of the
    command however it was worded.
    """
    path = _repo()

    if not path:
        return False, no_repo_message()

    if not staged_files():
        return False, "There's nothing staged to commit, sir."

    message = (message or "").strip()

    if not message:
        return False, "I don't have a message to commit with, sir."

    ok, output = _run(["commit", "-m", message], path)

    if not ok:
        detail = safety.clean(output, 120)

        return False, f"The commit failed, sir. {detail}"

    ok, short = _run(["rev-parse", "--short", "HEAD"], path)
    reference = f" as {short}" if ok and short else ""

    return True, f"Committed{reference}, sir."
