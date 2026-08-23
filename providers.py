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
        "default_vision_model": "qwen/qwen3.6-27b",
        # Qwen narrates its thinking unless told to hide it.
        "vision_reasoning": True,
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-3.6-flash",
        "vision_env": "GEMINI_VISION_MODEL",
        "default_vision_model": "gemini-3.6-flash",
        "reasoning": True,
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
    )


def should_failover(error):
    return (
        is_rate_limit(error)
        or is_model_unavailable(error)
        or is_permission_error(error)
    )


class Provider:
    def __init__(self, name, config, api_key):
        self.name = name
        self.model = os.getenv(config["model_env"]) or config["default_model"]
        self.reasoning = config.get("reasoning", False)

        vision_env = config.get("vision_env")
        self.vision_model = (
            (os.getenv(vision_env) if vision_env else None)
            or config.get("default_vision_model")
        )
        self.vision_reasoning = config.get("vision_reasoning", False)

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

    def chat(self, messages, response_format=None, temperature=0,
             max_tokens=None, reasoning_effort=None):
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
        if reasoning_effort and self.reasoning:
            kwargs["reasoning_effort"] = reasoning_effort

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

        return response.choices[0].message.content or ""


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


def chat(messages, response_format=None, temperature=0, max_tokens=None,
         reasoning_effort=None):
    """Send a chat request, rotating providers when one is unavailable."""
    last_error = None

    # Awake providers first, but a resting one is still better than failing.
    order = [p for p in _pool if not p.resting] + \
        [p for p in _pool if p.resting]

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


def _strip_reasoning(text):
    """Remove any chain-of-thought a reasoning model didn't actually hide."""
    cleaned = _THINK_TAGS.sub("", text).strip()

    # Only trust the stripped version if something sensible is left; an
    # empty result means the whole reply was inside the tags, which is
    # a stranger failure than leaked reasoning and worth seeing raw.
    return cleaned or text


def _describe_image(provider, prompt, image_bytes, mime, max_tokens):
    """One vision request to a single provider."""
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

    try:
        response = provider._client.chat.completions.create(**kwargs)
    except Exception as error:
        if "reasoning_format" in kwargs.get("extra_body", {}) and _is_parameter_error(error):
            print(
                f"[JARVIS] {provider.name} rejected reasoning_format; "
                "retrying without it"
            )
            kwargs.pop("extra_body", None)
            response = provider._client.chat.completions.create(**kwargs)
        else:
            raise

    content = (response.choices[0].message.content or "").strip()

    return _strip_reasoning(content)


def vision(prompt, image_bytes, mime="image/png", max_tokens=300):
    """Ask about an image, rotating providers exactly as chat does."""
    if not image_bytes:
        return None

    capable = [p for p in _pool if p.vision_model]

    if not capable:
        print("[JARVIS] no provider is configured for vision")
        return None

    order = [p for p in capable if not p.resting] + [
        p for p in capable if p.resting
    ]

    last_error = None

    for index, provider in enumerate(order):
        try:
            answer = _describe_image(
                provider, prompt, image_bytes, mime, max_tokens
            )

            provider.wake()

            return answer

        except Exception as error:
            last_error = error

            if not should_failover(error):
                print(
                    f"[JARVIS] {provider.name} could not see the image: {error}")
                return None

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


def active_provider():
    return _pool[0].name if _pool else None
