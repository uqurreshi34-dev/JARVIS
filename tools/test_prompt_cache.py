"""Offline tests for prompt caching on Claude requests.

No network, no model. A stand-in client records exactly what JARVIS would
send, so these check the request itself:

- the system prompt goes out marked for caching, with its text unchanged;
- every Claude path (chat, vision with a system prompt, agent turns) does it;
- an endpoint that refuses caching gets the same request without it, once,
  and is never asked with it again during the run;
- any other error is raised as before, not swallowed by the retry;
- usage.py counts the cache reads and writes the response reports.

    python tools/test_prompt_cache.py
"""

import os
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Build a Claude provider with a dummy key and no .env, so nothing real is
# contacted and the test does not depend on this PC's settings.
os.environ["ANTHROPIC_FOUNDRY_API_KEY"] = "test-key"
os.environ["ANTHROPIC_FOUNDRY_BASE_URL"] = "https://example.invalid/anthropic"
os.environ["LLM_PROVIDER"] = "claude"

# Keep the usage ledger out of the real JARVIS folder.
_home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = _home
os.environ["HOME"] = _home

import usage  # noqa: E402

usage._folder = lambda: _home

import providers  # noqa: E402


class _Usage:
    input_tokens = 12
    output_tokens = 5
    cache_read_input_tokens = 5900
    cache_creation_input_tokens = 0


class _Response:
    usage = _Usage()
    content = [types.SimpleNamespace(type="text", text='{"ok": true}')]


class _Stream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.response


class _Messages:
    def __init__(self, refuse=None):
        self.sent = []
        self.refuse = refuse

    def _take(self, kwargs):
        self.sent.append(kwargs)

        if self.refuse and self.refuse(kwargs):
            raise RuntimeError(self.refuse.message)

        return _Response()

    def stream(self, **kwargs):
        return _Stream(self._take(kwargs))

    def create(self, **kwargs):
        return self._take(kwargs)


def _provider(refuse=None):
    claude = next(p for p in providers._pool if p.name == "claude")
    claude._cache_system = True
    claude._client = types.SimpleNamespace(messages=_Messages(refuse))
    return claude


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


SYSTEM = "You are the command interpreter. " * 400
MESSAGES = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "open chrome"}]


def cached(kwargs):
    system = kwargs.get("system")
    return (
        isinstance(system, list)
        and system[0].get("cache_control") == {"type": "ephemeral"}
        and system[0].get("text") == SYSTEM.strip()
    )


# Chat
claude = _provider()
claude._chat_anthropic(MESSAGES)
check(cached(claude._client.messages.sent[-1]), "chat sends the system prompt marked for caching, text unchanged")

# Vision with a system prompt
claude = _provider()
claude._vision_chat_anthropic(
    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "what is this"}],
    image_bytes=b"\x89PNG fake", mime="image/png", max_tokens=100,
)
check(cached(claude._client.messages.sent[-1]), "vision with a system prompt is cached too")

# Agent turn
claude = _provider()
claude.agent_turn([{"role": "user", "content": "hi"}], tools=[], system=SYSTEM.strip())
check(cached(claude._client.messages.sent[-1]), "agent turns cache their system prompt")

# Refused by the endpoint: retried once without caching, then never again
refusal = lambda kwargs: isinstance(kwargs.get("system"), list)  # noqa: E731
refusal.message = "Error code: 400 - extra inputs are not permitted: system.0.cache_control"
claude = _provider(refusal)
answer = claude._chat_anthropic(MESSAGES)
sent = claude._client.messages.sent
check(answer == '{"ok": true}', "a refused cache still gets the answer")
check(len(sent) == 2 and sent[1]["system"] == SYSTEM.strip(), "retried once with the plain system prompt")
claude._chat_anthropic(MESSAGES)
check(len(sent) == 3 and sent[2]["system"] == SYSTEM.strip(), "later requests skip caching for the rest of the run")

# Any other error is raised, not retried
other = lambda kwargs: True  # noqa: E731
other.message = "Error code: 529 - overloaded"
claude = _provider(other)
try:
    claude._chat_anthropic(MESSAGES)
    raised = False
except RuntimeError:
    raised = True
check(raised and len(claude._client.messages.sent) == 1, "other errors are raised as before, with no extra request")

# The ledger counts cache reads
claude = _provider()
claude._chat_anthropic(MESSAGES)
report = usage.summary(1)
check(report["cache_read"] >= 5900, "usage.py counts cache reads from the response")

sys.exit(1 if failures else 0)
