from actions.applications import ApplicationManager
from llm import interpret


manager = ApplicationManager()

tests = [
    "open Chrome",
    "would you mind closing Cursor",
    "please open p g ottoman",
    "I'm finished with Postman",
]


for command in tests:
    result = interpret(command, manager.applications)
    print(f"\nYou: {command}")
    print(f"JARVIS: {result}")
