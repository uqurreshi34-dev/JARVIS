import re

from actions.applications import ApplicationManager


_application_manager = ApplicationManager()


def handle_command(command):
    command = command.lower().strip()

    match = re.match(r"^(open|launch|start|close|quit|exit)\s+(.+)$", command)

    if not match:
        return None

    action, application_name = match.groups()

    application_name = application_name.strip()

    if action in {"open", "launch", "start"}:
        return {
            "response": f"Opening {application_name}, sir.",
            "action": lambda: _application_manager.launch(application_name),
        }

    if action in {"close", "quit", "exit"}:
        return {
            "response": f"Closing {application_name}, sir.",
            "action": lambda: _application_manager.close(application_name),
        }

    return None
