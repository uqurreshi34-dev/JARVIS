"""Offline tests for which provider errors should move to the next provider.

No network, no model, no pool. These exercise one question: when a
request to a provider fails, does the error tell us anything about
whether a *different* provider would have succeeded?

Both directions matter, and they fail in opposite ways.

A missed rotation is the expensive one: a 500 or a timeout from one
provider killed the whole command while a healthy provider sat idle,
which is the opposite of the point of having a pool.

A false rotation is quieter but worse to debug. If JARVIS's own mistake
-- a malformed schema, a bad argument -- looks like a transient fault,
every provider is tried in turn, each fails identically, and the real
bug is buried under a rotation log instead of being raised where it
happened.

providers.py is imported for its classifiers only. Importing the module
normally would build the provider pool and need real keys, so the
classifier block is compiled on its own.
"""

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_classifiers():
    """Compile just the error classifiers out of providers.py."""
    source = (ROOT / "providers.py").read_text(encoding="utf-8")

    start = source.index("def is_rate_limit")
    end = source.index("def _claude_schema")

    module = types.ModuleType("failover_classifiers")
    exec(compile(source[start:end], "providers.py", "exec"), module.__dict__)

    return module


# Stand-ins for the SDK exception types. The real ones carry a
# status_code attribute and a class name that says what went wrong, and
# those are the two things the classifiers read.
class _Timeout(Exception):
    pass


class _APIConnectionError(Exception):
    pass


class _InternalServerError(Exception):
    status_code = 500


class _BadRequestError(Exception):
    status_code = 400


# (error, should_rotate, description)
_CASES = (
    # Transient -- the whole point of this change.
    (_InternalServerError("upstream exploded"), True, "500 status code"),
    (_Timeout("Request timed out."), True, "timeout by class name"),
    (_APIConnectionError("Connection error."), True, "dropped connection"),
    (Exception("503 Service Unavailable"), True, "503 in the message"),
    (Exception("502 Bad Gateway"), True, "502 in the message"),
    (Exception("the model is overloaded, try again"), True, "overloaded"),

    # Already handled before this change; here so a future edit to the
    # classifiers cannot quietly drop them.
    (Exception("429 rate_limit_exceeded"), True, "rate limit"),
    (Exception("404 model_not_found"), True, "model retired"),
    (Exception("403 forbidden"), True, "no access to this model"),

    # Must NOT rotate. These are facts about the request, not the
    # provider, and every provider would reject them the same way.
    (
        _BadRequestError("max_tokens must be less than or equal to 500"),
        False,
        "400 whose text happens to contain 500",
    ),
    (
        Exception("response_format json_schema unsupported"),
        False,
        "schema rejection, handled by its own retry",
    ),
    (ValueError("tool name missing"), False, "a bug in JARVIS itself"),
)


def main():
    providers = _load_classifiers()
    failures = []

    for error, expected, description in _CASES:
        actual = providers.should_failover(error)

        if actual != expected:
            failures.append(
                f"{description}: should_failover returned {actual}, "
                f"expected {expected} ({type(error).__name__}: {error})"
            )

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    rotating = sum(1 for _, expected, _ in _CASES if expected)

    print(
        f"PASSED: {rotating} error shapes rotate to the next provider, "
        f"{len(_CASES) - rotating} correctly do not."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
