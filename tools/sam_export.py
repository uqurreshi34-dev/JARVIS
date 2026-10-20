"""Make the outline model: MobileSAM's two ONNX files, for actions/outline.py.

    python tools/sam_export.py                 (once; then restart JARVIS)

MobileSAM (github.com/ChaoningZhang/MobileSAM, Apache-2.0) cuts out whatever is in a box: JARVIS uses
it to draw a highlighted thing's exact shape and to save it with no background. This fetches its
weights (40 MB, checked against their SHA-256) and its code at a fixed commit, and writes

    models/mobile_sam/mobile_sam_encoder.onnx    the image encoder: a 1024 x 1024 picture in
    models/mobile_sam/mobile_sam_decoder.onnx    the mask decoder: a box in, the thing's mask out

beside JARVIS (kept out of git), then checks them on a made-up picture. It needs torch (JARVIS's
already, for Whisper), timm and onnx (pip install timm onnx); nothing of it runs once the files are made.
"""

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "models" / "mobile_sam"
REPO = "https://github.com/ChaoningZhang/MobileSAM.git"
COMMIT = "f706ad9"
WEIGHTS = f"https://raw.githubusercontent.com/ChaoningZhang/MobileSAM/{COMMIT}/weights/mobile_sam.pt"
WEIGHTS_SHA256 = "6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f"


def fetch_weights(folder):
    path = folder / "mobile_sam.pt"
    print(f"fetching the weights ({WEIGHTS})")

    with urllib.request.urlopen(WEIGHTS, timeout=120) as answer:
        data = answer.read()

    digest = hashlib.sha256(data).hexdigest()

    if digest != WEIGHTS_SHA256:
        raise SystemExit(f"the weights are not the ones expected (sha256 {digest})")

    path.write_bytes(data)
    return path


def fetch_code(folder):
    code = folder / "MobileSAM"
    print(f"fetching the code ({REPO} at {COMMIT})")
    subprocess.run(["git", "clone", "--quiet", REPO, str(code)], check=True)
    subprocess.run(["git", "-C", str(code), "checkout", "--quiet", COMMIT], check=True)
    return code


def export(code, weights, out):
    import torch

    sys.path.insert(0, str(code))
    from mobile_sam import sam_model_registry
    from mobile_sam.utils.onnx import SamOnnxModel

    sam = sam_model_registry["vit_t"](checkpoint=str(weights))
    sam.eval()
    out.mkdir(parents=True, exist_ok=True)

    class Encoder(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.encoder = model.image_encoder

        def forward(self, image):
            return self.encoder(image)

    with warnings.catch_warnings(), torch.no_grad():
        warnings.simplefilter("ignore")
        print("exporting the image encoder")
        torch.onnx.export(Encoder(sam), (torch.randn(1, 3, 1024, 1024),), str(out / "mobile_sam_encoder.onnx"),
                          input_names=["image"], output_names=["image_embeddings"], opset_version=17,
                          do_constant_folding=True, dynamo=False)

        print("exporting the mask decoder")
        decoder = SamOnnxModel(model=sam, return_single_mask=True)
        size = sam.prompt_encoder.image_embedding_size
        dummy = {
            "image_embeddings": torch.randn(1, sam.prompt_encoder.embed_dim, *size),
            "point_coords": torch.randint(0, 1024, (1, 3, 2), dtype=torch.float),
            "point_labels": torch.tensor([[2, 3, -1]], dtype=torch.float),
            "mask_input": torch.zeros(1, 1, 4 * size[0], 4 * size[1]),
            "has_mask_input": torch.tensor([0], dtype=torch.float),
            "orig_im_size": torch.tensor([480, 640], dtype=torch.float),
        }
        torch.onnx.export(decoder, tuple(dummy.values()), str(out / "mobile_sam_decoder.onnx"),
                          input_names=list(dummy), output_names=["masks", "iou_predictions", "low_res_masks"],
                          dynamic_axes={"point_coords": {1: "num_points"}, "point_labels": {1: "num_points"}},
                          opset_version=17, do_constant_folding=True, dynamo=False)

    (out / "NOTICE.txt").write_text(
        "MobileSAM (github.com/ChaoningZhang/MobileSAM), Apache License 2.0, exported to ONNX by\n"
        f"tools/sam_export.py from commit {COMMIT}. Segment Anything (Meta), Apache License 2.0.\n",
        encoding="utf-8")


def check(out):
    """The two files, run as JARVIS runs them, cut a bright square out of a dark picture."""
    import numpy as np

    sys.path.insert(0, str(ROOT))
    from actions import outline

    outline.MODEL_DIR = out
    outline.forget()
    picture = np.full((480, 640, 3), 30, dtype=np.uint8)
    picture[150:330, 250:400] = (230, 200, 60)
    found = outline.mask(outline.embed(picture), (0.35, 0.28, 0.65, 0.72))
    inside = found[150:330, 250:400].mean() if found is not None else 0
    outside = found.mean() - inside * (180 * 150) / (480 * 640) if found is not None else 1

    if inside < 0.9 or outside > 0.02:
        raise SystemExit(f"the made model does not cut out a plain square ({inside:.2f} in, {outside:.3f} out)")

    print(f"checked: a plain square cut out ({inside:.0%} of it, {outside:.1%} of the rest)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="jarvis-sam-"))

    try:
        export(fetch_code(work), fetch_weights(work), args.out)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    check(args.out)

    for path in sorted(args.out.iterdir()):
        print(f"  {path.name:30s} {path.stat().st_size:>12,} B")

    print("\nRestart JARVIS: highlights now hug what they box, and 'save the highlight' cuts them out.")


if __name__ == "__main__":
    main()
