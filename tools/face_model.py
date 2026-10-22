"""Fetch the face models: YuNet, for actions/faces.py, and SFace, for actions/known_faces.py.

    python tools/face_model.py                 (once; then restart JARVIS)

YuNet (MIT licence; github.com/opencv/opencv_zoo, models/face_detection_yunet, 227 KB) finds faces and
five points on each: a sighting's best picture is the best of a face. SFace (Apache 2.0 licence;
models/face_recognition_sface, 38.7 MB) gives a face's fingerprint, so that once you ask "learn my face"
a sighting can say it was you. Each is fetched at its file in the zoo, checked against its SHA-256, and
written to

    models/faces/face_detection_yunet_2023mar.onnx
    models/faces/face_recognition_sface_2021dec.onnx

beside JARVIS (kept out of git), with NOTICE.txt saying where they came from and under what licence.
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "models" / "faces"
ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
MODELS = [
    {"name": "face_detection_yunet_2023mar.onnx",
     "url": ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
     "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
     "notice": "YuNet face detection (github.com/opencv/opencv_zoo, models/face_detection_yunet), MIT License,\n"
               "Copyright (c) 2020 Shiqi Yu."},
    {"name": "face_recognition_sface_2021dec.onnx",
     "url": ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
     "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
     "notice": "SFace face recognition (github.com/opencv/opencv_zoo, models/face_recognition_sface),\n"
               "Apache License 2.0 (all files in that directory, as its README says)."},
]
# Kept for anything that still asks for the one model by these names.
NAME, URL, SHA256 = MODELS[0]["name"], MODELS[0]["url"], MODELS[0]["sha256"]


def fetch(model, out):
    """Write [model] into [out] unless already there. Returns True if it is there after."""
    target = out / model["name"]

    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == model["sha256"]:
        print(f"already there: {target}")
        return True

    print(f"fetching {model['url']}")

    with urllib.request.urlopen(model["url"], timeout=300) as answer:
        data = answer.read()

    digest = hashlib.sha256(data).hexdigest()

    if digest != model["sha256"]:
        print(f"not the file expected (sha256 {digest}); {model['name']} not written")
        return False

    out.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"written: {target} ({len(data):,} B)")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    ok = all([fetch(model, args.out) for model in MODELS])

    if any((args.out / model["name"]).is_file() for model in MODELS):
        (args.out / "NOTICE.txt").write_text(
            "".join(f"{model['notice']}\nFetched by tools/face_model.py.\n\n" for model in MODELS), encoding="utf-8")

    if ok:
        print("Restart JARVIS.")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
