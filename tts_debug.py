"""Diagnose why the neural voice isn't playing. Run: python tts_debug.py"""

import asyncio
import os
import tempfile
import traceback

print("=== 1. imports ===")

try:
    import edge_tts
    print("edge_tts        OK")
except Exception:
    traceback.print_exc()
    raise SystemExit("edge_tts import failed")

try:
    import soundfile as sf
    print("soundfile       OK", sf.__version__,
          "| libsndfile", sf.__libsndfile_version__)
    print("MP3 decodable?  ", "MP3" in sf.available_formats())
except Exception:
    traceback.print_exc()
    raise SystemExit("soundfile import failed")

try:
    import sounddevice as sd
    print("sounddevice     OK", sd.__version__)
except Exception:
    traceback.print_exc()
    raise SystemExit("sounddevice import failed")

print()
print("=== 2. output devices ===")

try:
    print("default output:", sd.default.device)
    for index, device in enumerate(sd.query_devices()):
        if device["max_output_channels"] > 0:
            mark = "<= DEFAULT" if index == sd.default.device[1] else ""
            print(f"  [{index}] {device['name']} {mark}")
except Exception:
    traceback.print_exc()

print()
print("=== 3. synthesize ===")

path = os.path.join(tempfile.gettempdir(), "jarvis_tts_debug.mp3")


async def synth():
    c = edge_tts.Communicate(
        "Good evening. JARVIS is online.", "en-GB-RyanNeural")
    await c.save(path)

try:
    asyncio.run(synth())
    print("wrote", path, os.path.getsize(path), "bytes")
except Exception:
    traceback.print_exc()
    raise SystemExit("synthesis failed")

print()
print("=== 4. decode ===")

try:
    data, samplerate = sf.read(path, dtype="float32")
    print("decoded OK | samplerate", samplerate, "| frames", len(data),
          "| shape", data.shape)
    print("duration %.2fs" % (len(data) / samplerate))
    peak = float(abs(data).max()) if len(data) else 0.0
    print("peak amplitude %.4f" % peak)
    if peak < 0.001:
        print("WARNING: audio is effectively silent")
except Exception:
    traceback.print_exc()
    raise SystemExit("decode failed -- this is the culprit")

print()
print("=== 5. playback ===")

try:
    print("playing... you should hear a British male voice now")
    sd.play(data, samplerate)
    sd.wait()
    print("playback returned without error")
except Exception:
    traceback.print_exc()
    raise SystemExit("playback failed -- this is the culprit")

print()
print("=== DONE ===")
print("If every step said OK but you heard nothing, the audio went to the")
print("wrong output device -- check the device list in section 2.")
