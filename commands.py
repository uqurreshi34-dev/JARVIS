import webbrowser
from urllib.parse import urlparse

from actions.applications import ApplicationManager
from actions.desktop import (
    minimise_all,
    next_track,
    play_pause,
    previous_track,
    restore_all,
    toggle_mute,
    volume_down,
    volume_up,
)
from actions.projects import ProjectManager
from actions.system import describe_system, describe_time, describe_weather
from llm import CommandInterpreter


_application_manager = ApplicationManager()
_project_manager = ProjectManager()
_interpreter = CommandInterpreter()


def _open_website(url):
    return bool(webbrowser.open(url))


def _website_label(url):
    host = urlparse(url).netloc or url

    if host.startswith("www."):
        host = host[4:]

    return host


def _action(intent, response, action):
    """A command that does something; JARVIS confirms when it succeeds."""
    return {
        "kind": "action",
        "intent": intent,
        "response": response,
        "action": action,
    }


def _query(intent, action):
    """A command that finds something out; the action returns what to say."""
    return {
        "kind": "query",
        "intent": intent,
        "response": None,
        "action": action,
    }


# Intents that simply run a function and need no argument. Each entry is
# the spoken confirmation and the function to call.
_SIMPLE_ACTIONS = {
    "volume_up": ("Turning it up, sir.", volume_up),
    "volume_down": ("Turning it down, sir.", volume_down),
    "toggle_mute": ("Toggling mute, sir.", toggle_mute),
    "media_play_pause": ("Certainly, sir.", play_pause),
    "media_next": ("Skipping ahead, sir.", next_track),
    "media_previous": ("Going back, sir.", previous_track),
    "minimise_all": ("Clearing the desktop, sir.", minimise_all),
    "restore_all": ("Bringing them back, sir.", restore_all),
}


def handle_command(command):
    candidates = _application_manager.candidates(command)

    result = _interpreter.interpret(
        command,
        candidates,
        _project_manager.names(),
    )

    intent = result["intent"]
    application = result.get("application")
    website = result.get("website")
    project = result.get("project")

    if intent == "open_application" and application:
        return _action(
            intent,
            f"Opening {application}, sir.",
            lambda: _application_manager.launch(application),
        )

    if intent == "close_application" and application:
        return _action(
            intent,
            f"Closing {application}, sir.",
            lambda: _application_manager.close(application),
        )

    if intent == "open_website" and website:
        return _action(
            intent,
            f"Opening {_website_label(website)}, sir.",
            lambda: _open_website(website),
        )

    if intent == "open_project" and project:
        return _action(
            intent,
            f"Opening {project}, sir.",
            lambda: _project_manager.open(project) is not None,
        )

    if intent == "list_projects":
        return _query(intent, _project_manager.describe)

    if intent in _SIMPLE_ACTIONS:
        response, function = _SIMPLE_ACTIONS[intent]

        return _action(intent, response, function)

    if intent == "get_time":
        return _query(intent, describe_time)

    if intent == "get_weather":
        return _query(intent, describe_weather)

    if intent == "get_system_status":
        return _query(intent, describe_system)

    return None
