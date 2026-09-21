"""Talk to whichever LLM provider is configured, and rotate when one caps out.

Both Groq and Gemini expose OpenAI-compatible endpoints, so a single client
drives either. Set LLM_PROVIDER in .env to choose the primary; any other
provider with a key present is used automatically as a fallback.
"""

import base64
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI
from anthropic import AnthropicFoundry

try:
    # Anthropic's own API uses the plain client. Guarded so an SDK without
    # it cannot stop JARVIS booting -- the provider that needs it simply
    # refuses to build.
    from anthropic import Anthropic
except ImportError:
    Anthropic = None


load_dotenv()

# After a provider reports it is out of quota, stop trying it for this long
# rather than burning a failed request on every command.
_COOLDOWN_SECONDS = 15 * 60


PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-20b",
        # Vision needs a model that accepts images; the text default does not.
        "vision_env": "GROQ_VISION_MODEL",
        "default_vision_model": "qwen/qwen3.8-27b",
        # Qwen narrates its thinking unless told to hide it.
        "vision_reasoning": True,
        "default_reasoning_effort": "low",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-3.6-flash",
        "vision_env": "GEMINI_VISION_MODEL",
        "default_vision_model": "gemini-3.6-flash",
        "reasoning": True,
        "default_reasoning_effort": "low",
    },
    "claude": {
        "kind": "anthropic",
        "base_url_env": "ANTHROPIC_FOUNDRY_BASE_URL",
        "key_env": "ANTHROPIC_FOUNDRY_API_KEY",
        "model_env": "ANTHROPIC_FOUNDRY_MODEL",
        "default_model": "claude-opus-5",
        "vision_env": "ANTHROPIC_FOUNDRY_VISION_MODEL",
        "default_vision_model": "claude-opus-5",
        "reasoning": True,
        "default_effort_env": "CLAUDE_DEFAULT_EFFORT",
        "answer_effort_env": "CLAUDE_ANSWER_EFFORT",
        "vision_effort_env": "CLAUDE_VISION_EFFORT",
    },
    # Anthropic's own paid API. Same models and the same code path as the
    # Foundry deployment; only the client class and the absent base URL
    # differ. It is dropped from the pool when ANTHROPIC_API_KEY is unset,
    # so it costs nothing until it is wanted, and it sits last in the
    # natural order -- work that should prefer it asks for it by name.
    "anthropic": {
        "kind": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
        "model_env": "ANTHROPIC_MODEL",
        "default_model": "claude-opus-5",
        "vision_env": "ANTHROPIC_VISION_MODEL",
        "default_vision_model": "claude-opus-5",
        "reasoning": True,
        "default_effort_env": "CLAUDE_DEFAULT_EFFORT",
        "answer_effort_env": "CLAUDE_ANSWER_EFFORT",
        "vision_effort_env": "CLAUDE_VISION_EFFORT",
    },
}

_PRIMARY = (os.getenv("LLM_PROVIDER") or "groq").strip().casefold()


def is_rate_limit(error):
    """True when an error is a quota or rate limit rejection."""
    name = type(error).__name__.casefold()

    if "ratelimit" in name:
        return True

    text = str(error).casefold()

    return (
        "rate_limit" in text
        or "429" in text
        or "resource_exhausted" in text
        or "quota" in text
    )


def is_model_unavailable(error):
    """True when the model itself is retired, renamed, or not permitted.

    Providers retire models regularly, and that should move the request to
    the next provider rather than failing the command outright.
    """
    text = str(error).casefold()

    return (
        "no longer available" in text
        or "decommissioned" in text
        or "not_found" in text
        or "model_not_found" in text
        or "does not exist" in text
        or "404" in text
    )


def is_permission_error(error):
    """True when the account/key lacks access to the model or endpoint.

    This is distinct from a rate limit: it will not clear on its own, but
    the *next* provider may well have that access, so it should still move
    the request along rather than failing the command outright.
    """
    name = type(error).__name__.casefold()

    if "permissiondenied" in name or "authenticationerror" in name:
        return True

    text = str(error).casefold()

    return (
        "403" in text
        or "401" in text
        or "permission" in text
        or "forbidden" in text
        or "unauthorized" in text
        or "invalid api key" in text
        or "valid api key" in text
    )


def is_transient_error(error):
    """True when a request failed for a reason that says nothing about
    whether another provider would have succeeded.

    A 500, a timeout, or a dropped connection is a fact about one
    provider's servers at one moment, not about the request. Treating it
    as fatal meant the command died outright while a healthy provider sat
    idle -- the opposite of the point of having a pool.

    Status code first, because the SDK exceptions carry one and matching
    on the text alone would fire on any message that happens to contain
    "500".
    """
    status = getattr(error, "status_code", None)

    if isinstance(status, int) and status >= 500:
        return True

    name = type(error).__name__.casefold()

    if (
        "timeout" in name
        or "connection" in name
        or "internalserver" in name
        or "serviceunavailable" in name
    ):
        return True

    text = str(error).casefold()

    return (
        "timed out" in text
        or "timeout" in text
        or "overloaded" in text
        or "temporarily unavailable" in text
        or "service unavailable" in text
        or "bad gateway" in text
        or "connection reset" in text
        or "connection error" in text
    )


def should_failover(error):
    return (
        is_rate_limit(error)
        or is_model_unavailable(error)
        or is_permission_error(error)
        or is_transient_error(error)
    )


def _claude_schema(schema, root=False):
    """Adapt JARVIS JSON Schema to Claude's structured-output dialect."""
    if isinstance(schema, list):
        return [_claude_schema(item, root=False) for item in schema]

    if not isinstance(schema, dict):
        return schema

    converted = {}

    schema_type = schema.get("type")

    if isinstance(schema_type, list):
        non_null_types = [
            value
            for value in schema_type
            if value != "null"
        ]

        if len(non_null_types) == 1:
            converted["type"] = non_null_types[0]
        else:
            converted["type"] = non_null_types

    elif schema_type is not None:
        converted["type"] = schema_type

    if "enum" in schema:
        converted["enum"] = [
            value
            for value in schema["enum"]
            if value is not None
        ]

    for key in ("description", "title", "format"):
        if key in schema:
            converted[key] = schema[key]

    if schema_type == "object" or (
        isinstance(schema_type, list) and "object" in schema_type
    ):
        properties = schema.get("properties", {})
        converted["properties"] = {}

        required = list(schema.get("required", []))

        for name, property_schema in properties.items():
            nullable = (
                (
                    isinstance(property_schema.get("type"), list)
                    and "null" in property_schema["type"]
                )
                or (
                    None in property_schema.get("enum", [])
                )
            )

            converted["properties"][name] = _claude_schema(
                property_schema,
                root=False,
            )

            # Only top-level nullable fields become optional.
            # Nested fields such as memory.key/cardinality/etc remain
            # required when the memory object itself is present.
            if root and nullable and name in required:
                required.remove(name)

        converted["additionalProperties"] = False

        if required:
            converted["required"] = required

        return converted

    if schema_type == "array":
        if "items" in schema:
            converted["items"] = _claude_schema(
                schema["items"],
                root=False,
            )

    return converted


class Provider:
    def __init__(self, name, config, api_key):
        self.name = name
        self.kind = config.get("kind", "openai")
        self.model = os.getenv(config["model_env"]) or config["default_model"]
        self.reasoning = config.get("reasoning", False)

        effort_env = config.get("default_effort_env")
        self.default_effort = (
            os.getenv(effort_env).strip().casefold()
            if effort_env and os.getenv(effort_env)
            else config.get("default_reasoning_effort")
        )

        if self.default_effort not in {
            None, "low", "medium", "high", "xhigh", "max"
        }:
            print(
                f"[JARVIS] {self.name} has invalid effort "
                f"{self.default_effort!r}; ignoring it"
            )
            self.default_effort = None

        answer_effort_env = config.get("answer_effort_env")
        self.answer_effort = (
            os.getenv(answer_effort_env).strip().casefold()
            if answer_effort_env and os.getenv(answer_effort_env)
            else self.default_effort
        )

        vision_effort_env = config.get("vision_effort_env")
        self.vision_effort = (
            os.getenv(vision_effort_env).strip().casefold()
            if vision_effort_env and os.getenv(vision_effort_env)
            else self.answer_effort
        )

        valid_efforts = {
            None, "low", "medium", "high", "xhigh", "max"
        }

        if self.answer_effort not in valid_efforts:
            print(
                f"[JARVIS] {self.name} has invalid answer effort "
                f"{self.answer_effort!r}; using default"
            )
            self.answer_effort = self.default_effort

        if self.vision_effort not in valid_efforts:
            print(
                f"[JARVIS] {self.name} has invalid vision effort "
                f"{self.vision_effort!r}; using answer effort"
            )
            self.vision_effort = self.answer_effort

        vision_env = config.get("vision_env")
        self.vision_model = (
            (os.getenv(vision_env) if vision_env else None)
            or config.get("default_vision_model")
        )
        self.vision_reasoning = config.get("vision_reasoning", False)

        if self.kind == "anthropic":
            base_url_env = config.get("base_url_env")

            if base_url_env:
                base_url = os.getenv(base_url_env)

                if not base_url:
                    raise RuntimeError(
                        f"{self.name} is configured but its base URL is missing"
                    )

                self._client = AnthropicFoundry(
                    api_key=api_key,
                    base_url=base_url,
                )

            elif Anthropic is None:
                raise RuntimeError(
                    f"{self.name} needs the Anthropic client, which this "
                    "version of the anthropic package does not provide"
                )

            else:
                # No base URL configured means Anthropic's own endpoint,
                # which the plain client already knows.
                self._client = Anthropic(api_key=api_key)
        else:
            self._client = OpenAI(
                api_key=api_key,
                base_url=config["base_url"],
            )

        self._resting_until = 0.0

    @property
    def resting(self):
        return time.monotonic() < self._resting_until

    def rest(self, seconds=_COOLDOWN_SECONDS):
        self._resting_until = time.monotonic() + seconds

    def wake(self):
        self._resting_until = 0.0

    def _chat_anthropic(
        self,
        messages,
        response_format=None,
        temperature=0,
        max_tokens=None,
        reasoning_effort=None,
    ):
        """Send a chat request through the native Anthropic Messages API."""
        system_parts = []
        anthropic_messages = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_parts.append(content.strip())
                continue

            if isinstance(content, str):
                normalized_content = content
            elif isinstance(content, list):
                text_parts = []

                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "text"
                        and item.get("text")
                    ):
                        text_parts.append(str(item["text"]))

                normalized_content = "\n".join(text_parts)
            else:
                normalized_content = str(content)

            anthropic_messages.append(
                {
                    "role": role,
                    "content": normalized_content,
                }
            )

        kwargs = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 2048,
        }

        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        output_config = {}

        effort = reasoning_effort or self.answer_effort

        if effort:
            output_config["effort"] = effort

        if response_format:
            if response_format.get("type") != "json_schema":
                raise ValueError(
                    f"Unsupported Claude response format: "
                    f"{response_format.get('type')!r}"
                )

            json_schema = response_format.get("json_schema") or {}
            schema = json_schema.get("schema")

            if not schema:
                raise ValueError(
                    "Claude response format is missing its schema")

            output_config["format"] = {
                "type": "json_schema",
                "schema": _claude_schema(schema, root=True),
            }

        if output_config:
            kwargs["output_config"] = output_config

        with self._client.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()

        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = (block.text or "").strip()

                if text:
                    return text

        return ""

    def chat(self, messages, response_format=None, temperature=0,
             max_tokens=None, reasoning_effort=None):

        if self.kind == "anthropic":
            return self._chat_anthropic(
                messages,
                response_format=response_format,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        if response_format:
            kwargs["response_format"] = response_format

        # Thinking models spend part of max_tokens on internal reasoning,
        # which can truncate a short answer. Keep that minimal where the
        # provider allows it.
        effort = reasoning_effort or self.default_effort

        if effort and self.reasoning:
            kwargs["reasoning_effort"] = effort

        try:
            response = self._client.chat.completions.create(**kwargs)

        except Exception as error:
            retried = dict(kwargs)
            changed = False

            # Not every provider accepts a strict json_schema. Fall back to
            # plain JSON mode rather than losing the request entirely.
            if response_format and _is_format_error(error):
                print(
                    f"[JARVIS] {self.name} rejected the strict schema; "
                    "retrying in JSON mode"
                )
                retried["response_format"] = {"type": "json_object"}
                changed = True

            if "reasoning_effort" in retried and _is_parameter_error(error):
                print(
                    f"[JARVIS] {self.name} rejected reasoning_effort; retrying")
                retried.pop("reasoning_effort")
                changed = True

            if not changed:
                raise

            response = self._client.chat.completions.create(**retried)

        message = response.choices[0].message
        content = (message.content or "").strip()

        if content:
            return content

        # A reasoning model can leave content empty and put the answer in
        # a separate field instead -- vision() below has handled this for
        # a while, but chat() never did, so any prompt where the model
        # chose that shape came back as an empty string. That surfaced as
        # a vague failure in whatever called it rather than anything
        # pointing here.
        for field in ("reasoning", "reasoning_content"):
            spare = getattr(message, field, None)

            if spare and str(spare).strip():
                return _strip_reasoning(str(spare).strip())

        return ""

    def _vision_anthropic(
        self,
        prompt,
        image_bytes,
        mime,
        max_tokens,
        reasoning_effort=None,
    ):
        """Send an image through the native Anthropic Messages API."""
        encoded = base64.b64encode(image_bytes).decode("ascii")

        kwargs = {
            "model": self.vision_model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": encoded,
                            },
                        },
                    ],
                }
            ],
        }

        effort = reasoning_effort or self.vision_effort

        if effort:
            kwargs["output_config"] = {
                "effort": effort,
            }

        with self._client.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()

        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = (block.text or "").strip()

                if text:
                    return text

        stop_reason = getattr(
            response,
            "stop_reason",
            None,
        )

        if stop_reason == "max_tokens":
            print(
                f"[JARVIS] {self.name} exhausted the vision token "
                "budget before producing visible text."
            )

        return ""

    def _vision_chat_anthropic(
        self,
        messages,
        image_bytes,
        mime,
        max_tokens,
        reasoning_effort=None,
    ):
        """Run a native Anthropic multimodal chat request."""
        system_parts = []
        anthropic_messages = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_parts.append(content.strip())
                continue

            anthropic_messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        encoded = base64.b64encode(
            image_bytes
        ).decode("ascii")

        if not anthropic_messages:
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": [],
                }
            )

        last_content = anthropic_messages[-1]["content"]

        if isinstance(last_content, str):
            last_content = [
                {
                    "type": "text",
                    "text": last_content,
                }
            ]
            anthropic_messages[-1]["content"] = last_content

        last_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": encoded,
                },
            }
        )

        kwargs = {
            "model": self.vision_model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }

        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        effort = reasoning_effort or self.vision_effort

        if effort:
            kwargs["output_config"] = {
                "effort": effort,
            }

        with self._client.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()

        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = (block.text or "").strip()

                if text:
                    return text

        return ""

    def agent_turn(self, messages, tools, max_tokens=1800, system=None):
        """Run one tool-capable agent turn for this provider."""
        if self.kind == "anthropic":
            kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "tools": tools,
                "tool_choice": {
                    "type": "auto",
                    "disable_parallel_tool_use": False,
                },
            }

            if system:
                kwargs["system"] = system

            if self.answer_effort:
                kwargs["output_config"] = {
                    "effort": self.answer_effort,
                }

            return self._client.messages.create(**kwargs)

        if system:
            messages = [
                {
                    "role": "system",
                    "content": system,
                },
                *messages,
            ]

        kwargs = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }

        effort = self.default_effort

        if effort and self.reasoning:
            kwargs["reasoning_effort"] = effort

        return self._client.chat.completions.create(**kwargs)


def _is_parameter_error(error):
    text = str(error).casefold()

    return (
        "reasoning_effort" in text
        or "reasoning_format" in text
        or "unknown parameter" in text
    )


def _is_format_error(error):
    text = str(error).casefold()

    return (
        "response_format" in text
        or "json_schema" in text
        or "schema" in text and "unsupported" in text
        or "json_validate_failed" in text
        or "failed_generation" in text
    )


def _build_pool():
    """Configured providers, primary first."""
    order = [_PRIMARY] + [n for n in PROVIDERS if n != _PRIMARY]
    pool = []

    for name in order:
        config = PROVIDERS.get(name)

        if not config:
            print(f"[JARVIS] unknown LLM_PROVIDER {name!r}, ignoring")
            continue

        api_key = os.getenv(config["key_env"])

        if not api_key:
            continue

        pool.append(Provider(name, config, api_key))

    return pool


_pool = _build_pool()

if not _pool:
    raise RuntimeError(
        "No LLM provider configured. Set GROQ_API_KEY or GEMINI_API_KEY "
        "in .env"
    )

print(
    "[JARVIS] LLM providers: "
    + ", ".join(f"{p.name} ({p.model})" for p in _pool)
)


def _ordered(prefer=None, pool=None):
    """Providers in the order they should be tried.

    Awake ones first, but a resting provider is still better than
    failing. Any names in prefer are moved ahead of both, in the order
    given, and only when they are actually present -- that is how work
    whose quality depends on the model asks for a capable one without
    pinning itself to a provider that may not be configured at all.

    pool defaults to every provider; the vision paths pass the subset
    that has a vision model.
    """
    candidates = _pool if pool is None else pool

    order = [p for p in candidates if not p.resting] + \
        [p for p in candidates if p.resting]

    if not prefer:
        return order

    wanted = [str(name).casefold() for name in prefer]

    def rank(provider):
        try:
            return wanted.index(provider.name.casefold())
        except ValueError:
            return len(wanted)

    # Stable, so providers of equal rank keep the awake-first ordering.
    return sorted(order, key=rank)


def chat(messages, response_format=None, temperature=0, max_tokens=None,
         reasoning_effort=None, prefer=None):
    """Send a chat request, rotating providers when one is unavailable."""
    last_error = None

    order = _ordered(prefer)

    for index, provider in enumerate(order):
        try:
            content = provider.chat(
                messages,
                response_format=response_format,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )

            # It answered, so it is clearly available again.
            provider.wake()

            return content

        except Exception as error:
            last_error = error

            if not should_failover(error):
                raise

            provider.rest()

            if is_rate_limit(error):
                reason = "rate limited"
            elif is_permission_error(error):
                reason = f"access denied ({provider.model})"
            else:
                reason = f"model unavailable ({provider.model})"

            remaining = len(order) - index - 1

            if remaining:
                print(
                    f"[JARVIS] {provider.name} {reason}; "
                    f"switching to {order[index + 1].name}"
                )
            else:
                print(f"[JARVIS] {provider.name} {reason}, no fallback left")

    raise last_error


_THINK_TAGS = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


# An opening think tag with no closing one, which is what a reply cut
# off mid-thought looks like. The regex above cannot match that, so the
# reasoning would survive untouched and be handed on as if it were the
# answer.
_OPEN_THINK = re.compile(r"<think>.*", re.IGNORECASE | re.DOTALL)


def _strip_reasoning(text):
    """Remove any chain-of-thought a reasoning model didn't actually hide."""
    cleaned = _THINK_TAGS.sub("", text).strip()

    # Truncated mid-thought: everything from the opening tag onward is
    # reasoning that never finished, so nothing after it is worth
    # keeping either.
    if "<think>" in cleaned.lower():
        cleaned = _OPEN_THINK.sub("", cleaned).strip()

    # Only trust the stripped version if something sensible is left; an
    # empty result means the whole reply was inside the tags, which is
    # a stranger failure than leaked reasoning and worth seeing raw.
    return cleaned or text


def _describe_image(
    provider,
    prompt,
    image_bytes,
    mime,
    max_tokens,
    reasoning_effort=None,
):
    """One vision request to a single provider.

    The budget matters more than it looks for a reasoning model. Hidden
    reasoning is still generated and still spends tokens, so a ceiling
    sized for the answer alone leaves nothing for the answer itself --
    the model thinks until it runs out and returns an empty string,
    with no error to explain it. That is what "returned nothing for the
    image" means, and it is not a picture the model could not read.
    """
    if provider.kind == "anthropic":
        return provider._vision_anthropic(
            prompt,
            image_bytes,
            mime,
            max_tokens,
            reasoning_effort=reasoning_effort,
        )

    encoded = base64.b64encode(image_bytes).decode("ascii")

    kwargs = {
        "model": provider.vision_model,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{encoded}"
                        },
                    },
                ],
            }
        ],
    }

    # Reasoning models narrate their thinking into the answer unless told
    # not to. Groq accepts reasoning_format for these, but it isn't a
    # parameter the SDK knows by name, so it has to travel via extra_body
    # or the client rejects it locally before any request is sent.
    if provider.vision_reasoning:
        kwargs["extra_body"] = {"reasoning_format": "hidden"}

        # Qwen3 reads /no_think in the prompt as an instruction not to
        # reason at all. It matters because Groq refuses both of the
        # parameters below for this model, leaving the prompt as the
        # only way to ask -- and an unasked model thinks at length,
        # visibly, until the budget runs out and the answer never
        # arrives. Documented as working in the user message rather
        # than a system one, which is where this prompt already goes.
        text_part = kwargs["messages"][0]["content"][0]

        if not text_part["text"].rstrip().endswith("/no_think"):
            text_part["text"] = f"{text_part['text']}\n\n/no_think"

        # Hiding the reasoning does not shorten it. Without this the
        # model thinks at full effort and spends the whole budget doing
        # it, returning an empty answer with no error -- which reads as
        # "the picture could not be read" and is nothing of the kind.
        # chat() has always asked for low effort; vision never did.
        kwargs["reasoning_effort"] = "none"

    try:
        response = provider._client.chat.completions.create(**kwargs)
    except Exception as error:
        if not _is_parameter_error(error):
            raise

        # Drop whichever knob was refused and try again, rather than
        # failing outright over a parameter that was only ever an
        # optimisation.
        retried = dict(kwargs)
        changed = False

        if "extra_body" in retried:
            print(
                f"[JARVIS] {provider.name} rejected reasoning_format; "
                "retrying without it"
            )
            retried.pop("extra_body", None)
            changed = True

        if "reasoning_effort" in retried:
            print(
                f"[JARVIS] {provider.name} rejected reasoning_effort; "
                "retrying without it"
            )
            retried.pop("reasoning_effort", None)
            changed = True

        if not changed:
            raise

        response = provider._client.chat.completions.create(**retried)

    message = response.choices[0].message
    content = (message.content or "").strip()

    if not content:
        # With reasoning hidden, some models return the answer in a separate
        # field and leave content empty.
        for field in ("reasoning", "reasoning_content"):
            spare = getattr(message, field, None)

            if spare and str(spare).strip():
                content = str(spare).strip()
                break

    if not content:
        finish = getattr(response.choices[0], "finish_reason", None)

        if finish == "length":
            print(
                f"[JARVIS] {provider.name} ran out of tokens before "
                f"answering (max_tokens={max_tokens}). A reasoning model "
                "spends this budget thinking as well as writing."
            )

    return _strip_reasoning(content)


def vision(
    prompt,
    image_bytes,
    mime="image/png",
    max_tokens=3000,
    reasoning_effort=None,
    prefer=None,
):
    """Ask about an image, rotating providers exactly as chat does."""
    if not image_bytes:
        return None

    capable = [p for p in _pool if p.vision_model]

    if not capable:
        print("[JARVIS] no provider is configured for vision")
        return None

    order = _ordered(prefer, capable)

    last_error = None

    for index, provider in enumerate(order):
        try:
            answer = _describe_image(
                provider,
                prompt,
                image_bytes,
                mime,
                max_tokens,
                reasoning_effort=reasoning_effort,
            )

            if not (answer or "").strip():
                # No exception, but nothing said either. Treated as a
                # failure so the next provider is tried, rather than
                # returning empty and looking like the picture was
                # unreadable.
                print(
                    f"[JARVIS] {provider.name} returned nothing for the image"
                )

                if len(order) - index - 1:
                    print(f"[JARVIS] trying {order[index + 1].name}")

                continue

            provider.wake()

            return answer

        except Exception as error:
            last_error = error

            if not should_failover(error):
                print(
                    f"[JARVIS] {provider.name} could not see the image: {error}")

                # Not a provider-health problem, so it is not rested. But
                # the next provider may still manage the picture -- one
                # provider rejecting an image says little about another,
                # and returning here threw that chance away. The empty
                # answer branch above already continues; this matches it.
                if len(order) - index - 1:
                    print(f"[JARVIS] trying {order[index + 1].name}")

                continue

            provider.rest()

            if is_rate_limit(error):
                reason = "rate limited"
            elif is_permission_error(error):
                reason = f"access denied ({provider.vision_model})"
            else:
                reason = f"model unavailable ({provider.vision_model})"

            if len(order) - index - 1:
                print(
                    f"[JARVIS] {provider.name} {reason} for vision; "
                    f"trying {order[index + 1].name}"
                )

    print(f"[JARVIS] no provider could see the image: {last_error}")

    return None


def vision_chat(
    messages,
    image_bytes,
    mime="image/png",
    max_tokens=12000,
    reasoning_effort=None,
    prefer=None,
):
    """Run a multimodal chat request with provider failover."""
    if not image_bytes:
        return ""

    capable = [
        provider
        for provider in _pool
        if provider.vision_model
    ]

    if not capable:
        print(
            "[JARVIS] no provider is configured for multimodal chat"
        )
        return ""

    order = _ordered(prefer, capable)

    last_error = None

    for index, provider in enumerate(order):
        try:
            if provider.kind == "anthropic":
                answer = provider._vision_chat_anthropic(
                    messages,
                    image_bytes,
                    mime,
                    max_tokens,
                    reasoning_effort=reasoning_effort,
                )

            else:
                encoded = base64.b64encode(
                    image_bytes
                ).decode("ascii")

                multimodal_messages = []

                for message in messages:
                    content = message.get(
                        "content",
                        "",
                    )

                    if (
                        message.get("role") == "user"
                        and isinstance(content, str)
                    ):
                        content = [
                            {
                                "type": "text",
                                "text": content,
                            },
                        ]

                    multimodal_messages.append(
                        {
                            **message,
                            "content": content,
                        }
                    )

                user_message = (
                    multimodal_messages[-1]
                    if multimodal_messages
                    else {
                        "role": "user",
                        "content": [],
                    }
                )

                content = user_message["content"]

                if isinstance(content, list):
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{mime};base64,"
                                    f"{encoded}"
                                )
                            },
                        }
                    )

                kwargs = {
                    "model": provider.vision_model,
                    "messages": multimodal_messages,
                    "max_tokens": max_tokens,
                }

                effort = (
                    reasoning_effort
                    or provider.vision_effort
                )

                if (
                    effort
                    and provider.reasoning
                ):
                    kwargs["reasoning_effort"] = effort

                response = provider._client.chat.completions.create(
                    **kwargs
                )

                message = response.choices[0].message
                answer = (message.content or "").strip()

                if not answer:
                    for field in (
                        "reasoning",
                        "reasoning_content",
                    ):
                        spare = getattr(
                            message,
                            field,
                            None,
                        )

                        if spare and str(spare).strip():
                            answer = str(spare).strip()
                            break

            if (answer or "").strip():
                provider.wake()
                return _strip_reasoning(answer)

            print(
                f"[JARVIS] {provider.name} returned nothing "
                "for multimodal refinement"
            )

        except Exception as error:
            last_error = error

            if not should_failover(error):
                print(
                    f"[JARVIS] {provider.name} could not process "
                    f"multimodal refinement: {error}"
                )

                # As in vision(): not rested, but not fatal to the whole
                # request either. The next provider gets its turn.
                if len(order) - index - 1:
                    print(f"[JARVIS] trying {order[index + 1].name}")

                continue

            provider.rest()

            if is_rate_limit(error):
                reason = "rate limited"

            elif is_permission_error(error):
                reason = (
                    f"access denied ({provider.model})"
                )

            else:
                reason = (
                    f"model unavailable ({provider.model})"
                )

            if len(order) - index - 1:
                print(
                    f"[JARVIS] {provider.name} {reason} for "
                    "multimodal refinement; trying "
                    f"{order[index + 1].name}"
                )

    print(
        "[JARVIS] no provider could process multimodal refinement: "
        f"{last_error}"
    )

    return ""


def active_provider():
    return _pool[0].name if _pool else None
