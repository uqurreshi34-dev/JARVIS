from commands import handle_command
from speech import speak
from voice import listen


speak("Good evening. JARVIS is online.")


while True:
    command = listen()

    if not command:
        continue

    if command.lower().strip() == "quit":
        speak("Shutting down.")
        break

    result = handle_command(command)

    if not result:
        speak("I don't know how to do that yet.")
        continue

    speak(result["response"])

    success = result["action"]()

    if success:
        speak("Done, sir.")
    elif result["intent"] == "open_application":
        speak("I couldn't open the application, sir.")
    elif result["intent"] == "close_application":
        speak("I couldn't close the application, sir.")
