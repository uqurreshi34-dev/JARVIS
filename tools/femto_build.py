"""Make the house cam's detection sources: YOLO26n compiled to C by FEMTO-ML, into arduino/jarvis_camera/detect/.

    pip install onnx numpy                 (once, into JARVIS's .venv)
    python tools/femto_build.py            (then: pio run -e s3detect -t upload)

FEMTO-ML is your cousin's ONNX-to-C compiler (github.com/Reuna-AI/FEMTO-ML), cloned beside JARVIS:
C:\\Users\\<you>\\Projects\\FEMTO-ML. This runs its six stages on the model it ships
(examples/esp32_yolo26n/yolo26n_q.onnx) for the ESP32-S3 with SIMD, copies the esp-nn kernels it
needs, and writes labels.h -- the 80 names the model knows -- from the model's own metadata.

Two changes from FEMTO-ML's defaults, both so detection runs beside the camera's live view without
taking from it:

- the arena's fast internal memory budget is 0: all of the model's working memory goes in the
  board's 8 MB PSRAM, and the internal RAM stays for the wifi and the encrypted connections;
- its orchestrator (build_model.py) is not used, as it stops at once on a doubled --debug flag;
  its stages are called directly, in the same order.

The sources are made, not kept: they are 11 MB, rebuilt from the model in a minute, and the
model's own licence (Ultralytics, AGPL-3.0) is not JARVIS's. detect/ is in .gitignore.
"""

import argparse
import ast
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "arduino" / "jarvis_camera" / "detect"
DEFAULT_FEMTO = ROOT.parent / "FEMTO-ML"
MODEL = Path("examples") / "esp32_yolo26n" / "yolo26n_q.onnx"
KERNELS = ("esp_nn_conv_s8_simd.c", "esp_nn_conv_s8_simd.h",
           "esp_nn_depthwise_conv_s8_simd.c", "esp_nn_depthwise_conv_s8_simd.h")

# The model's input, as the sketch letterboxes to it (jarvis_camera.ino, MODEL_SIZE).
INPUT = [1, 3, 256, 256]


def labels_header(names):
    """labels.h: the model's class names, by number."""
    lines = ["// Made by tools/femto_build.py from the model's metadata. Do not edit.", "#pragma once", "",
             f"#define LABEL_COUNT {len(names)}", "", "static const char *const LABELS[LABEL_COUNT] = {"]
    lines += [f'  "{name}",' for name in names]
    lines += ["};", ""]
    return "\n".join(lines)


def model_names(model):
    """The class names in the ONNX model's metadata, in order; checked to be the model the sketch expects."""
    import onnx

    loaded = onnx.load(str(model), load_external_data=False)
    shape = [dim.dim_value for dim in loaded.graph.input[0].type.tensor_type.shape.dim]

    if shape != INPUT:
        raise SystemExit(f"{model.name} takes {shape}; the sketch letterboxes to {INPUT}")

    meta = {prop.key: prop.value for prop in loaded.metadata_props}

    if "names" not in meta:
        raise SystemExit(f"{model.name} carries no class names in its metadata")

    names = ast.literal_eval(meta["names"])
    return [str(names[index]).replace('"', "'") for index in sorted(names)]


def build(femto, model, out):
    compiler = femto / "nhwc_compiler"

    if not compiler.is_dir():
        raise SystemExit(f"FEMTO-ML not found at {femto}: clone it there, or pass --femto")

    if not model.is_file():
        raise SystemExit(f"model not found: {model}")

    names = model_names(model)

    if out.exists():
        shutil.rmtree(out)

    out.mkdir(parents=True)
    sys.path.insert(0, str(compiler))

    import codegen
    import fuse
    import gen_activations
    import gen_weights
    import graph_json

    gen_activations.SRAM_BUDGET_BYTES = 0
    work = out / "_ir"
    work.mkdir()

    def at(name):
        return str(work / name)

    print("1/6 ONNX -> JSON")
    graph_json.build(str(model), at("graph.json"))
    print("2/6 NHWC")
    graph_json.nhwc_pass(at("graph.json"), at("graph_nhwc.json"))
    print("3/6 fusion")
    fuse.main(at("graph_nhwc.json"), at("graph_fused.json"))
    print("4/6 weights")
    gen_weights.main(at("graph_fused.json"), str(out), 5_000_000, "esp32s3")
    print("5/6 arena")
    gen_activations.main(at("graph_fused.json"), str(out / "activations.h"), "esp32s3")
    print("6/6 code")
    codegen.main(at("graph_fused.json"), str(out / "model"), "esp32s3", True, False)
    shutil.rmtree(work)

    kernels = model.parent

    for name in KERNELS:
        shutil.copy2(kernels / name, out / name)

    (out / "labels.h").write_text(labels_header(names), encoding="ascii")

    for name in ("LICENSE", "NOTICE"):
        if (femto / name).is_file():
            shutil.copy2(femto / name, out / f"FEMTO-ML-{name}")

    made = sorted(out.iterdir())
    print(f"\nwrote {len(made)} files to {out}:")

    for path in made:
        print(f"  {path.name:40s} {path.stat().st_size:>12,} B")

    print("\nnext, from arduino/jarvis_camera:  pio run -e s3detect -t upload")
    return made


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--femto", type=Path, default=DEFAULT_FEMTO, help=f"FEMTO-ML's folder ({DEFAULT_FEMTO})")
    parser.add_argument("--model", type=Path, help=f"the quantized ONNX model (FEMTO-ML's {MODEL})")
    parser.add_argument("--out", type=Path, default=OUT, help=f"where the sources go ({OUT})")
    args = parser.parse_args(argv)
    build(args.femto, args.model or args.femto / MODEL, args.out)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
