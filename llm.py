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

The action verb must be clear. Speech recognition is imperfect, and guessing
wrong does the opposite of what the user wanted, which is worse than doing
nothing. If the command names an application but does not clearly state
whether to open or close it, return unknown. Do not default to opening.

For example "chrome" alone, or "those chrome", state no clear verb: return
unknown. But "open chrome", "launch chrome", "close chrome", "quit chrome",
and "I'm done with chrome" all state a clear verb.

Use open_application when the user wants a local application opened.
Use close_application when the user wants a local application closed.

Use open_website when the user wants to open a website or online service that
is NOT in the candidate application list (for example YouTube, Gmail, Google,
Maps, a news or shopping site). Set "website" to a full https URL such as
"https://www.youtube.com". Leave "application" null.

For open_application and close_application, leave "website" null.
For open_website, leave "application" null.

Use open_project when the user wants one of their code projects opened in
their editor. Set "project" to the matching name from the candidate project
list. Never invent a project name.
Use list_projects when the user asks what their recent projects are.

Use get_time when the user asks for the current time or today's date.
Use get_weather when the user asks about the weather, temperature, or forecast.
Use get_system_status when the user asks how the machine, PC, or system is
doing, or about CPU, memory, disk space, or battery.
For these, leave both "application" and "website" null.

Use unknown for anything else.
"""


class CommandInterpreter:
    def interpret(self, command, applications, projects=()):
        candidates = "\n".join(
            f"- {application}"
            for application in applications
        )

        project_list = "\n".join(
            f"- {project}"
            for project in projects
        ) or "- (none found)"

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
                                    "open_project",
                                    "list_projects",
                                    "get_time",
                                    "get_weather",
                                    "get_system_status",
                                    "unknown",
                                ],
                            },
                            "application": {
                                "type": ["string", "null"],
                            },
                            "website": {
                                "type": ["string", "null"],
                            },
                            "project": {
                                "type": ["string", "null"],
                            },
                        },
                        "required": [
                            "intent",
                            "application",
                            "website",
                            "project",
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
                        f"Candidate projects:\n"
                        f"{project_list}\n\n"
                        f"User command:\n"
                        f"{command}"
                    ),
                },
            ],
        )

        return json.loads(
            response.choices[0].message.content
        )
