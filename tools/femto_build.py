"""Make the house cam's detection sources: YOLO26n compiled to C by FEMTO-ML, into arduino/jarvis_camera/detect/.

    pip install onnx numpy                 (once, into JARVIS's .venv)
    python tools/femto_build.py            (then: pio run -e s3detect -t upload)

FEMTO-ML is your cousin's ONNX-to-C compiler (github.com/Reuna-AI/FEMTO-ML), cloned beside JARVIS:
C:\\Users\\<you>\\Projects\\FEMTO-ML. This runs its six stages on the model it ships
(examples/esp32_yolo26n/yolo26n_q.onnx) for the ESP32-S3 with SIMD, copies the esp-nn kernels it
needs, and writes labels.h -- the 80 names the model knows -- from the model's own metadata.

What FEMTO-ML itself does about speed and scores (its changes of October 2026, in FEMTO-ML, not here):
each conv runs in blocks of output channels whose filters fit in a buffer of internal SRAM, which the
sketch gives it once wifi is up (takeFastBuffer), on both cores (set_cores); a class score above 0.5
is no longer clamped to 0.5; SiLU and the final Sigmoid are table lookups; values are quantized where
they are made rather than moved as floats first; the sketch writes the model's int8 input itself
(run_model_int8_input); and the sources carry a step timer (env s3profile).

Two changes from FEMTO-ML's defaults, both so detection runs beside the camera's live view without
taking from it:

- the arena's fast internal memory budget is 0: all of the model's working memory goes in the
  board's 8 MB PSRAM, and the internal RAM stays for the wifi and the encrypted connections (and
  the filters' buffer, which the sketch takes only while enough stays free for them);
- its orchestrator (build_model.py) is not used, as it stops at once on a doubled --debug flag;
  its stages are called directly, in the same order.

The sources are made, not kept: they are 11 MB, rebuilt from the model in a minute, and the
model's own licence (Ultralytics, AGPL-3.0) is not JARVIS's. detect/ is in .gitignore.

The face finder, beside it, into arduino/jarvis_camera/faces/ (also not kept): YuNet (MIT, the
model tools/face_model.py fetches), its input set to the board's half-size picture -- 320 x 240,
padded below to 256 -- quantized to int8 once, with up to 100 of your own sightings' frames to
tune it (models/faces/face_detection_yunet_320x256_int8.onnx; delete it to tune again), then
compiled by FEMTO-ML with every name prefixed face_ so both models build into one firmware.
That needs a FEMTO-ML with --prefix (nhwc_compiler/prefix.py, October 2026), YuNet, onnxruntime,
and at least 30 sightings' frames; without any of them the face finder is left out, said why,
and the board is built as before. --no-faces leaves it out on purpose.
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
# The step timer the s3profile build switches on: FEMTO-ML's own from October 2026. A
# FEMTO-ML from before then has none, and the sources are made without it.
OPTIONAL = ("femto_profile.c", "femto_profile.h")

# The model's input, as the sketch letterboxes to it (jarvis_camera.ino, MODEL_SIZE).
INPUT = [1, 3, 256, 256]

FACES_OUT = ROOT / "arduino" / "jarvis_camera" / "faces"
FACE_FLOAT = ROOT / "models" / "faces" / "face_detection_yunet_2023mar.onnx"
FACE_INT8 = ROOT / "models" / "faces" / "face_detection_yunet_320x256_int8.onnx"
# The board's VGA picture, decoded at half size for YOLO (320 x 240), padded below to rows of 32.
FACE_W, FACE_H, FACE_ROWS = 320, 256, 240
FACE_PREFIX = "face_"
CALIBRATE_AT_MOST = 100
CALIBRATE_AT_LEAST = 30
SIGHTINGS = Path.home() / "JARVIS" / "sightings"


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

    for name in OPTIONAL:
        if (kernels / name).is_file():
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


# ---- the face finder ---------------------------------------------------------------------------------------

def calibration_frames(folder=SIGHTINGS, at_most=CALIBRATE_AT_MOST):
    """Up to [at_most] of the sightings' frames, spread evenly over all of them (the same ones each time)."""
    frames = sorted(Path(folder).glob("*/frame-*.jpg")) if Path(folder).is_dir() else []

    if len(frames) <= at_most:
        return frames

    step = len(frames) / at_most
    return [frames[int(index * step)] for index in range(at_most)]


def face_input(path):
    """A frame as the board hands it to the face finder: halved, blue-green-red 0 to 255, padded below."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as picture:
        rgb = np.asarray(picture.convert("RGB").resize((FACE_W, FACE_ROWS), Image.BILINEAR), np.float32)

    padded = np.zeros((FACE_H, FACE_W, 3), np.float32)
    padded[:FACE_ROWS] = rgb[..., ::-1]
    return padded.transpose(2, 0, 1)[None]


def quantize_faces(float_model, out, frames):
    """YuNet at the board's size, int8: its convs, ReLUs, pools and layout steps, per channel."""
    import onnx
    from onnx import shape_inference
    from onnxruntime.quantization import CalibrationDataReader, QuantFormat, QuantType, quantize_static

    model = onnx.load(str(float_model))
    dims = model.graph.input[0].type.tensor_type.shape.dim
    dims[2].dim_value, dims[3].dim_value = FACE_H, FACE_W

    for output in model.graph.output:
        for dim in output.type.tensor_type.shape.dim:
            dim.ClearField("dim_value")
            dim.ClearField("dim_param")

    del model.graph.value_info[:]
    weights = {item.name for item in model.graph.initializer}
    inputs = [item for item in model.graph.input if item.name not in weights]
    del model.graph.input[:]
    model.graph.input.extend(inputs)
    model = shape_inference.infer_shapes(model)
    sized = out.with_name(out.stem + ".float.onnx")
    onnx.save(model, str(sized))
    name = model.graph.input[0].name

    class Frames(CalibrationDataReader):
        def __init__(self):
            self.left = iter({name: face_input(path)} for path in frames)

        def get_next(self):
            return next(self.left, None)

    quantize_static(str(sized), str(out), Frames(), quant_format=QuantFormat.QOperator,
                    op_types_to_quantize=["Conv", "Relu", "MaxPool", "Resize", "Transpose", "Reshape"],
                    activation_type=QuantType.QInt8, weight_type=QuantType.QInt8, per_channel=True)
    sized.unlink()


def build_faces(femto, out=FACES_OUT, float_model=FACE_FLOAT, int8_model=FACE_INT8, sightings=SIGHTINGS):
    """The face finder's sources into [out], or None (and why, said) when it cannot be made."""
    def without(why):
        print(f"\nno face finder: {why}. The board is built without it, as before.")

        if out.exists():
            shutil.rmtree(out)

        return None

    compiler = femto / "nhwc_compiler"

    if not (compiler / "prefix.py").is_file():
        return without("this FEMTO-ML has no --prefix (nhwc_compiler/prefix.py); update it")

    if not int8_model.is_file():
        if not float_model.is_file():
            return without(f"YuNet is not at {float_model}: python tools/face_model.py")

        frames = calibration_frames(sightings)

        if len(frames) < CALIBRATE_AT_LEAST:
            return without(f"{len(frames)} sightings' frames to tune it with, and it needs {CALIBRATE_AT_LEAST}")

        try:
            print(f"face finder: quantizing YuNet to int8 with {len(frames)} of your sightings' frames")
            quantize_faces(float_model, int8_model, frames)
        except ImportError as error:
            return without(f"{error.name} is not installed")

    if out.exists():
        shutil.rmtree(out)

    out.mkdir(parents=True)
    sys.path.insert(0, str(compiler))

    import codegen
    import fuse
    import gen_activations
    import gen_weights
    import graph_json
    import prefix

    gen_activations.SRAM_BUDGET_BYTES = 0
    work = out / "_ir"
    work.mkdir()

    def at(name):
        return str(work / name)

    print("face finder: compiling")
    graph_json.build(str(int8_model), at("graph.json"))
    graph_json.nhwc_pass(at("graph.json"), at("graph_nhwc.json"))
    fuse.main(at("graph_nhwc.json"), at("graph_fused.json"))
    gen_weights.main(at("graph_fused.json"), str(out), 5_000_000, "esp32s3")
    gen_activations.main(at("graph_fused.json"), str(out / "activations.h"), "esp32s3")
    codegen.main(at("graph_fused.json"), str(out / "model"), "esp32s3", True, False)
    shutil.rmtree(work)
    prefix.apply(str(out), FACE_PREFIX)
    (out / "YuNet-NOTICE.txt").write_text(
        "YuNet face detection (github.com/opencv/opencv_zoo, models/face_detection_yunet), MIT License,\n"
        "Copyright (c) 2020 Shiqi Yu. Quantized and compiled by tools/femto_build.py.\n", encoding="ascii")
    made = sorted(out.iterdir())
    print(f"wrote {len(made)} files to {out}")
    return made


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--femto", type=Path, default=DEFAULT_FEMTO, help=f"FEMTO-ML's folder ({DEFAULT_FEMTO})")
    parser.add_argument("--model", type=Path, help=f"the quantized ONNX model (FEMTO-ML's {MODEL})")
    parser.add_argument("--out", type=Path, default=OUT, help=f"where the sources go ({OUT})")
    parser.add_argument("--no-faces", action="store_true", help="build without the face finder")
    args = parser.parse_args(argv)
    build(args.femto, args.model or args.femto / MODEL, args.out)

    if args.no_faces:
        if FACES_OUT.exists():
            shutil.rmtree(FACES_OUT)
    else:
        build_faces(args.femto)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
