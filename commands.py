from actions.applications import ApplicationManager
from llm import interpret


_application_manager = ApplicationManager()


def handle_command(command):
    result = interpret(
        command,
        _application_manager.applications,
    )

    intent = result.get("intent")
    application = result.get("application")

    if not application:
        return None

    if intent == "open_application":
        return {
            "intent": intent,
            "response": f"Opening {application}, sir.",
            "action": lambda: _application_manager.launch(application),
        }

    if intent == "close_application":
        return {
            "intent": intent,
            "response": f"Closing {application}, sir.",
            "action": lambda: _application_manager.close(application),
        }

    return None
