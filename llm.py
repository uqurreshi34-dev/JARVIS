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

Interpret the user's natural-language command.

For application requests, select the application that best matches the user's
intended application from the supplied candidate list. Correct obvious
speech-recognition errors. Never invent an application name.

Use open_application when the user wants a local application opened.
Use close_application when the user wants a local application closed.

Use open_website when the user wants to open a website or online service that
is NOT in the candidate application list (for example YouTube, Gmail, Google,
Maps, a news or shopping site). Set "website" to a full https URL such as
"https://www.youtube.com". Leave "application" null.

For open_application and close_application, leave "website" null.
For open_website, leave "application" null.

Use unknown for anything that is not an application or website request.
"""


class CommandInterpreter:
    def interpret(self, command, applications):
        candidates = "\n".join(
            f"- {application}"
            for application in applications
        )

        response = _client.chat.completions.create(
            model=_MODEL,
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "jarvis_command",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "intent": {
                                "type": "string",
                                "enum": [
                                    "open_application",
                                    "close_application",
                                    "open_website",
                                    "unknown",
                                ],
                            },
                            "application": {
                                "type": ["string", "null"],
                            },
                            "website": {
                                "type": ["string", "null"],
                            },
                        },
                        "required": [
                            "intent",
                            "application",
                            "website",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            messages=[
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        f"Candidate applications:\n"
                        f"{candidates}\n\n"
                        f"User command:\n"
                        f"{command}"
                    ),
                },
            ],
        )

        return json.loads(
            response.choices[0].message.content
        )
