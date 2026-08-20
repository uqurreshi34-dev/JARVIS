import re

from providers import chat


# Thinking models spend part of this budget on internal reasoning before
# writing anything, so it needs headroom well beyond the spoken answer.
_MAX_TOKENS = 900

_SYSTEM_PROMPT = """
You are JARVIS, a British AI assistant answering a spoken question.

Your reply is read aloud by a speech engine, so:
- Answer in one to three short sentences. Never more.
- Plain prose only. No markdown, lists, headings, code blocks, or emoji.
- Spell out symbols where a listener needs them, but keep it natural.
- If you do not know, say so briefly rather than guessing.
- Address the user as "sir" at most once, and only when it fits.

Be direct and factual. Do not restate the question.
"""


def _for_speech(text):
    """Strip anything that would sound wrong when read aloud."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"^\s*[-\u2022]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def answer(question):
    """Answer a general question, or None if it cannot be answered."""
    if not question or not question.strip():
        return None

    try:
        raw = chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": question.strip()},
            ],
            temperature=0.3,
            max_tokens=_MAX_TOKENS,
            reasoning_effort="low",
        )

    except Exception as error:
        print(f"[JARVIS] question lookup failed: {error}")
        return None

    spoken = _for_speech(raw)

    return spoken or None
