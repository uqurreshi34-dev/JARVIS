"""DeepSeek as a provider: last of the free-first fallbacks, thinking off unless asked for (providers.py).

    python tools/test_deepseek_provider.py

No network: the client is replaced by one that records the request. Checked:

- with Claude primary, the fallback order is Groq, Gemini, then DeepSeek;
- DeepSeek is asked for deepseek-flash with thinking switched off by default -- hidden reasoning is billed
  as output and delays a spoken answer -- and DEEPSEEK_THINKING switches it on at a given effort;
- it has no vision model, so pictures never go to it;
- a spent balance (402) moves the request to the next provider instead of failing the command;
- a structured request (the command interpreter's strict json_schema) is sent as DeepSeek's json_object with
  the schema written into the system prompt -- its JSON mode takes no schema and needs the word "json" in the
  prompt, which the interpreter's prompt never had -- while Groq and Gemini keep the strict schema;
- every provider waits only so long for a reply (its own limit, longer for a request allowed to write a lot)
  and its client retries once, not twice, so a stalled provider hands over to the next quickly;
- tool-calling turns keep DeepSeek's thinking off whatever DEEPSEEK_THINKING says: with it on, DeepSeek
  refuses later turns that do not send its reasoning back.
"""

import json

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

import openai  # noqa: E402
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

SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}
STRICT = {"type": "json_schema", "json_schema": {"name": "jarvis_command", "strict": True, "schema": SCHEMA}}
ASKED = [{"role": "system", "content": "You are JARVIS's interpreter."},
         {"role": "user", "content": "open notepad"}]

with patch.object(providers.usage, "record"):
    structured = deepseek()
    structured._client = Client(answer='{"intent": "open_app"}')
    structured.chat(ASKED, response_format=STRICT)
    sent = structured._client.asked[-1]
    system = sent["messages"][0]["content"]
    check(sent.get("response_format") == {"type": "json_object"},
          f"DeepSeek is asked for json_object, not the json_schema it refuses ({sent.get('response_format')})")
    check(system.startswith("You are JARVIS's interpreter.") and "json" in system.casefold()
          and json.dumps(SCHEMA, separators=(",", ":")) in system,
          "with the schema, and the word json, added to the end of the system prompt")
    check(ASKED[0]["content"] == "You are JARVIS's interpreter.", "the caller's own messages are left untouched")

    bare = deepseek()
    bare._client = Client(answer='{"intent": "open_app"}')
    bare.chat([{"role": "user", "content": "open notepad"}], response_format=STRICT)
    first = bare._client.asked[-1]["messages"][0]
    check(first["role"] == "system" and "JSON Schema" in first["content"],
          "with no system prompt, one carrying the schema is put first")

    groq = providers.Provider("groq", providers.PROVIDERS["groq"], "test")
    groq._client = Client(answer='{"intent": "open_app"}')
    groq.chat(ASKED, response_format=STRICT)
    check(groq._client.asked[-1].get("response_format") == STRICT
          and groq._client.asked[-1]["messages"] == ASKED,
          "Groq still gets the strict schema, and the prompt as it was")


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

# ---- how long each provider is waited for ---------------------------------------------------------
fresh = providers.Provider("deepseek", providers.PROVIDERS["deepseek"], "test")
check(fresh.timeout == 45 and fresh._client.max_retries == providers._CLIENT_RETRIES == 1
      and float(fresh._client.timeout) == 45,
      f"DeepSeek is waited for 45 seconds, and its client retries once ({fresh._client.timeout}, "
      f"{fresh._client.max_retries})")
check(providers.Provider("groq", providers.PROVIDERS["groq"], "test").timeout == 30,
      "each provider has its own limit: Groq, the quickest, 30 seconds")

with patch.dict(os.environ, {"DEEPSEEK_TIMEOUT_SECONDS": "20"}):
    check(providers.Provider("deepseek", providers.PROVIDERS["deepseek"], "test").timeout == 20,
          "DEEPSEEK_TIMEOUT_SECONDS sets it")

with patch.dict(os.environ, {"DEEPSEEK_TIMEOUT_SECONDS": "soon"}):
    check(providers.Provider("deepseek", providers.PROVIDERS["deepseek"], "test").timeout == 45,
          "and a value that is not a number leaves the default")

with patch.object(providers.usage, "record"):
    timed = deepseek()
    timed.chat([{"role": "user", "content": "Capital of France?"}])
    check(timed._client.asked[-1]["timeout"] == 45, "a short question is waited for 45 seconds")
    timed.chat([{"role": "user", "content": "Write the report."}], max_tokens=6000)
    check(timed._client.asked[-1]["timeout"] == 45 + 6000 / providers._SLOW_TOKENS_PER_SECOND,
          "a request allowed to write 6,000 tokens is given time to write them")

    agent = deepseek("high")
    agent._client = Client()
    agent.agent_turn([{"role": "user", "content": "check my calendar"}], tools=[], max_tokens=1800)
    sent = agent._client.asked[-1]
    check(sent.get("extra_body") == {"thinking": {"type": "disabled"}} and "reasoning_effort" not in sent,
          "a tool-calling turn keeps thinking off even with DEEPSEEK_THINKING=high")
    check(sent.get("timeout") == 45 + 1800 / providers._SLOW_TOKENS_PER_SECOND, "and has its limit too")


# A provider that stalls past its limit hands over to the next.
class Stalls(Client):
    def create(self, **kwargs):
        self.asked.append(kwargs)
        raise openai.APITimeoutError(request=None)


stalled = deepseek()
stalled._client = Stalls()
backup = deepseek()
backup.name = "next"
backup._client = Client(answer="Here instead, sir.")

with patch.object(providers, "_pool", [stalled, backup]), patch.object(providers.usage, "record"):
    check(providers.chat([{"role": "user", "content": "Hello"}]) == "Here instead, sir.",
          "a provider that times out hands the question to the next")

# ---- every failover says why, in the provider's own words ------------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402

limited = deepseek()
limited.name = "groq"
limited._client = Client(error=Exception(
    "Error code: 429 - Rate limit reached for model openai/gpt-oss-20b on tokens per minute (TPM): "
    "Limit 8000, Used 7200, Requested 6400. Key gsk_abcdefghijklmnop1234"))
answers = deepseek()
answers.name = "gemini"
answers._client = Client(answer="Juventus, sir.")
printed = io.StringIO()

with patch.object(providers, "_pool", [limited, answers]), patch.object(providers.usage, "record"), \
        contextlib.redirect_stdout(printed):
    providers.chat([{"role": "user", "content": "Most successful Italian team?"}])

line = printed.getvalue()
check("groq rate limited; switching to gemini -- Exception: Error code: 429" in line
      and "tokens per minute" in line,
      f"the failover line carries the provider's own reason ({line.strip()[:160]})")
check("gsk_" not in line and "[key]" in line, "with anything that looks like a key blanked out")

long_error = Exception("x" * 1000 + "\n\nsecond line")
check(len(providers.said(long_error)) < 300 and "\n" not in providers.said(long_error),
      "and kept to one short line")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
