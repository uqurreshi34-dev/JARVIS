"""The exact shape of a thing a camera has boxed: its outline, and a cut-out of it with no background.

The house cam's board says where each thing is as a box (actions/detections.py). Asked to highlight
it (actions/highlight.py), JARVIS cuts it out of the picture on the PC with MobileSAM -- "segment
whatever is in this box", for any thing, with no list of things in it -- so the line drawn hugs the
bottle rather than the box around it, and "save the highlight" keeps the bottle alone.

MobileSAM (Apache-2.0, github.com/ChaoningZhang/MobileSAM) as two ONNX files in models/mobile_sam/
beside JARVIS, run by onnxruntime (already JARVIS's, for Kokoro): the image encoder, once a picture
(about a third of a second on a laptop's CPU), and the mask decoder, once a box (a few milliseconds).
They are made by tools/sam_export.py and kept out of git. Without them, highlights stay boxes.
"""

import os
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(os.getenv("JARVIS_SAM_FOLDER") or (ROOT / "models" / "mobile_sam"))
ENCODER = "mobile_sam_encoder.onnx"
DECODER = "mobile_sam_decoder.onnx"

# The encoder's input: the picture's longer side scaled to this, padded square, normalised as SAM
# was trained (its ResizeLongestSide and pixel mean and deviation).
SIZE = 1024
_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
_STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)

# An outline is traced on the thing's mask scaled to at most this many pixels across, and kept to at
# most this many points: smooth to the eye, small to send to the phone each look.
TRACE_SIZE = 160
MAX_POINTS = 160

_lock = threading.Lock()
_sessions = None      # (encoder, decoder) once loaded; False if they could not be


def present():
    """Whether the model's two files are there (without loading them)."""
    return (MODEL_DIR / ENCODER).is_file() and (MODEL_DIR / DECODER).is_file()


def why_not():
    """None when outlines can be made, else why not, in words."""
    if not present():
        return "the outline model isn't set up (python tools/sam_export.py)"

    if _load() is False:
        return "the outline model could not be loaded"

    return None


def _load():
    global _sessions

    with _lock:
        if _sessions is None:
            try:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.log_severity_level = 3
                _sessions = (ort.InferenceSession(str(MODEL_DIR / ENCODER), options,
                                                  providers=["CPUExecutionProvider"]),
                             ort.InferenceSession(str(MODEL_DIR / DECODER), options,
                                                  providers=["CPUExecutionProvider"]))
            except Exception as error:   # noqa: BLE001 - a broken model leaves the boxes as they were
                print(f"[JARVIS] the outline model could not be loaded: {error}")
                _sessions = False

        return _sessions


def picture(jpeg):
    """A JPEG as an RGB array (height, width, 3), or None."""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(jpeg)) as image:
            return np.asarray(image.convert("RGB"))
    except Exception:
        return None


def embed(rgb):
    """The encoder's view of picture [rgb]: what masks() needs, or None."""
    sessions = _load()

    if not sessions:
        return None

    from PIL import Image

    height, width = rgb.shape[:2]
    scale = SIZE / max(height, width)
    new_h, new_w = int(height * scale + 0.5), int(width * scale + 0.5)
    resized = np.asarray(Image.fromarray(rgb).resize((new_w, new_h), Image.BILINEAR), dtype=np.float32)
    padded = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
    padded[:new_h, :new_w] = (resized - _MEAN) / _STD
    tensor = padded.transpose(2, 0, 1)[None]
    encoder = sessions[0]
    embedding = encoder.run(None, {encoder.get_inputs()[0].name: tensor})[0]
    return {"embedding": embedding, "scale": scale, "size": (height, width)}


def mask(view, box):
    """The thing in [box] (0 to 1 across and down) of the picture [view] was made from: a boolean array
    the picture's size, or None."""
    sessions = _load()

    if not sessions or view is None:
        return None

    height, width = view["size"]
    x1, y1, x2, y2 = box
    corners = np.array([[[x1 * width, y1 * height], [x2 * width, y2 * height], [0.0, 0.0]]],
                       dtype=np.float32) * np.array([view["scale"], view["scale"]], dtype=np.float32)
    corners[0, 2] = 0.0
    feeds = {"image_embeddings": view["embedding"], "point_coords": corners,
             "point_labels": np.array([[2, 3, -1]], dtype=np.float32),
             "mask_input": np.zeros((1, 1, 256, 256), dtype=np.float32),
             "has_mask_input": np.zeros(1, dtype=np.float32),
             "orig_im_size": np.array([height, width], dtype=np.float32)}
    decoder = sessions[1]
    masks = decoder.run(None, {k: v for k, v in feeds.items() if k in {i.name for i in decoder.get_inputs()}})[0]
    found = masks[0, 0] > 0.0

    # Only what is inside the box (a little either side) is the thing asked for.
    keep = np.zeros_like(found)
    pad_x, pad_y = 0.02 * width, 0.02 * height
    keep[max(0, int(y1 * height - pad_y)):int(y2 * height + pad_y) + 1,
         max(0, int(x1 * width - pad_x)):int(x2 * width + pad_x) + 1] = True
    found &= keep
    return found if found.any() else None


def _largest(region):
    """The largest 4-connected part of boolean array [region]."""
    height, width = region.shape
    seen = np.zeros_like(region)
    best = None
    best_size = 0

    for start in zip(*np.nonzero(region)):
        if seen[start]:
            continue

        stack = [start]
        seen[start] = True
        part = []

        while stack:
            y, x = stack.pop()
            part.append((y, x))

            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and region[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))

        if len(part) > best_size:
            best, best_size = part, len(part)

    kept = np.zeros_like(region)

    if best:
        ys, xs = zip(*best)
        kept[list(ys), list(xs)] = True

    return kept


# Moore neighbours, clockwise from west.
_AROUND = ((0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1))


def outline(found):
    """The outline of the largest part of boolean mask [found], as [[x, y], ...] from 0 to 1 across and
    down the picture, clockwise; [] if there is none."""
    if found is None or not found.any():
        return []

    height, width = found.shape
    ys, xs = np.nonzero(found)
    top, bottom, left, right = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    step = max(1, int(np.ceil(max(bottom - top, right - left) / TRACE_SIZE)))
    small = _largest(found[top:bottom:step, left:right:step])
    grid = np.pad(small, 1)

    starts = np.argwhere(grid)

    if not len(starts):
        return []

    start = tuple(starts[0])        # top-most, then left-most: its west neighbour is outside
    points = [start]
    current, came = start, 0        # searching clockwise from the west

    for _ in range(grid.size * 4):
        for turn in range(8):
            direction = (came + turn) % 8
            dy, dx = _AROUND[direction]
            nxt = (current[0] + dy, current[1] + dx)

            if grid[nxt]:
                break
        else:
            break                   # a single pixel

        current = nxt
        came = (direction + 5) % 8  # back past the way we came, then on clockwise

        if current == start:
            break

        points.append(current)

    if len(points) > MAX_POINTS:
        points = [points[int(i * len(points) / MAX_POINTS)] for i in range(MAX_POINTS)]

    # Back to the picture: the grid's 1-pixel pad, the step, the crop; the middle of each pixel.
    return [[round(min(1.0, max(0.0, (left + (x - 1 + 0.5) * step) / width)), 4),
             round(min(1.0, max(0.0, (top + (y - 1 + 0.5) * step) / height)), 4)] for y, x in points]


def cutout(rgb, found):
    """The thing alone: a PIL RGBA image of [rgb] cropped to mask [found], transparent outside it."""
    from PIL import Image

    ys, xs = np.nonzero(found)
    top, bottom, left, right = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    rgba = np.zeros((bottom - top, right - left, 4), dtype=np.uint8)
    rgba[..., :3] = rgb[top:bottom, left:right]
    rgba[..., 3] = np.where(found[top:bottom, left:right], 255, 0)
    return Image.fromarray(rgba, "RGBA")


def forget():
    """Load the model afresh next time. For tests."""
    global _sessions

    with _lock:
        _sessions = None
