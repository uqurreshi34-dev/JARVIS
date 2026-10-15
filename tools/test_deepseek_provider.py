"""DeepSeek as a provider: last of the free-first fallbacks, thinking off unless asked for (providers.py).

    python tools/test_deepseek_provider.py

No network: the client is replaced by one that records the request. Checked:

- with Claude primary, the fallback order is Groq, Gemini, then DeepSeek;
- DeepSeek is asked for deepseek-flash with thinking switched off by default -- hidden reasoning is billed
  as output and delays a spoken answer -- and DEEPSEEK_THINKING switches it on at a given effort;
- it has no vision model, so pictures never go to it;
- a spent balance (402) moves the request to the next provider instead of failing the command.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The pool is built when providers.py is imported, from the keys present. Every provider gets a stand-in key,
# Claude is made primary, and DEEPSEEK_THINKING is left unset, before the import.
for name, value in (("LLM_PROVIDER", "claude"), ("GROQ_API_KEY", "test"), ("GEMINI_API_KEY", "test"),
                    ("DEEPSEEK_API_KEY", "test"), ("ANTHROPIC_FOUNDRY_API_KEY", "test"),
                    ("ANTHROPIC_FOUNDRY_BASE_URL", "https://foundry.test/anthropic")):
    os.environ[name] = value

os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DEEPSEEK_THINKING", None)
os.environ.pop("DEEPSEEK_MODEL", None)

import providers  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


class Client:
    """Records each request and answers it, or raises what it is given."""

    def __init__(self, answer="Paris, sir.", error=None):
        self.asked = []
        self.answer, self.error = answer, error
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.asked.append(kwargs)

        if self.error:
            raise self.error

        message = types.SimpleNamespace(content=self.answer)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)


def deepseek(thinking=None):
    with patch.dict(os.environ, {} if thinking is None else {"DEEPSEEK_THINKING": thinking}):
        provider = providers.Provider("deepseek", providers.PROVIDERS["deepseek"], "test")

    provider._client = Client()
    return provider


check([p.name for p in providers._pool] == ["claude", "groq", "gemini", "deepseek"],
      f"Claude first, then Groq, Gemini and DeepSeek ({[p.name for p in providers._pool]})")
check(providers.PROVIDERS["deepseek"]["base_url"] == "https://api.deepseek.com",
      "DeepSeek's OpenAI-format address")

with patch.object(providers.usage, "record"):
    plain = deepseek()
    said = plain.chat([{"role": "user", "content": "What is the capital of France?"}])
    sent = plain._client.asked[-1]
    check(said == "Paris, sir." and sent["model"] == "deepseek-flash", "asked of deepseek-flash, and answered")
    check(sent.get("extra_body") == {"thinking": {"type": "disabled"}} and "reasoning_effort" not in sent,
          f"thinking off by default ({sent.get('extra_body')})")

    thoughtful = deepseek("high")
    thoughtful.chat([{"role": "user", "content": "Why is the sky blue?"}])
    sent = thoughtful._client.asked[-1]
    check(sent.get("extra_body") == {"thinking": {"type": "enabled"}} and sent.get("reasoning_effort") == "high",
          "DEEPSEEK_THINKING=high switches thinking on at high")

    thoughtful.chat([{"role": "user", "content": "Plan it."}], reasoning_effort="max")
    check(thoughtful._client.asked[-1].get("reasoning_effort") == "max",
          "with thinking on, a caller asking for an effort gets it")

    check(deepseek("very").thinking == "off", "a setting it does not know leaves thinking off")

check(plain.vision_model is None, "no vision model, so pictures go to the others")


class Refuses(Client):
    """Refuses the thinking switch once, as a provider that did not know it would."""

    def create(self, **kwargs):
        self.asked.append(kwargs)

        if "extra_body" in kwargs:
            raise Exception("Error code: 400 - unknown parameter: thinking")

        message = types.SimpleNamespace(content=self.answer)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)


refused = deepseek()
refused._client = Refuses()

with patch.object(providers.usage, "record"):
    check(refused.chat([{"role": "user", "content": "Hello"}]) == "Paris, sir."
          and "extra_body" not in refused._client.asked[-1],
          "a refused thinking setting is dropped and the question asked again without it")

# A spent balance moves the request on.
spent = Exception("Error code: 402 - {'error': {'message': 'Insufficient Balance'}}")
first = deepseek()
first._client = Client(error=spent)
second = deepseek()
second.name = "next"
second._client = Client(answer="Still here, sir.")

with patch.object(providers, "_pool", [first, second]), patch.object(providers.usage, "record"):
    check(providers.chat([{"role": "user", "content": "Hello"}]) == "Still here, sir.",
          "a 402 'Insufficient Balance' moves to the next provider")
    check(first.resting, "and the spent one rests rather than being asked again at once")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
