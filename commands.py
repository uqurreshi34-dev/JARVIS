import webbrowser
from urllib.parse import urlparse

from actions.applications import ApplicationManager
from actions.system import describe_time
from llm import CommandInterpreter


_application_manager = ApplicationManager()
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


def handle_command(command):
    candidates = _application_manager.candidates(command)

    result = _interpreter.interpret(
        command,
        candidates,
    )

    intent = result["intent"]
    application = result.get("application")
    website = result.get("website")

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

    if intent == "get_time":
        return _query(intent, describe_time)

    return None
