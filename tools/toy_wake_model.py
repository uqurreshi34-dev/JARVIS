"""The toy's own ear for "Hey Jarvis": the microWakeWord model, made ready for the toy cam's build.

The toy listens all the time. Only what is said with "Hey Jarvis" (or as a follow-up just after an
answer, or with BOOT held) ever leaves it: the board hears the wake word itself.

The ear is Home Assistant's "Hey Jarvis" microWakeWord model (Apache 2.0, by Kevin Ahrendt, from
esphome/micro-wake-word-models), trained on thousands of voices, so it works for anyone, and made
for the ESP32-S3. It runs on TensorFlow Lite Micro, already in the toy's Arduino core. Its sound
goes in as the TensorFlow micro audio frontend's 40 features every 10 ms (Apache 2.0, from
TensorFlow v2.18.0), whose sources are fetched here too: the core has the frontend's FFT
(kiss_fft_fixed16) but not the frontend itself.

Everything is fetched from GitHub, checked by SHA-256, and written under
arduino/jarvis_camera/wake/ (kept out of git): hey_jarvis_model.h (the model as a C array, with its
settings) and tensorflow/lite/experimental/microfrontend/lib/. The toy's PlatformIO build compiles
them when they are there; without them, it sends every recording to be checked for Jarvis.

    python tools/toy_wake_model.py
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "arduino" / "jarvis_camera" / "wake"

MODELS_COMMIT = "40ff33f57f8fc6ad71a75ef085abbf742495b225"
MODEL_URL = "https://raw.githubusercontent.com/esphome/micro-wake-word-models/{commit}/models/v2/{file}"
MODEL_FILES = {
    "hey_jarvis.tflite": "21a7976add39ee24ec96c63d96b7aaa18e24d1d9824b963e451da8feb4b78b77",
    "hey_jarvis.json": "b153867d818675d8abcc9dace474afe7f83551ae0d5a9b1d71a98681320185af",
}

FRONTEND_VERSION = "v2.18.0"
FRONTEND_PATH = "tensorflow/lite/experimental/microfrontend/lib"
FRONTEND_URL = "https://raw.githubusercontent.com/tensorflow/tensorflow/{version}/" + FRONTEND_PATH + "/{file}"
FRONTEND_FILES = {
    "bits.h": "e1b749fc16c2e82e08235d7004931782f4538210bf29985381e65ac1b0885ad8",
    "fft.cc": "a0fa0af8551056d8caa34127e35589c304f2e089d5a459f309d234994527017c",
    "fft.h": "5d730a623766027dc2326a5131c65be6ec8c6c900f34a2d05d726a9475eab509",
    "fft_util.cc": "bda0186e012bdfd6946673c8f18dd3587384f466d14b4e98c04540a6d9b5afb1",
    "fft_util.h": "bd613d33db3795a43ab27e2155f6a47611ecc83c6ec6ff2ae1bec338a49ec928",
    "filterbank.c": "ee5274e05be0ac58d9e185b9b074d3682c09aa88695130498441a99d53626e19",
    "filterbank.h": "46159287a955c8ead10d73bb5e94c18679f9a805454f09f3f6979b0678fe333e",
    "filterbank_util.c": "d511136f84e3650904cf82c7227bd9ddecb629e2f8a027a446b598c0e9113543",
    "filterbank_util.h": "63bafdec450bfc89278fd0c93a6015d109d38f04ec35743ab222c471fa6e3302",
    "frontend.c": "0598d172c3b75c81c55ca64652f5d179dba35939840af64af1b87b6200b64300",
    "frontend.h": "46ac313857158ea99148f648d4f3c45fca0e78b58757882b5b524803337a8fed",
    "frontend_util.c": "06f926fdb4276f93c2f9b6ba2a00a2273a5f4f9039b8007cf289ce6438b207bc",
    "frontend_util.h": "a46432fb2dfd0af985a3d20ede2f6c9e3c0501500fd155491006c867c49cdce1",
    "log_lut.c": "057c040484526b8a6f84e95bb040d90da3f48d93e846ab8aa1c8e9f46c812e08",
    "log_lut.h": "38bc096e21af8494866080d66438454da2382ee910edbd4a36b77eb9e824a3c0",
    "log_scale.c": "ffcc82bdb44df8e219c186f4137be1d90b9697ad98575fe48b4fc321958874dd",
    "log_scale.h": "2c90d6bc5fd5620f4b30e9ceb61fef689bac358819b56b2b2aec3f8120363150",
    "log_scale_util.c": "650875cbdfbbc89ae2627bfac7d29939d3d204822b1ddf2a8f6eaaf707cd2390",
    "log_scale_util.h": "c3d096b028a3cb1230c6895cc51212114c79a825ed91996c7b934293f81a241a",
    "noise_reduction.c": "73b688d001dea7e9d543cf9d51549ea8c2a163f3b6145e6158adf208c5eea8ac",
    "noise_reduction.h": "f5f9b8c303f9c28357bf5ba17867b483ee0a2bf9c1732dc740834a7ca6ce66aa",
    "noise_reduction_util.c": "116afa4b0fb79cc83aea1a2eaf023c1f76ca6b64c172aec2014f8495f499672e",
    "noise_reduction_util.h": "e02d09d5873e5a5826da1d73b6a972af49dddc23b54db7245e52a4ba07a4a445",
    "pcan_gain_control.c": "b0119d03931f312c10fce099cfef7e01874d5aad085069c1129de2c679e2cf60",
    "pcan_gain_control.h": "820ab1e4c8712661c0af4c5a579552b9cafb3ad6ea622329028a6f9430fbe575",
    "pcan_gain_control_util.c": "4481862cc4d2dd2c8267791334ea9589ece77657d29231e3417d7c88574361bb",
    "pcan_gain_control_util.h": "9715d7b9c03ae534f28fa4598da26621b99c17a049a28099ed7ea68a987876e5",
    "window.c": "9facbb75884f4bdd0f90d8d876e547d7213bb99344fa6ccb682e45290c36dbfa",
    "window.h": "e4ca258111c2b0ffbd9e702cb9586de095c5c86cca5709a13c1b85dc79026075",
    "window_util.c": "e81e2b7b7c284525f6ae80621f68e6a377c19bdb51fc4e2523f3884f0788bc13",
    "window_util.h": "d069df14ec078dcc45f61c4198fc050f0fe37439b1fd3c328400468a58e625a4",
}

# The core's FFT is kiss fft in its own namespace (signal/src/kiss_fft_wrappers), not the global one
# these two files were written for.
KISS_GLOBAL = '#define FIXED_POINT 16\n#include "kiss_fft.h"\n#include "kiss_fftr.h"\n'
KISS_CORE = '#include "signal/src/kiss_fft_wrappers/kiss_fft_int16.h"\nusing namespace kiss_fft_fixed16;\n'


class ModelError(Exception):
    pass


def fetch(url, digest, opener=urllib.request.urlopen):
    with opener(url, timeout=60) as response:
        content = response.read()

    if hashlib.sha256(content).hexdigest() != digest:
        raise ModelError(f"{url} is not the file expected")

    return content


def for_the_core(name, content):
    """[content] of frontend file [name], its FFT the core's own."""
    if name not in ("fft.cc", "fft_util.cc"):
        return content

    text = content.decode("utf-8")

    if KISS_GLOBAL not in text:
        raise ModelError(f"{name} does not include kiss fft as expected")

    return text.replace(KISS_GLOBAL, KISS_CORE).encode("utf-8")


def model_header(model, settings):
    """hey_jarvis_model.h: the model as a C array, aligned for TensorFlow Lite Micro, and its settings."""
    micro = settings["micro"]

    if settings.get("version") != 2 or micro.get("feature_step_size") != 10:
        raise ModelError("the model is not a version 2 microWakeWord model with 10 ms features")

    rows = [", ".join(f"0x{byte:02x}" for byte in model[at:at + 16]) for at in range(0, len(model), 16)]
    body = ",\n  ".join(rows)
    return (
        "// The toy's \"Hey Jarvis\" ear (tools/toy_wake_model.py). Kept out of git. Rewritten on every run.\n"
        f"// {settings['wake_word']} by {settings['author']}, esphome/micro-wake-word-models {MODELS_COMMIT[:7]},"
        " Apache 2.0.\n"
        "#pragma once\n"
        "#include <stdint.h>\n"
        "\n"
        f'#define WAKE_WORD_NAME "{settings["wake_word"]}"\n'
        f"#define WAKE_CUTOFF {float(micro['probability_cutoff'])}f\n"
        f"#define WAKE_SLIDING_WINDOW {int(micro['sliding_window_size'])}\n"
        f"#define WAKE_TENSOR_ARENA {int(micro['tensor_arena_size'])}\n"
        f"#define WAKE_FEATURE_STEP_MS {int(micro['feature_step_size'])}\n"
        "\n"
        f"static const unsigned int WAKE_MODEL_SIZE = {len(model)};\n"
        f"alignas(16) static const uint8_t WAKE_MODEL[] = {{\n  {body}\n}};\n"
    )


def build(out=OUT, opener=urllib.request.urlopen):
    model = fetch(MODEL_URL.format(commit=MODELS_COMMIT, file="hey_jarvis.tflite"),
                  MODEL_FILES["hey_jarvis.tflite"], opener)
    settings = json.loads(fetch(MODEL_URL.format(commit=MODELS_COMMIT, file="hey_jarvis.json"),
                                MODEL_FILES["hey_jarvis.json"], opener))
    frontend = {name: for_the_core(name, fetch(FRONTEND_URL.format(version=FRONTEND_VERSION, file=name), digest,
                                               opener))
                for name, digest in FRONTEND_FILES.items()}

    header = model_header(model, settings)
    folder = out / FRONTEND_PATH
    folder.mkdir(parents=True, exist_ok=True)

    for name, content in frontend.items():
        (folder / name).write_bytes(content)

    (out / "hey_jarvis_model.h").write_text(header, encoding="ascii")
    return out


def main():
    try:
        out = build()
    except (OSError, ValueError, KeyError, ModelError) as error:
        print(f"Could not make the wake word ear: {error}")
        return 1

    print(f"The 'Hey Jarvis' ear is ready in {out}.")
    print("Flash the toy as usual (pio run -e aicamdetect -t upload --upload-port COM8): it is built in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
