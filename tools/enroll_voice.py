"""Teach JARVIS your voice, so he answers only you -- not a video, a phone on speaker, or a friend.

    python tools/enroll_voice.py                 record a few clips of you speaking and save your voiceprint
    python tools/enroll_voice.py --check         say something: how much it sounds like you, and whether he would answer
    python tools/enroll_voice.py --add           record more clips -- another spot, distance or time of day -- and
                                                 add them to the voiceprint you have, when he misses you
    python tools/enroll_voice.py --report        how your voice has scored as JARVIS heard it, from jarvis-log.txt
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
import re
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


def enrol(model, clips, threshold, earlier=()):
    """Save a voiceprint from [clips], added to the [earlier] clips' vectors when there are any."""
    earlier = [np.asarray(vector, dtype=np.float64) for vector in earlier]
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

    vectors = earlier + vectors

    # Each clip against a voiceprint made from the others: how you will score in use.
    scores = [float(vector @ speaker.centroid(vectors[:index] + vectors[index + 1:]))
              for index, vector in enumerate(vectors)]
    voiceprint = speaker.centroid(vectors)
    speaker.save_voiceprint({"enabled": True, "threshold": threshold, "voiceprint": voiceprint, "clips": len(vectors),
                             "clip_vectors": vectors, "model_sha256": speaker.MODEL_SHA256}, replace=True)

    print(f"\nSaved your voiceprint from {len(vectors)} clips{f' ({len(earlier)} of them from before)' if earlier else ''}. Each clip scored {min(scores):.2f} to {max(scores):.2f}"
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
    which.add_argument("--add", action="store_true")
    which.add_argument("--report", action="store_true")
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

    if options.report:
        return report()

    needs_one = options.on or options.off or options.check or options.add

    try:
        saved = speaker.load_voiceprint()
    except ValueError as error:
        if needs_one:
            print(f"{error}.")
            return 1

        saved = None   # enrolling again replaces it

    if needs_one and saved is None:
        print("No voiceprint yet: run python tools/enroll_voice.py first.")
        return 1

    if options.on or options.off:
        speaker.save_voiceprint({"enabled": bool(options.on)})
        print(f"Voice check {'on: he answers only you' if options.on else 'off: he answers anyone'}."
              " Restart JARVIS for it to take effect.")
        return 0

    # A new threshold for the voiceprint you have; with none yet, it is the one you enrol with.
    if options.threshold is not None and saved is not None and not (options.check or options.wav or options.add):
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

    earlier = ()

    if options.add:
        # A voiceprint saved before clips were kept counts as its clips, all alike.
        earlier = (saved["clip_vectors"] if saved["clip_vectors"] is not None
                   else [saved["voiceprint"]] * int(saved.get("clips") or 1))

    return enrol(model, clips, threshold, earlier)


def report(lines=None):
    """What jarvis-log.txt says about how your voice has scored: let through, and missed narrowly."""
    from actions import journal

    lines = journal.recent(journal.MAX_LINES) if lines is None else lines
    heard, missed = [], []

    for line in lines:
        found = re.search(rf"\s{speaker.JOURNAL_KIND}\s+(yours|near miss): sounds ([0-9.]+) like you", line)

        if found:
            (heard if found.group(1) == "yours" else missed).append((line[:16], float(found.group(2))))

    if not heard and not missed:
        print("Nothing in jarvis-log.txt yet: talk to JARVIS for a while with the voice check on, then look again.")
        return 0

    if heard:
        scores = sorted(score for _when, score in heard)
        print(f"Let through as you: {len(scores)} times, scoring {scores[0]:.2f} to {scores[-1]:.2f}"
              f" (half above {scores[len(scores) // 2]:.2f}).")

    if missed:
        print(f"Missed narrowly ({speaker.NEAR_MISS:.2f} or less below the threshold): {len(missed)} times. The latest:")

        for when, score in missed[-10:]:
            print(f"  {when}  {score:.2f}")

        print("If those were you, add clips from where you were (--add); or lower the bar just under them with"
              f" --threshold -- keeping it above {speaker.SAFE_THRESHOLD:.2f}, where other people's voices start to"
              " get through.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
