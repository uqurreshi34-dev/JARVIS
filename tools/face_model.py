"""Fetch the face model: YuNet, for actions/faces.py (a sighting's best picture is the best of a face).

    python tools/face_model.py                 (once; then restart JARVIS)

YuNet (MIT licence; github.com/opencv/opencv_zoo, models/face_detection_yunet, 227 KB) finds faces and
five points on each. This fetches it at a fixed commit's file, checks it against its SHA-256, and writes

    models/faces/face_detection_yunet_2023mar.onnx

beside JARVIS (kept out of git). It finds faces only; nothing recognises anyone.
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "models" / "faces"
NAME = "face_detection_yunet_2023mar.onnx"
URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
       "face_detection_yunet_2023mar.onnx")
SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
NOTICE = ("YuNet face detection (github.com/opencv/opencv_zoo, models/face_detection_yunet), MIT License,\n"
          "Copyright (c) 2020 Shiqi Yu. Fetched by tools/face_model.py.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    target = args.out / NAME

    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == SHA256:
        print(f"already there: {target}")
        return 0

    print(f"fetching {URL}")

    with urllib.request.urlopen(URL, timeout=120) as answer:
        data = answer.read()

    digest = hashlib.sha256(data).hexdigest()

    if digest != SHA256:
        print(f"not the file expected (sha256 {digest}); nothing written")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    (args.out / "NOTICE.txt").write_text(NOTICE, encoding="utf-8")
    print(f"written: {target} ({len(data):,} B). Restart JARVIS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
