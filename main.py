from voice import listen
from speech import speak
from commands import handle_command


speak("Good evening. JARVIS is online.")

while True:
    command = listen()

    if command.lower() == "quit":
        speak("Shutting down.")
        break

    result = handle_command(command)

    if result:
        speak(result["response"])

        if result["action"]():
            print("Action completed.")
        else:
            speak("I couldn't launch the application, sir.")
    else:
        speak("I don't know how to do that yet.")
