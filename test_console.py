import time

from commands import _application_manager, handle_command
from speech import speak


# Set to False to test logic quickly without waiting for speech.
SPEAK = True


def say(text):
    if SPEAK:
        speak(text)
    else:
        print(f"JARVIS: {text}")


def run_direct(line):
    verb, _, name = line.partition(" ")
    name = name.strip()

    if not name:
        print("  direct mode: use '!open <name>' or '!close <name>'")
        return

    if verb == "open":
        ok = _application_manager.launch(name)
    elif verb == "close":
        ok = _application_manager.close(name)
    else:
        print("  direct mode: use '!open <name>' or '!close <name>'")
        return

    print(f"  -> {'success' if ok else 'failed'}")


def run_action(result):
    say(result["response"])

    start = time.monotonic()

    try:
        success = result["action"]()
    except Exception as error:
        print(f"  action error: {error}")
        success = False

    elapsed = time.monotonic() - start

    if success:
        say("Done, sir.")
    elif result["intent"] == "close_application":
        say("I couldn't close the application, sir.")
    elif result["intent"] in ("open_application", "open_website", "open_project"):
        say("I couldn't open that, sir.")
    else:
        say("That didn't work, sir.")

    print(f"  -> {'success' if success else 'failed'} ({elapsed:.1f}s)")


def run_query(result):
    start = time.monotonic()

    try:
        answer = result["action"]()
    except Exception as error:
        print(f"  query error: {error}")
        answer = None

    elapsed = time.monotonic() - start

    say(answer if answer else "I couldn't find that out, sir.")

    print(f"  -> {'answered' if answer else 'no answer'} ({elapsed:.1f}s)")


print("JARVIS console.")
print("  Full pipeline (LLM + manager):  open outlook")
print("  Bypass the LLM (manager only):  !close outlook")
print("  Quit:                           quit")
print()

while True:
    try:
        line = input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        break

    if not line:
        continue

    if line.lower() == "quit":
        break

    if line.startswith("!"):
        run_direct(line[1:].strip())
        continue

    result = handle_command(line)

    if not result:
        say("I don't know how to do that yet.")
        continue

    if result.get("kind") == "query":
        run_query(result)
    else:
        run_action(result)
