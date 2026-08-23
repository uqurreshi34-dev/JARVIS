"""Look through the webcam.

Capture goes through pygrabber, which is pure Python driving DirectShow via
comtypes. Nothing here ships a compiled binary, so Windows Application
Control has nothing to block, unlike OpenCV.

Windows itself gates camera access: if desktop apps are denied the camera in
Privacy settings, no application can prompt for it, so JARVIS says where to
turn it on rather than pretending it can ask.
"""

import io
import threading
import time

import numpy as np

# pygrabber is Windows only and needs comtypes; both are optional so the rest
# of JARVIS still runs without a camera.
try:
    from pygrabber.dshow_graph import FilterGraph

    _AVAILABLE = True
except Exception as error:  # pragma: no cover - depends on the machine
    print(f"[JARVIS] camera support unavailable: {error}")
    _AVAILABLE = False

try:
    from PIL import Image

    _PIL = True
except ImportError:
    _PIL = False


# The device is kept open briefly so a follow-up question does not pay the
# cost of opening it again, then released so the camera light goes out.
IDLE_SECONDS = 30.0

# Frames discarded after opening, while exposure and white balance settle.
WARMUP_FRAMES = 6

# Sent to the vision model at this width, which is plenty to recognise
# objects and keeps the request small.
SEND_WIDTH = 768

_lock = threading.Lock()
_graph = None
_opened_at = 0.0
_device_index = 0

# Frames arrive on DirectShow's own thread, into this holder.
_latest = {"frame": None}
_arrived = threading.Event()


def _receive(frame):
    """Called by DirectShow whenever a frame is grabbed."""
    _latest["frame"] = frame
    _arrived.set()


def available():
    return _AVAILABLE


def devices():
    """Names of the cameras Windows can see."""
    if not _AVAILABLE:
        return []

    try:
        return FilterGraph().get_input_devices()
    except Exception as error:
        print(f"[JARVIS] could not list cameras: {error}")
        return []


def _open():
    """Open the camera, reusing it if it is already running."""
    global _graph, _opened_at

    if _graph is not None:
        _opened_at = time.monotonic()
        return _graph

    graph = FilterGraph()

    names = graph.get_input_devices()

    if not names:
        raise RuntimeError("no camera found")

    index = _device_index if _device_index < len(names) else 0

    graph.add_video_input_device(index)
    graph.add_sample_grabber(_receive)
    graph.add_null_render()
    graph.prepare_preview_graph()
    graph.run()

    _graph = graph
    _opened_at = time.monotonic()

    print(f"[JARVIS] camera opened: {names[index]}")

    return graph


def release():
    """Close the camera so its light goes off."""
    global _graph, _opened_at

    with _lock:
        if _graph is None:
            return

        try:
            _graph.stop()
        except Exception:
            pass

        _graph = None
        _opened_at = 0.0

        print("[JARVIS] camera released")


def _release_if_idle():
    """Close the camera once it has been unused for a while."""
    if _graph is None:
        return

    if time.monotonic() - _opened_at > IDLE_SECONDS:
        release()


def capture():
    """Grab one frame as PNG bytes, or None with a reason printed."""
    if not _AVAILABLE:
        return None

    with _lock:
        try:
            graph = _open()
        except Exception as error:
            print(f"[JARVIS] could not open the camera: {error}")
            return None

        try:
            # The first frames are usually dark while the sensor settles, so
            # several are taken and the last one kept.
            for attempt in range(WARMUP_FRAMES):
                _arrived.clear()

                if not graph.grab_frame():
                    print("[JARVIS] the capture graph has no sample grabber")
                    return None

                if not _arrived.wait(timeout=3.0) and attempt == 0:
                    print("[JARVIS] the camera sent no frame")
                    return None

                time.sleep(0.05)

        except Exception as error:
            print(f"[JARVIS] could not take a picture: {error}")
            return None

    frame = _latest.get("frame")

    if frame is None:
        print("[JARVIS] the camera returned no image")
        return None

    return _to_png(frame)


def _to_png(frame):
    """Turn a captured frame into PNG bytes."""
    try:
        array = np.asarray(frame)

        if array.ndim != 3:
            print("[JARVIS] unexpected image from the camera")
            return None

        # pygrabber hands back BGR, and often upside down.
        array = array[:, :, ::-1]
        array = np.flipud(array)

        if not _PIL:
            print("[JARVIS] Pillow is needed to encode the picture")
            return None

        image = Image.fromarray(array.astype("uint8"), "RGB")

        if image.width > SEND_WIDTH:
            height = round(image.height * SEND_WIDTH / image.width)
            image = image.resize((SEND_WIDTH, height), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        return buffer.getvalue()

    except Exception as error:
        print(f"[JARVIS] could not encode the picture: {error}")
        return None


def housekeeping():
    """Called periodically so an unused camera does not stay open."""
    with _lock:
        _release_if_idle()


_SYSTEM_PROMPT = (
    "You are JARVIS, glancing through a webcam for your employer. Answer in "
    "one short sentence, plainly, as a butler would. Name what you can see "
    "and say nothing else. If the picture is too dark or unclear to tell, "
    "say so. Address him as sir at most once."
)

# What was asked last, so "how about now?" repeats the same question.
_last_question = "What am I holding?"


def look(question=None):
    """Take a picture and answer a question about it.

    Returns (answer, image bytes). The image is returned even when the
    answer fails, so the panel can still show what was seen.
    """
    global _last_question

    if not _AVAILABLE:
        return permission_hint(), None

    if not devices():
        return permission_hint(), None

    image = capture()

    if not image:
        # The camera was found, so this is not a permissions problem.
        return (
            "The camera opened but sent no picture, sir. "
            "Something else may be using it."
        ), None

    # Imported here so a missing provider cannot stop the rest of JARVIS
    # from loading.
    try:
        from providers import vision
    except Exception as error:
        print(f"[JARVIS] vision unavailable: {error}")
        return "I can see, but I can't describe it, sir.", image

    asked = (question or "").strip() or _last_question
    _last_question = asked

    answer = vision(f"{_SYSTEM_PROMPT}\n\nQuestion: {asked}", image)

    if not answer:
        return "I couldn't make sense of that, sir.", image

    return answer, image


def permission_hint():
    """What to tell the user when the camera cannot be reached."""
    return (
        "I can't reach the camera, sir. You may need to allow desktop apps "
        "to use it, in Settings, Privacy and Security, Camera."
    )
