"""Checks for the toy's own "Jarvis" wake word: the model tool, the flash map that holds it, and the
firmware that only lets what was said to Jarvis leave the toy. No network: the downloads are faked.

    python tools/test_toy_wake_model.py
"""

import hashlib
import io
import re
import struct
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


# ---- packing, as ESP-SR's pack_model.py -------------------------------------------------------------

files = {"_MODEL_INFO_": b"wakenet9l_tts1h8_Jarvis_3_0.627_0.632", "wn9_index": b"\x01" * 12, "wn9_data": b"\x02" * 40}
packed = wake.pack({"wn9_jarvis_tts": files})
check(struct.unpack_from("<I", packed)[0] == 1 and packed[4:4 + 14] == b"wn9_jarvis_tts",
      "one model, its name first, as ESP-SR reads it")
check(wake.unpack(packed) == {"wn9_jarvis_tts": files}, "and it reads back file by file")
header = 4 + (32 + 4) + 3 * (32 + 4 + 4)
check(struct.unpack_from("<II", packed, 4 + 36 + 32) == (header, len(files["_MODEL_INFO_"])),
      "each file's start counted from the end of the header")
check(list(wake.FILES) == ["_MODEL_INFO_", "wn9_index", "wn9_data"] and wake.ESP_SR_VERSION == "v1.9.5"
      and wake.MODEL == "wn9_jarvis_tts",
      "the Jarvis model, from the ESP-SR release built into the toy's Arduino core, files in its order")

# ---- fetching: only the expected files ---------------------------------------------------------------


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


real = {name: name.encode() * 3 for name in wake.FILES}
saved_hashes = dict(wake.FILES)
wake.FILES.update({name: hashlib.sha256(content).hexdigest() for name, content in real.items()})
asked = []


def opener(url, timeout=None):
    asked.append(url)
    return Reply(real[url.rsplit("/", 1)[1]])


with tempfile.TemporaryDirectory() as folder:
    out = wake.build(out=Path(folder) / "wake" / "srmodels.bin", opener=opener)
    check(out.is_file() and wake.unpack(out.read_bytes()) == {"wn9_jarvis_tts": real},
          "built into wake/srmodels.bin from the three files")
    check(all(url.startswith("https://raw.githubusercontent.com/espressif/esp-sr/v1.9.5/model/wakenet_model/"
                             "wn9_jarvis_tts/") for url in asked) and len(asked) == 3,
          "fetched from ESP-SR's own repository at that release")

    try:
        wake.build(out=Path(folder) / "bad.bin", opener=lambda url, timeout=None: Reply(b"tampered"))
        refused = False
    except wake.ModelError:
        refused = True

    check(refused and not (Path(folder) / "bad.bin").exists(), "a file that is not the one expected is refused")

wake.FILES.clear()
wake.FILES.update(saved_hashes)
check(all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in wake.FILES.values()), "every file pinned by SHA-256")

# ---- the flash map and the upload ---------------------------------------------------------------------

sketch_dir = ROOT / "arduino" / "jarvis_camera"
rows = {}

for line in (sketch_dir / "toy_partitions.csv").read_text(encoding="ascii").splitlines():
    if line.strip() and not line.startswith("#"):
        name, kind, sub, offset, size = [part.strip() for part in line.split(",")[:5]]
        rows[name] = (kind, sub, int(offset, 16), int(size, 16))

check(rows["app0"][2:] == (0x10000, 0x640000) and rows["app1"][2:] == (0x650000, 0x640000)
      and rows["nvs"][2] == 0x9000,
      "the apps and the toy's saved settings (nvs) where they always were: nothing kept is lost")
check(rows["model"] == ("data", "spiffs", 0xEF0000, 0x100000), "a 1 MB model partition, as ESP-SR looks for")
order = sorted(rows.values(), key=lambda row: row[2])
check(all(a[2] + a[3] <= b[2] for a, b in zip(order, order[1:])) and order[-1][2] + order[-1][3] <= 16 * 1024 * 1024,
      "no partition overlaps the next, and all fit the 16 MB flash")
image = (sketch_dir / "toy_wake_image.py").read_text(encoding="ascii")
check('MODEL_OFFSET = "0xef0000"' in image and 'join(env.subst("$PROJECT_DIR"), "wake", "srmodels.bin")' in image
      and "FLASH_EXTRA_IMAGES" in image, "every upload writes the model to that partition, when it has been made")
ini = (sketch_dir / "platformio.ini").read_text(encoding="ascii")
aicam = ini[ini.index("[env:aicam]"):ini.index("[env:aicamdetect]")]
check("board_build.partitions = toy_partitions.csv" in aicam and "extra_scripts = pre:toy_wake_image.py" in aicam,
      "the toy's builds (aicam and aicamdetect) use them")
s3 = ini[ini.index("[env:s3cam]"):ini.index("[env:s3detect]")]
check("default_16MB.csv" in s3 and "toy_wake_image" not in s3, "the house cam's flash is left as it was")
ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("arduino/jarvis_camera/wake/" in ignored, "the model itself kept out of git")

# ---- the firmware -------------------------------------------------------------------------------------

sketch = (sketch_dir / "jarvis_camera.ino").read_text(encoding="ascii")
check('WAKE_PARTITION = "model"' in sketch and "esp_srmodel_init(WAKE_PARTITION)" in sketch
      and "esp_srmodel_filter(models, ESP_WN_PREFIX, NULL)" in sketch and "->create(name, DET_MODE_90)" in sketch,
      "the board finds the model in that partition and starts listening for Jarvis")
task = sketch[sketch.index("void talkTask(void *)"):sketch.index("static void chime()")]
check(task.count("listenForWake(frame);") == 2, "every frame heard, waiting and recording, goes past the wake word")
check("if (wakeModel && !button && !named && !following) {" in task
      and task.index("!named && !following") < task.index("askAndPlay(got, button)"),
      "a recording without Jarvis, BOOT or a follow-up is kept on the toy, never sent")
check("followUntil = answered > 0 ? asked + TOY_FOLLOW_MS : 0;" in task and "TOY_FOLLOW_MS = 12000UL" in sketch,
      "follow-ups for twelve seconds after an answer, as the cloud's and the PC's window")
check("(long)(wakeHeardAt + WAKE_BEFORE_MS - began) >= 0" in task,
      "Jarvis said just before the recording began still counts")
check("every recording goes to be checked for Jarvis" in sketch and "if (!wakeModel) {" in sketch,
      "with no model, every recording goes to be checked, as before, and the toy says so")
check(sketch.index("startWake();") < sketch.index("xTaskCreatePinnedToCore(talkTask"),
      "listening for Jarvis starts before the talk does")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
