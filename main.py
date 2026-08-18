# import pyttsx3

# engine = pyttsx3.init()

# engine.say("Good evening, Umer. JARVIS is online.")
# engine.runAndWait()

from voice import listen


print("JARVIS microphone test")
print("----------------------")

while True:
    command = listen()

    if command.lower() == "quit":
        print("JARVIS shutting down.")
        break
