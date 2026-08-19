import webbrowser
from urllib.parse import urlparse

from actions.applications import ApplicationManager
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
        return {
            "intent": intent,
            "response": f"Opening {application}, sir.",
            "action": lambda: _application_manager.launch(application),
        }

    if intent == "close_application" and application:
        return {
            "intent": intent,
            "response": f"Closing {application}, sir.",
            "action": lambda: _application_manager.close(application),
        }

    if intent == "open_website" and website:
        label = _website_label(website)

        return {
            "intent": intent,
            "response": f"Opening {label}, sir.",
            "action": lambda: _open_website(website),
        }

    return None
