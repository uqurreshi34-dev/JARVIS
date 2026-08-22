"""Compare voice settings by ear, then put the winner in .env.

Run:  python voice_lab.py
      python voice_lab.py "any sentence you like"

Nothing is changed; each option is spoken with its label so you can pick.
"""

import asyncio
import os
import sys
import tempfile

import edge_tts
import numpy as np
import sounddevice as sd
import soundfile as sf


LINE = " ".join(sys.argv[1:]) or (
    "Good evening, sir. Battery at seventy two percent, "
    "and the news is on screen."
)

# Voice, rate, pitch. The first is the current default.
OPTIONS = [
    ("current default", "en-GB-RyanNeural", "", ""),
    ("Ryan, a touch slower", "en-GB-RyanNeural", "-4%", ""),
    ("Ryan, slightly lower", "en-GB-RyanNeural", "", "-3Hz"),
    ("Ryan, slower and lower", "en-GB-RyanNeural", "-4%", "-3Hz"),
    ("Ryan, brisk", "en-GB-RyanNeural", "+6%", ""),
    ("Thomas", "en-GB-ThomasNeural", "", ""),
    ("Thomas, a touch slower", "en-GB-ThomasNeural", "-4%", ""),
    ("Thomas, slightly lower", "en-GB-ThomasNeural", "", "-3Hz"),
    ("Ollie", "en-GB-OllieMultilingualNeural", "", ""),
    ("Ryan, measured", "en-GB-RyanNeural", "-8%", "-2Hz"),
]


async def synthesise(text, voice, rate, pitch, path):
    options = {}

    if rate:
        options["rate"] = rate

    if pitch:
        options["pitch"] = pitch

    await edge_tts.Communicate(text, voice, **options).save(path)


def play(path):
    data, samplerate = sf.read(path, dtype="float32")

    if data.ndim > 1:
        data = data.mean(axis=1)

    sd.play(data, samplerate)
    sd.wait()


print(f"Line: {LINE!r}")
print()
print("Listen through, note the number you like, then set it in .env.")
print("-" * 62)

folder = os.path.join(tempfile.gettempdir(), "jarvis_voice_lab")
os.makedirs(folder, exist_ok=True)

for index, (label, voice, rate, pitch) in enumerate(OPTIONS, start=1):
    path = os.path.join(folder, f"option_{index}.mp3")

    print(f"{index:2}. {label}")
    print(f"    voice {voice}  rate {rate or 'default'}  "
          f"pitch {pitch or 'default'}")

    try:
        if not os.path.exists(path):
            asyncio.run(synthesise(LINE, voice, rate, pitch, path))

        play(path)

    except Exception as error:
        print(f"    could not play this one: {error}")
        continue

    print()

print("-" * 62)
print("To use one, put its settings in .env, for example:")
print()
print("  JARVIS_VOICE=en-GB-RyanNeural")
print("  JARVIS_VOICE_RATE=-4%")
print("  JARVIS_VOICE_PITCH=-3Hz")
print()
print("Leave a value empty for that voice's own default.")
print()
print("Note: the phrase cache keys on these settings, so the first few")
print("replies after a change are synthesised fresh.")
