import json

from providers import chat


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
Use close_project when the user wants one of those project windows closed.
Set "project" the same way.
Use list_projects when the user asks what their recent projects are.

Use these for sound and media control:
volume_up when the user wants the volume raised or something louder.
volume_down when the user wants the volume lowered or something quieter.
set_volume when the user names a specific level, such as "set volume to 40"
or "volume at half". Put the target percentage in "amount" as a number from
0 to 100. Treat "half" as 50, "full" or "max" as 100.
get_volume when the user asks how loud it currently is.
mute when the user clearly wants sound off. unmute when they clearly want it
back on. toggle_mute only when it is ambiguous which they mean.
media_play_pause for play, pause, or resume.
media_next to skip forward a track.
media_previous to go back a track.

Leave "amount" null for every intent except set_volume and set_timer. When you
do set it, give plain digits only, such as 40. Never write the word null as
text. Leave "text" null for every intent except set_timer.

Use minimise_all when the user wants all windows minimised or the desktop
shown. Use restore_all when the user wants those windows brought back.

Use set_reminder when the user wants a timer or reminder. Put the total duration
in seconds in "amount" — so five minutes is 300, an hour is 3600, ninety
seconds is 90 — and set "unit" to "seconds". If the user says what to be
reminded about, put that in "text" as a short phrase, otherwise leave "text"
null. For "remind me in ten minutes to call mum", amount is 600, unit is
"seconds", and text is "call mum".
Use list_reminders when the user asks what timers or reminders are running.
Use cancel_reminders when the user wants timers or reminders cancelled.

Use read_clipboard when the user asks what is on their clipboard.
Use copy_to_clipboard when the user wants something put on the clipboard, and
put the exact wording to copy in "text".
Use clear_clipboard when the user wants the clipboard emptied.

Use take_screenshot when the user wants a screenshot, screen capture, or a
picture of their screen.

Use get_time when the user asks for the current time or today's date.
Use get_weather when the user asks about the weather, temperature, or forecast.
Use get_system_status when the user asks how the machine, PC, or system is
doing, or about CPU, memory, disk space, or battery.
For these, leave both "application" and "website" null.

Use answer_question when the user asks a general knowledge or factual
question that none of the intents above cover, such as "what's the capital of
Peru", "how far away is the moon", or "explain what an API is". This is a last
resort: if the command is a request to control the machine, use the matching
intent above instead. Never use answer_question for opening or closing things,
for volume or media, or for the time, weather, or system status.

Use unknown for anything else.
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "open_application",
                "close_application",
                "open_website",
                "open_project",
                "close_project",
                "list_projects",
                "volume_up",
                "volume_down",
                "set_volume",
                "get_volume",
                "mute",
                "unmute",
                "toggle_mute",
                "media_play_pause",
                "media_next",
                "media_previous",
                "minimise_all",
                "restore_all",
                "set_reminder",
                "list_reminders",
                "cancel_reminders",
                "take_screenshot",
                "read_clipboard",
                "copy_to_clipboard",
                "clear_clipboard",
                "get_time",
                "get_weather",
                "get_system_status",
                "answer_question",
                "unknown",
            ],
        },
        "application": {"type": ["string", "null"]},
        "website": {"type": ["string", "null"]},
        "project": {"type": ["string", "null"]},
        "amount": {"type": ["string", "number", "null"]},
        "text": {"type": ["string", "null"]},
        "unit": {"type": ["string", "null"]},
    },
    "required": [
        "intent",
        "application",
        "website",
        "project",
        "amount",
        "text",
        "unit",
    ],
    "additionalProperties": False,
}

_FIELDS = ("intent", "application", "website", "project", "amount",
           "text", "unit")


def _parse(content):
    """Turn the model's reply into a complete result dictionary."""
    text = (content or "").strip()

    # Some providers wrap JSON in markdown fences.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.removeprefix("json").strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        print(f"[JARVIS] could not parse model reply: {text[:120]!r}")
        return {field: None for field in _FIELDS} | {"intent": "unknown"}

    if not isinstance(data, dict):
        return {field: None for field in _FIELDS} | {"intent": "unknown"}

    # Guarantee every field exists, whichever provider answered.
    result = {field: data.get(field) for field in _FIELDS}

    if not result["intent"]:
        result["intent"] = "unknown"

    return result


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

        content = chat(
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
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "jarvis_command",
                    "strict": True,
                    "schema": _SCHEMA,
                },
            },
            temperature=0,
            reasoning_effort="low",
        )

        return _parse(content)
