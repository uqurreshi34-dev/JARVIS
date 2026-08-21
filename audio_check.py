"""Look inside the cached speech files and find where sound actually starts.

Run:  python audio_check.py
"""

import os

import numpy as np
import soundfile as sf

import speech
from actions.files import describe_listing, describe_listing_named


engine = speech.speech

sentences = [
    ("files, brief", describe_listing()),
    ("files, named", describe_listing_named()),
    ("time", "It's twelve fifty six PM on Friday, the 21st of August."),
    ("stock phrase", "Done, sir."),
]

print(f"cache folder: {speech._CACHE_DIR}")
print()

for label, sentence in sentences:
    key = engine._cache_key(sentence)
    path = os.path.join(speech._CACHE_DIR, f"{key}.mp3")

    print(f"--- {label} ---")
    print(f"  {sentence[:64]!r}")

    if not os.path.exists(path):
        print("  not cached yet")
        print()
        continue

    size = os.path.getsize(path)

    try:
        data, rate = sf.read(path, dtype="float32")
    except Exception as error:
        print(f"  could not decode: {error}")
        print()
        continue

    if data.ndim > 1:
        data = data.mean(axis=1)

    duration = len(data) / rate
    peak = float(np.abs(data).max()) if len(data) else 0.0

    # Where does sound actually begin?
    loud = np.where(np.abs(data) > 0.01)[0]

    if len(loud):
        starts = loud[0] / rate
        ends = loud[-1] / rate
    else:
        starts = ends = None

    print(f"  file size      {size} bytes")
    print(f"  duration       {duration:.2f}s")
    print(f"  peak level     {peak:.4f}")

    if starts is None:
        print("  SOUND STARTS   never - this file is silent")
    else:
        print(f"  sound starts   {starts:.2f}s")
        print(f"  sound ends     {ends:.2f}s")

        if starts > 0.5:
            print(
                f"  >>> {starts:.2f}s OF LEADING SILENCE - this is the delay")

    print()
