"""Hear Google's British voices say a JARVIS line, to choose one by ear.

    python tools/voice_samples.py                 every British man's Chirp 3 HD voice
    python tools/voice_samples.py --women         the women's too
    python tools/voice_samples.py "Your line."    a line of your own

Asks Google which Chirp 3 HD voices it has for en-GB (none are listed
here), says the line in each, and saves one MP3 per voice in "Voice
samples" in the JARVIS folder. The one you like goes in .env:

    GOOGLE_TTS_VOICE=en-GB-Chirp3-HD-Achird

Needs GOOGLE_TTS_API_KEY in .env. Each sample counts towards the month's
free characters like anything JARVIS says (about 130 characters each).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import voices  # noqa: E402
from actions import files  # noqa: E402

LINE = ("Good afternoon, sir. Bitcoin is up a tenth of a percent since three o'clock. "
        "Shall I bring up the chart?")


def main(arguments):
    women = "--women" in arguments
    words = [argument for argument in arguments if not argument.startswith("--")]
    line = " ".join(words) or LINE

    if not voices.google_configured():
        print(f"Put {voices.GOOGLE_KEY_ENV} in .env first.")
        return 1

    try:
        found = voices.google_voices("en-GB")
    except Exception as error:
        print(f"Could not list Google's voices: {error}")
        return 1

    chosen = [name for name, gender in found if women or gender == "MALE"]

    if not chosen:
        print("Google lists no Chirp 3 HD voices for en-GB on this key.")
        return 1

    folder = os.path.join(files.root(), "Voice samples")
    os.makedirs(folder, exist_ok=True)

    for name in chosen:
        try:
            audio, _timings = voices.google_mp3(line, voice=name)
        except Exception as error:
            print(f"{name}: {error}")
            continue

        path = os.path.join(folder, f"{name}.mp3")

        with open(path, "wb") as handle:
            handle.write(audio)

        print(f"{name}: {path}")

    print(f"\nPlay them, then put the one you like in .env as {voices.GOOGLE_VOICE_ENV}=<name>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
