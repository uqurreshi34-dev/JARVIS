# pylint: disable=import-error

"""Local JARVIS bridge running inside Blender."""

import hmac
import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy


_HOST = "127.0.0.1"
_TOKEN = os.getenv("JARVIS_BLENDER_TOKEN", "")
_PORT = int(os.getenv("JARVIS_BLENDER_PORT", "0"))
_STATE_FILE = Path(os.getenv("JARVIS_BLENDER_STATE", ""))

_MAX_BODY = 64 * 1024
_REQUEST_TIMEOUT = 5.0

_requests = queue.Queue()
_server = None


def _json_bytes(payload):
    return json.dumps(payload).encode("utf-8")


def _scene_snapshot():
    """Return a compact, LLM-friendly description of the current scene."""
    objects = []

    for obj in bpy.context.scene.objects:
        if len(objects) >= 500:
            break

        dimensions = tuple(round(float(value), 4) for value in obj.dimensions)
        location = tuple(
            round(float(value), 4)
            for value in obj.matrix_world.translation
        )
        rotation = tuple(
            round(float(value), 4)
            for value in obj.rotation_euler
        )

        materials = []

        if getattr(obj, "data", None) is not None:
            slots = getattr(obj.data, "materials", None)

            if slots is not None:
                materials = [
                    material.name
                    for material in slots
                    if material is not None
                ]

        objects.append(
            {
                "name": obj.name,
                "type": obj.type,
                "visible": not obj.hide_get(),
                "location": location,
                "dimensions": dimensions,
                "rotation": rotation,
                "scale": tuple(
                    round(float(value), 4)
                    for value in obj.scale
                ),
                "materials": materials,
                "parent": obj.parent.name if obj.parent else None,
            }
        )

    active = bpy.context.view_layer.objects.active

    return {
        "blend_path": bpy.data.filepath,
        "scene": bpy.context.scene.name,
        "active_object": active.name if active else None,
        "selected_objects": [
            obj.name
            for obj in bpy.context.selected_objects
        ],
        "objects": objects,
    }


def _run_pending():
    """Execute queued bridge requests on Blender's main application thread."""
    while True:
        try:
            job = _requests.get_nowait()
        except queue.Empty:
            break

        try:
            job["result"] = job["function"]()
        except Exception as error:
            job["error"] = str(error)
        finally:
            job["done"].set()

    return 0.1


def _submit(function):
    """Run a Blender-data operation on the application thread."""
    done = threading.Event()

    job = {
        "function": function,
        "done": done,
        "result": None,
        "error": None,
    }

    _requests.put(job)

    if not done.wait(_REQUEST_TIMEOUT):
        raise TimeoutError("Blender did not respond to the bridge request.")

    if job["error"]:
        raise RuntimeError(job["error"])

    return job["result"]


class _Handler(BaseHTTPRequestHandler):
    """Very small authenticated localhost bridge."""

    def log_message(self, format_string, *args):
        return

    def _authorized(self):
        supplied = self.headers.get("X-JARVIS-Token", "")

        return bool(
            _TOKEN
            and hmac.compare_digest(supplied, _TOKEN)
            and self.client_address[0] == _HOST
        )

    def _send(self, status, payload):
        data = _json_bytes(payload)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authorized():
            self._send(403, {"ok": False, "error": "Forbidden."})
            return

        if self.path == "/health":
            try:
                result = _submit(
                    lambda: {
                        "ok": True,
                        "blend_path": bpy.data.filepath,
                        "scene": bpy.context.scene.name,
                        "pid": os.getpid(),
                    }
                )

                self._send(200, result)

            except Exception as error:
                self._send(
                    503,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )

            return

        if self.path == "/scene":
            try:
                self._send(
                    200,
                    _submit(_scene_snapshot),
                )

            except Exception as error:
                self._send(
                    503,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )

            return

        self._send(
            404,
            {
                "ok": False,
                "error": "Unknown bridge endpoint.",
            },
        )


def _start():
    """Start the bridge once Blender has loaded the current file."""
    global _server

    if not _TOKEN or not _STATE_FILE:
        print("[JARVIS] Blender bridge credentials are missing.")
        return None

    if _server is not None:
        return None

    try:
        _server = ThreadingHTTPServer(
            (_HOST, _PORT),
            _Handler,
        )

        thread = threading.Thread(
            target=_server.serve_forever,
            name="JARVIS-Blender-Bridge",
            daemon=True,
        )
        thread.start()

        actual_port = _server.server_address[1]

        _STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        _STATE_FILE.write_text(
            json.dumps(
                {
                    "host": _HOST,
                    "port": actual_port,
                    "token": _TOKEN,
                }
            ),
            encoding="utf-8",
        )

        print(
            f"[JARVIS] Blender bridge ready on {_HOST}:{actual_port}",
            flush=True,
        )

    except OSError as error:
        _server = None
        print(
            f"[JARVIS] Blender bridge could not start: {error}",
            flush=True,
        )

    return None


# Timer callbacks execute through Blender's application loop rather than
# directly from the HTTP worker thread, keeping bpy access on Blender's side.
if not bpy.app.timers.is_registered(_run_pending):
    bpy.app.timers.register(
        _run_pending,
        first_interval=0.1,
        persistent=True,
    )

bpy.app.timers.register(
    _start,
    first_interval=0.5,
    persistent=True,
)
