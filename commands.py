import os
import shutil
import subprocess


def launch_application(app_name):
    """Launch a Windows application using multiple fallback methods."""

    executable = shutil.which(app_name)

    if executable:
        subprocess.Popen(
            [executable],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    try:
        os.startfile(app_name)
        return True
    except OSError:
        return False


def handle_command(command):
    command = command.lower().strip()

    if "open chrome" in command or "launch chrome" in command:
        return {
            "response": "Opening Chrome, sir.",
            "action": lambda: launch_application("chrome"),
        }

    return None
