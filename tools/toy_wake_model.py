"""The toy's own ear for "Jarvis": Espressif's WakeNet model, packed for the toy cam's flash.

The toy listens all the time, and used to send everything it heard to the cloud (or the PC), which
then checked for "Jarvis" and threw the rest away. With this model the board hears "Jarvis" itself,
and only what is said with it (or as a follow-up just after an answer, or with BOOT held) ever
leaves the room.

The model is ESP-SR's wn9_jarvis_tts (Espressif's licence: free on Espressif chips), from the same
ESP-SR release (v1.9.5) as the library built into the toy's Arduino core, so the two match. Its
three files are fetched from GitHub, checked by SHA-256, and packed as ESP-SR's pack_model.py
does into arduino/jarvis_camera/wake/srmodels.bin (kept out of git). PlatformIO then writes that
file to the "model" partition with every upload of the toy (toy_wake_image.py).

    python tools/toy_wake_model.py
"""

import hashlib
import struct
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "arduino" / "jarvis_camera" / "wake" / "srmodels.bin"

ESP_SR_VERSION = "v1.9.5"
MODEL = "wn9_jarvis_tts"
URL = "https://raw.githubusercontent.com/espressif/esp-sr/{version}/model/wakenet_model/{model}/{file}"

# The files, and what each must be.
FILES = {
    "_MODEL_INFO_": "ee752fe7bd3eaa78b94e723a4782c2000014508848a53e31222ebe52be2d7970",
    "wn9_index": "c5557ca5d1ec6943540a6abbb3ea235a693f4637369f92e873ff566b73a61cfd",
    "wn9_data": "221e5d90fb5ce2d14e851f4c9affc0271c5b02e55a10deb671a4243d3ccdcc3f",
}

NAME_LENGTH = 32


class ModelError(Exception):
    pass


def _name(text):
    raw = text.encode("ascii")

    if len(raw) > NAME_LENGTH:
        raise ModelError(f"{text} is longer than {NAME_LENGTH} characters")

    return raw + b"\0" * (NAME_LENGTH - len(raw))


def pack(models):
    """ESP-SR's srmodels.bin for [models] ({model name: {file name: bytes}}), as its pack_model.py:
    the model count, then each model's name, file count and each file's name, start and length,
    then all the files' bytes in that order."""
    file_count = sum(len(files) for files in models.values())
    header_length = 4 + len(models) * (NAME_LENGTH + 4) + file_count * (NAME_LENGTH + 4 + 4)
    header = struct.pack("<I", len(models))
    data = b""

    for model, files in models.items():
        header += _name(model) + struct.pack("<I", len(files))

        for file_name, content in files.items():
            header += _name(file_name) + struct.pack("<II", header_length + len(data), len(content))
            data += content

    assert len(header) == header_length
    return header + data


def unpack(packed):
    """{model name: {file name: bytes}} from a srmodels.bin (to check one)."""
    count = struct.unpack_from("<I", packed, 0)[0]
    at = 4
    models = {}

    for _ in range(count):
        model = packed[at:at + NAME_LENGTH].rstrip(b"\0").decode("ascii")
        files = struct.unpack_from("<I", packed, at + NAME_LENGTH)[0]
        at += NAME_LENGTH + 4
        models[model] = {}

        for _ in range(files):
            file_name = packed[at:at + NAME_LENGTH].rstrip(b"\0").decode("ascii")
            start, length = struct.unpack_from("<II", packed, at + NAME_LENGTH)
            at += NAME_LENGTH + 8
            models[model][file_name] = packed[start:start + length]

    return models


def fetch(file_name, opener=urllib.request.urlopen):
    url = URL.format(version=ESP_SR_VERSION, model=MODEL, file=file_name)

    with opener(url, timeout=60) as response:
        content = response.read()

    if hashlib.sha256(content).hexdigest() != FILES[file_name]:
        raise ModelError(f"{file_name} from {url} is not the file expected")

    return content


def build(out=OUT, opener=urllib.request.urlopen):
    files = {file_name: fetch(file_name, opener) for file_name in FILES}
    packed = pack({MODEL: files})

    if unpack(packed) != {MODEL: files}:
        raise ModelError("the packed model does not read back")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(packed)
    return out


def main():
    try:
        out = build()
    except (OSError, ModelError) as error:
        print(f"Could not make the wake word model: {error}")
        return 1

    print(f"The 'Jarvis' wake word model is ready: {out} ({out.stat().st_size} bytes).")
    print("Flash the toy as usual (pio run -e aicamdetect -t upload --upload-port COM8): it goes on with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
