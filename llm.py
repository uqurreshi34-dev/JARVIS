import json
import os

from dotenv import load_dotenv
from groq import Groq


load_dotenv()

_api_key = os.getenv("GROQ_API_KEY")

if not _api_key:
    raise RuntimeError("GROQ_API_KEY is not configured.")

_client = Groq(api_key=_api_key)

_MODEL = "openai/gpt-oss-20b"

_SYSTEM_PROMPT = """
You are the command interpreter for a Windows voice assistant called JARVIS.

Return JSON with exactly these fields:

{
    "intent": "open_application" | "close_application" | "unknown",
    "application": string | null
}

Rules:

- Determine what the user wants from their natural-language request.
- For application requests, match the user's intended application
  to the closest application name in the supplied installed-applications list.
- Correct obvious speech-recognition errors.
- Prefer an exact installed application name when possible.
- Never invent an application name that is not in the supplied list.
- open_application means the user wants an application opened.
- close_application means the user wants an application closed.
- For unrelated requests, return:
  {"intent": "unknown", "application": null}
"""


def interpret(command, applications):
    response = _client.chat.completions.create(
        model=_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": _SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    f"Installed applications:\n"
                    f"{applications}\n\n"
                    f"User command:\n{command}"
                ),
            },
        ],
    )

    return json.loads(response.choices[0].message.content)
