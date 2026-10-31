"""Checks for the toy's own "Hey Jarvis" ear: the tool that fetches the microWakeWord model and the
audio frontend, and the firmware that only lets what was said to Jarvis leave the toy. No network:
the downloads are faked.

    python tools/test_toy_wake_model.py
"""

import hashlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import toy_wake_model as wake  # noqa: E402

RESULTS = []


def check(ok, label):
    RESULTS.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label)


# ---- what is fetched, and from where ------------------------------------------------------------------

check(re.fullmatch(r"[0-9a-f]{40}", wake.MODELS_COMMIT) and wake.FRONTEND_VERSION == "v2.18.0",
      "the model at one fixed commit of esphome/micro-wake-word-models, the frontend at TensorFlow v2.18.0")
check(all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in {**wake.MODEL_FILES, **wake.FRONTEND_FILES}.values()),
      "every file pinned by SHA-256")
check({"frontend.c", "frontend_util.c", "fft.cc", "fft_util.cc", "filterbank.c", "noise_reduction.c",
       "pcan_gain_control.c", "log_scale.c", "window.c"} <= set(wake.FRONTEND_FILES),
      "the whole micro audio frontend, as the model was trained with")

# ---- building, with the downloads faked ---------------------------------------------------------------

settings = {"type": "micro", "wake_word": "Hey Jarvis", "author": "Kevin Ahrendt", "model": "hey_jarvis.tflite",
            "version": 2, "micro": {"probability_cutoff": 0.97, "feature_step_size": 10, "sliding_window_size": 5,
                                    "tensor_arena_size": 22860}}
model = bytes(range(256)) * 3
files = {"hey_jarvis.tflite": model, "hey_jarvis.json": json.dumps(settings).encode()}

for name in wake.FRONTEND_FILES:
    files[name] = (wake.KISS_GLOBAL if name in ("fft.cc", "fft_util.cc") else "").encode() + name.encode()


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


saved = dict(wake.MODEL_FILES), dict(wake.FRONTEND_FILES)
wake.MODEL_FILES.update({name: hashlib.sha256(files[name]).hexdigest() for name in wake.MODEL_FILES})
wake.FRONTEND_FILES.update({name: hashlib.sha256(files[name]).hexdigest() for name in wake.FRONTEND_FILES})
asked = []


def opener(url, timeout=None):
    asked.append(url)
    return Reply(files[url.rsplit("/", 1)[1]])


with tempfile.TemporaryDirectory() as folder:
    out = wake.build(out=Path(folder) / "wake", opener=opener)
    header = (out / "hey_jarvis_model.h").read_text(encoding="ascii")
    check('#define WAKE_WORD_NAME "Hey Jarvis"' in header and "#define WAKE_CUTOFF 0.97f" in header
          and "#define WAKE_SLIDING_WINDOW 5" in header and "#define WAKE_TENSOR_ARENA 22860" in header
          and f"WAKE_MODEL_SIZE = {len(model)};" in header and "alignas(16) static const uint8_t WAKE_MODEL[]" in header,
          "hey_jarvis_model.h: the model as an aligned C array, with its own settings")
    check(header.count("0x") == len(model), "every byte of the model in it")
    lib = out / wake.FRONTEND_PATH
    fft = (lib / "fft.cc").read_text()
    check(wake.KISS_CORE in fft and wake.KISS_GLOBAL not in fft and (lib / "frontend.c").is_file(),
          "the frontend under wake/tensorflow/..., its FFT the core's own kiss fft (kiss_fft_fixed16)")
    check(any("esphome/micro-wake-word-models/" + wake.MODELS_COMMIT in url for url in asked)
          and any("tensorflow/tensorflow/v2.18.0/" in url for url in asked),
          "fetched from those repositories at those versions")

    def tampered(url, timeout=None):
        return Reply(b"tampered")

    try:
        wake.build(out=Path(folder) / "bad", opener=tampered)
        refused = False
    except wake.ModelError:
        refused = True

    check(refused and not (Path(folder) / "bad").exists(), "a file that is not the one expected is refused")

    try:
        wake.model_header(model, {**settings, "version": 1})
        old_refused = False
    except wake.ModelError:
        old_refused = True

    check(old_refused, "only a version 2 model with 10 ms features, as the firmware runs it")

wake.MODEL_FILES.clear()
wake.MODEL_FILES.update(saved[0])
wake.FRONTEND_FILES.clear()
wake.FRONTEND_FILES.update(saved[1])

# ---- the build ----------------------------------------------------------------------------------------

sketch_dir = ROOT / "arduino" / "jarvis_camera"
ini = (sketch_dir / "platformio.ini").read_text(encoding="ascii")
aicam = ini[ini.index("[env:aicam]"):ini.index("[env:aicamdetect]")]
check("-I wake" in aicam and "+<wake/tensorflow/lite/experimental/microfrontend/lib/*.c>" in aicam
      and "+<wake/tensorflow/lite/experimental/microfrontend/lib/*.cc>" in aicam,
      "the toy's builds compile the ear from wake/ when it is there")
detect = ini[ini.index("[env:aicamdetect]"):]
check("+<wake/tensorflow/lite/experimental/microfrontend/lib/*.cc> +<detect/*.c> +<faces/*.c>" in detect,
      "aicamdetect too, beside its detection")
check("toy_wake_image" not in ini and not (sketch_dir / "toy_partitions.csv").exists(),
      "no flash partition or extra upload step: the ear is in the firmware itself")
ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("arduino/jarvis_camera/wake/" in ignored, "the ear itself kept out of git")

# ---- the firmware -------------------------------------------------------------------------------------

sketch = (sketch_dir / "jarvis_camera.ino").read_text(encoding="ascii")
check('__has_include("hey_jarvis_model.h")' in sketch and "#define JARVIS_WAKE" in sketch
      and "esp_srmodel" not in sketch and "esp_wn_" not in sketch,
      "built with the ear when wake/ has it, and ESP-SR's WakeNet gone")
check("wakeFrontendConfig.window.size_ms = 30;" in sketch and "filterbank.num_channels = WAKE_FEATURES;" in sketch
      and "lower_band_limit = 125.0f" in sketch and "upper_band_limit = 7500.0f" in sketch
      and "pcan_gain_control.strength = 0.95f" in sketch and "log_scale.scale_shift = 6" in sketch,
      "the frontend set as the model was trained")
check("((int32_t)values[i] * 256 + 333) / 666 - 128" in sketch,
      "each feature scaled to the model's int8 as in training")
check("MicroResourceVariables::Create(allocator, 20)" in sketch and "wakeOps.AddVarHandle()" in sketch
      and "wakeOps.AddSplitV()" in sketch, "a streaming model: its state kept between runs, every op it uses")
check("total <= WAKE_CUTOFF * 255.0f * WAKE_SLIDING_WINDOW" in sketch and "wakeCooling = -WAKE_COOL_SLICES;" in sketch,
      "heard when its last few outputs average over its cutoff, then a second's rest before hearing it again")
task = sketch[sketch.index("void talkTask(void *)"):sketch.index("static void chime()")]
check(task.count("listenForWake(frame);") == 2, "every frame heard, waiting and recording, goes past the ear")
check("if (wakeReady && !button && !named && !following) {" in task
      and task.index("!named && !following") < task.index("askAndPlay(got, button, named)"),
      "a recording without Hey Jarvis, BOOT or a follow-up is kept on the toy, never sent")
check("followUntil = answered > 0 ? asked + TOY_FOLLOW_MS : 0;" in task and "TOY_FOLLOW_MS = 12000UL" in sketch,
      "follow-ups for twelve seconds after an answer, as the cloud's and the PC's window")
check("(long)(wakeHeardAt + WAKE_BEFORE_MS - began) >= 0" in task, "Hey Jarvis said just before the recording counts")
check("memcpy(talkBuffer + WAV_HEADER + got, frame, sizeof(frame));" in task,
      "what is sent is the sound as heard, never the ear's copy")
check("every recording goes to be checked for Jarvis" in sketch and "static bool startWake() {\n  Serial.println" in sketch,
      "built without the ear, every recording goes to be checked, as before, and the toy says so")
check(sketch.index("startWake();") < sketch.index("xTaskCreatePinnedToCore(talkTask"),
      "listening for Hey Jarvis starts before the talk does")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
