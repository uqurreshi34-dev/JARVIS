"""Faces in a picture: where each is, how sure, and whether it looks at the camera. Never who.

A sighting's best picture (actions/sightings.py) is the one showing someone's face best: found, turned
to the camera, large and sharp. YuNet (MIT licence, OpenCV's model zoo; 227 KB), run by onnxruntime on
the PC, finds faces and five points on each (eyes, nose, mouth corners). Nothing here recognises anyone
or keeps a face: it only says where faces are in a picture already kept.

The model file is models/faces/face_detection_yunet_2023mar.onnx beside JARVIS, kept out of git; made by
tools/face_model.py (fetched and checked against its SHA-256). Without it, there are no faces, and a
sighting's best picture is chosen from the board's boxes as before.
"""

import os
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(os.getenv("JARVIS_FACE_FOLDER") or (ROOT / "models" / "faces"))
MODEL = "face_detection_yunet_2023mar.onnx"

# The model's fixed input: the picture scaled to fit, padded, as blue-green-red values 0 to 255.
SIZE = 640
STRIDES = (8, 16, 32)

# A face counts from this sure (YuNet's score); OpenCV's own default is 0.9, for photographs in good
# light. On the house cam's dim pictures (21 October 2026) faces facing the camera scored 0.62 to 0.89.
SURE_AT_LEAST = 0.6

# Two finds overlapping more than this are one face.
SAME_FACE = 0.3

# Sharpness is measured on the middle of a face (its five points, and this much more of their spread
# around them) scaled to this many pixels across.
SHARP_MORE = 0.25
SHARP_SIZE = 32

_lock = threading.Lock()
_session = None     # the model once loaded; False if it could not be


def present():
    """Whether the model's file is there (without loading it)."""
    return (MODEL_DIR / MODEL).is_file()


def _load():
    global _session

    with _lock:
        if _session is None:
            try:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.log_severity_level = 3
                _session = ort.InferenceSession(str(MODEL_DIR / MODEL), options,
                                                providers=["CPUExecutionProvider"])
            except Exception as error:   # noqa: BLE001 - no faces, and the best picture is chosen as before
                print(f"[JARVIS] the face model could not be loaded: {error}")
                _session = False

        return _session


def _overlap(a, b):
    across = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    down = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    either = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - across * down
    return across * down / either if either > 0 else 0.0


def detect(rgb):
    """[{"score", "box": (x1, y1, x2, y2), "points": [(x, y) right eye, left eye, nose, mouth right,
    mouth left]}] in picture [rgb]'s pixels, surest first; [] without the model."""
    session = _load() if present() else None

    if not session or rgb is None:
        return []

    from PIL import Image

    height, width = rgb.shape[:2]
    scale = min(SIZE / width, SIZE / height)
    shown = Image.fromarray(rgb)

    if scale != 1:
        shown = shown.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.BILINEAR)

    padded = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
    values = np.asarray(shown, dtype=np.float32)[..., ::-1]
    padded[:values.shape[0], :values.shape[1]] = values
    outputs = dict(zip([output.name for output in session.get_outputs()],
                       session.run(None, {session.get_inputs()[0].name: padded.transpose(2, 0, 1)[None]})))
    found = []

    # YuNet's outputs, as OpenCV's FaceDetectorYN reads them: at each stride, a score (the square root of
    # class and objectness), a box from the cell's corner, and five points.
    for stride in STRIDES:
        columns = SIZE // stride
        score = np.sqrt(np.clip(outputs[f"cls_{stride}"][0, :, 0], 0, 1)
                        * np.clip(outputs[f"obj_{stride}"][0, :, 0], 0, 1))

        for index in np.nonzero(score >= SURE_AT_LEAST)[0]:
            row, column = divmod(int(index), columns)
            box = outputs[f"bbox_{stride}"][0, index]
            marks = outputs[f"kps_{stride}"][0, index]
            middle_x, middle_y = (column + box[0]) * stride, (row + box[1]) * stride
            half_w, half_h = np.exp(box[2]) * stride / 2, np.exp(box[3]) * stride / 2
            found.append({
                "score": float(score[index]),
                "box": tuple(float(v) / scale for v in (middle_x - half_w, middle_y - half_h,
                                                        middle_x + half_w, middle_y + half_h)),
                "points": [(float((marks[2 * n] + column) * stride / scale),
                            float((marks[2 * n + 1] + row) * stride / scale)) for n in range(5)],
            })

    found.sort(key=lambda face: -face["score"])
    kept = []

    for face in found:
        if all(_overlap(face["box"], other["box"]) < SAME_FACE for other in kept):
            kept.append(face)

    return kept


def facing(face):
    """How squarely [face] looks at the camera, 0 to 1: its nose midway between its eyes is 1, a nose at
    one eye or beyond (turned side on) 0."""
    (right_x, right_y), (left_x, left_y), (nose_x, _) = face["points"][:3]
    apart = float(np.hypot(left_x - right_x, left_y - right_y))

    if apart < 1.0:
        return 0.0

    return float(max(0.0, 1.0 - abs(nose_x - (right_x + left_x) / 2) / (apart / 2)))


def sharpness(rgb, face):
    """How sharp [face] is in picture [rgb], whatever its brightness or size: the middle of the face (its
    eyes, nose and mouth, and a quarter more around them) scaled to SHARP_SIZE across, then its mean
    change from pixel to pixel over its mean brightness. Measured on the house cam (22 October 2026):
    faces held still 0.09 to 0.10, moving 0.05 to 0.06. The middle only, so the door or wall behind a
    face's box is not counted; one size, so a near face is judged as sharp for its size as a far one."""
    from PIL import Image

    height, width = rgb.shape[:2]
    xs = [x for x, _ in face["points"]]
    ys = [y for _, y in face["points"]]
    more_x, more_y = (max(xs) - min(xs)) * SHARP_MORE, (max(ys) - min(ys)) * SHARP_MORE
    x1, y1 = int(round(min(xs) - more_x)), int(round(min(ys) - more_y))
    x2, y2 = int(round(max(xs) + more_x)), int(round(max(ys) + more_y))
    part = rgb[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]

    if part.shape[0] < 3 or part.shape[1] < 3:
        return 0.0

    scaled = Image.fromarray(np.ascontiguousarray(part)).resize((SHARP_SIZE, SHARP_SIZE), Image.BILINEAR)
    grey = np.asarray(scaled, np.float32).mean(axis=2)
    change = np.abs(np.diff(grey, axis=1)).mean() + np.abs(np.diff(grey, axis=0)).mean()
    return float(change / (grey.mean() + 1.0))


def worth(rgb, face):
    """How good a picture of a face [face] is in [rgb]: sure, turned to the camera, sharp (counted twice:
    a blurred face is no use however large) and large (counted as the square root of its width: a face
    a little nearer is not worth a little blur)."""
    width = face["box"][2] - face["box"][0]
    return face["score"] * facing(face) * np.sqrt(max(0.0, width)) * sharpness(rgb, face) ** 2


def forget():
    """Load the model afresh next time. For tests."""
    global _session

    with _lock:
        _session = None
