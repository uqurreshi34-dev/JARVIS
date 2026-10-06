"""Teach JARVIS your voice, so he answers only you -- not a video, a phone on speaker, or a friend.

    python tools/enroll_voice.py                 record a few clips of you speaking and save your voiceprint
    python tools/enroll_voice.py --check         say something: how much it sounds like you, and whether he would answer
    python tools/enroll_voice.py --threshold 0.55
                                                 how alike a voice must be, 0.2 to 0.95 (default 0.5): raise it if
                                                 others get through, lower it if you are missed
    python tools/enroll_voice.py --off           answer anyone again (the voiceprint is kept)
    python tools/enroll_voice.py --on            answer only you again
    python tools/enroll_voice.py --wav a.wav b.wav ...
                                                 enrol from recordings of you instead (16 kHz mono WAV)

The first run downloads the voice model (about 26 MB, checked against its
SHA-256) into the JARVIS folder. Enrol in the room and with the microphone
JARVIS uses, as you normally talk to him; restart JARVIS afterwards.
Your voiceprint is voiceprint.json in the JARVIS folder; see speaker.py.
"""

import argparse
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import speaker  # noqa: E402


CLIPS = 8
NAME_SECONDS = 2.5
COMMAND_SECONDS = 5.0
CHECK_SECONDS = 4.0
FEWEST_CLIPS = 3


def record(seconds, prompt):
    import sounddevice

    input(f"\n{prompt}\n  Press Enter, then speak ({seconds:g} seconds)...")
    audio = sounddevice.rec(int(seconds * speaker.SAMPLE_RATE), samplerate=speaker.SAMPLE_RATE, channels=1,
                            dtype="float32")
    sounddevice.wait()
    return audio.reshape(-1)


def read_wav(path):
    import wave

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2 or handle.getframerate() != speaker.SAMPLE_RATE:
            raise ValueError(f"{path} is not 16-bit mono at {speaker.SAMPLE_RATE} Hz")

        frames = handle.readframes(handle.getnframes())

    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def prompts(count):
    for index in range(count):
        if index % 2 == 0:
            yield NAME_SECONDS, f"Clip {index + 1} of {count}: say his name, as you would to wake him."
        else:
            yield COMMAND_SECONDS, (f"Clip {index + 1} of {count}: give him a command or two, as you normally"
                                    " would, starting with his name.")


def enrol(model, clips, threshold):
    vectors = []

    for number, audio in enumerate(clips, 1):
        vector = model.embed(audio)

        if vector is None:
            print(f"  Clip {number}: too little speech in it; left out.")
            continue

        vectors.append(vector)

    if len(vectors) < FEWEST_CLIPS:
        print(f"Only {len(vectors)} usable clips; at least {FEWEST_CLIPS} are needed. Nothing saved.")
        return 1

    # Each clip against a voiceprint made from the others: how you will score in use.
    scores = [float(vector @ speaker.centroid(vectors[:index] + vectors[index + 1:]))
              for index, vector in enumerate(vectors)]
    voiceprint = speaker.centroid(vectors)
    speaker.save_voiceprint({"enabled": True, "threshold": threshold, "voiceprint": voiceprint,
                             "clips": len(vectors), "model_sha256": speaker.MODEL_SHA256}, replace=True)

    print(f"\nSaved your voiceprint from {len(vectors)} clips. Each clip scored {min(scores):.2f} to {max(scores):.2f}"
          f" against the others (threshold {threshold:.2f}).")

    if min(scores) < threshold:
        print("Some of your own clips scored below the threshold, so he may miss you at times: enrol again"
              " in a quieter room, or lower it with --threshold.")

    print("Restart JARVIS to use it. Check it with: python tools/enroll_voice.py --check")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--check", action="store_true")
    which.add_argument("--on", action="store_true")
    which.add_argument("--off", action="store_true")
    which.add_argument("--wav", nargs="+", metavar="FILE")
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--clips", type=int, default=CLIPS, help=f"how many clips to record (default {CLIPS})")
    options = parser.parse_args(argv)

    if options.threshold is not None and not speaker.LOWEST_THRESHOLD <= options.threshold <= speaker.HIGHEST_THRESHOLD:
        print(f"--threshold wants a number from {speaker.LOWEST_THRESHOLD} to {speaker.HIGHEST_THRESHOLD}.")
        return 1

    if options.clips < FEWEST_CLIPS:
        print(f"--clips wants {FEWEST_CLIPS} or more.")
        return 1

    try:
        saved = speaker.load_voiceprint()
    except ValueError as error:
        if options.on or options.off or options.check:
            print(f"{error}.")
            return 1

        saved = None   # enrolling again replaces it

    if (options.on or options.off or options.check) and saved is None:
        print("No voiceprint yet: run python tools/enroll_voice.py first.")
        return 1

    if options.on or options.off:
        speaker.save_voiceprint({"enabled": bool(options.on)})
        print(f"Voice check {'on: he answers only you' if options.on else 'off: he answers anyone'}."
              " Restart JARVIS for it to take effect.")
        return 0

    # A new threshold for the voiceprint you have; with none yet, it is the one you enrol with.
    if options.threshold is not None and saved is not None and not (options.check or options.wav):
        speaker.save_voiceprint({"threshold": options.threshold})
        print(f"Threshold {options.threshold:.2f}. Restart JARVIS for it to take effect.")
        return 0

    try:
        speaker.download_model()
        model = speaker.Model()
    except Exception as error:
        print(f"The voice model could not be made ready: {error}.")
        return 1

    if options.check:
        threshold = options.threshold if options.threshold is not None else saved["threshold"]
        score = speaker.Gate(saved["voiceprint"], threshold, model).score(
            record(CHECK_SECONDS, "Say something -- or play the video, or have someone else speak."))

        if score is None:
            print("Too little speech to tell.")
        else:
            print(f"Sounds {score:.2f} like you; {threshold:.2f} needed: JARVIS would "
                  f"{'answer' if score >= threshold else 'ignore'} it.")

        return 0

    threshold = options.threshold if options.threshold is not None else (
        saved["threshold"] if saved else speaker.DEFAULT_THRESHOLD)

    try:
        clips = [read_wav(path) for path in options.wav] if options.wav else [
            record(seconds, prompt) for seconds, prompt in prompts(options.clips)]
    except (OSError, ValueError) as error:
        print(f"{error}.")
        return 1

    return enrol(model, clips, threshold)


if __name__ == "__main__":
    sys.exit(main())
