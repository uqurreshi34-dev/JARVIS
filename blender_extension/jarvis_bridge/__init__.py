# pylint: disable=import-error

"""Persistent JARVIS bridge for Blender."""

import ast
import atexit
import hmac
import json
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy

_ALLOWED_IMPORTS = frozenset({
    "bpy",
    "math",
    "mathutils",
})

_FORBIDDEN_NAMES = frozenset({
    "exec",
    "eval",
    "open",
    "compile",
    "__import__",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "os",
    "pathlib",
})


_HOST = "127.0.0.1"
_BRIDGE_DIR = Path.home() / "JARVIS" / ".blender-bridges"
_STATE_PATH = _BRIDGE_DIR / f"{os.getpid()}.json"

_MAX_BODY = 64 * 1024
_REQUEST_TIMEOUT = 5.0

_TOKEN = secrets.token_urlsafe(32)

_requests = queue.Queue()
_server = None
_server_thread = None
_visibility_restore_state = None


def _visibility_snapshot():
    """Capture viewport visibility for every scene object."""
    return {
        obj.name: not obj.hide_get()
        for obj in bpy.context.scene.objects
    }


def _remember_visibility_change(before, after):
    """Remember the state immediately before a visibility change."""
    global _visibility_restore_state

    if before != after:
        _visibility_restore_state = before


def _json_bytes(payload):
    return json.dumps(payload).encode("utf-8")


def _scene_snapshot():
    """Return compact structured state from the active Blender scene."""
    objects = []

    for obj in bpy.context.scene.objects:
        if len(objects) >= 500:
            break

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
                "location": tuple(
                    round(float(value), 4)
                    for value in obj.matrix_world.translation
                ),
                "dimensions": tuple(
                    round(float(value), 4)
                    for value in obj.dimensions
                ),
                "rotation": tuple(
                    round(float(value), 4)
                    for value in obj.rotation_euler
                ),
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
        "ok": True,
        "pid": os.getpid(),
        "blend_path": bpy.data.filepath,
        "scene": bpy.context.scene.name,
        "active_object": active.name if active else None,
        "selected_objects": [
            obj.name
            for obj in bpy.context.selected_objects
        ],
        "visibility_restore": _visibility_restore_state or {},
        "objects": objects,
    }


def _validate_script(source):
    """Reject generated code that can escape the Blender modelling task."""
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]

                if root not in _ALLOWED_IMPORTS:
                    raise ValueError(
                        f"Disallowed import: {alias.name}"
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError(
                    "Relative imports are not allowed."
                )

            module = node.module or ""
            root = module.split(".", 1)[0]

            if root not in _ALLOWED_IMPORTS:
                raise ValueError(
                    f"Disallowed import: {module}"
                )

        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                raise ValueError(
                    f"Disallowed name: {node.id}"
                )

    return True


def _run_pending():
    """Execute queued bpy work on Blender's main thread."""
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
    """Marshal a Blender operation onto the application thread."""
    done = threading.Event()

    job = {
        "function": function,
        "done": done,
        "result": None,
        "error": None,
    }

    _requests.put(job)

    if not done.wait(_REQUEST_TIMEOUT):
        raise TimeoutError(
            "Blender did not respond to the bridge request."
        )

    if job["error"]:
        raise RuntimeError(job["error"])

    return job["result"]


class _Handler(BaseHTTPRequestHandler):
    """Authenticated localhost HTTP bridge."""

    def log_message(self, format_string, *args):
        return

    def _authorized(self):
        supplied = self.headers.get("X-JARVIS-Token", "")

        return (
            self.client_address[0] == _HOST
            and hmac.compare_digest(supplied, _TOKEN)
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
            self._send(
                403,
                {
                    "ok": False,
                    "error": "Forbidden.",
                },
            )
            return

        if self.path == "/health":
            try:
                self._send(
                    200,
                    _submit(
                        lambda: {
                            "ok": True,
                            "pid": os.getpid(),
                            "blend_path": bpy.data.filepath,
                            "scene": bpy.context.scene.name,
                        }
                    ),
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

    def do_POST(self):
        if not self._authorized():
            self._send(
                403,
                {
                    "ok": False,
                    "error": "Forbidden.",
                },
            )
            return

        if self.path != "/execute":
            self._send(
                404,
                {
                    "ok": False,
                    "error": "Unknown bridge endpoint.",
                },
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))

            if length <= 0 or length > _MAX_BODY:
                raise ValueError("Invalid request size.")

            payload = json.loads(
                self.rfile.read(length).decode("utf-8")
            )

            script = payload.get("script")

            if not isinstance(script, str) or not script.strip():
                raise ValueError("Missing Blender script.")

            _validate_script(script)

            def execute():
                before_visibility = _visibility_snapshot()

                namespace = {
                    "bpy": bpy,
                }

                exec(
                    compile(script, "<jarvis-blender>", "exec"),
                    namespace,
                    namespace,
                )

                after_visibility = _visibility_snapshot()

                _remember_visibility_change(
                    before_visibility,
                    after_visibility,
                )

                bpy.ops.wm.save_as_mainfile(
                    filepath=bpy.data.filepath
                )

                return {
                    "ok": True,
                    "blend_path": bpy.data.filepath,
                }

            result = _submit(execute)

            self._send(200, result)

        except Exception as error:
            self._send(
                400,
                {
                    "ok": False,
                    "error": str(error),
                },
            )


def _write_state(port):
    """Publish the connection details for the JARVIS client."""
    _BRIDGE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    _STATE_PATH.write_text(
        json.dumps(
            {
                "host": _HOST,
                "port": port,
                "token": _TOKEN,
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )


def _remove_state():
    try:
        _STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _stop_server():
    global _server
    global _server_thread

    if _server is None:
        _remove_state()
        return

    try:
        _server.shutdown()
        _server.server_close()
    except OSError:
        pass

    if _server_thread is not None:
        _server_thread.join(timeout=1)

    _server = None
    _server_thread = None

    _remove_state()


def _start_server():
    global _server
    global _server_thread

    if _server is not None:
        return

    try:
        _server = ThreadingHTTPServer(
            (_HOST, 0),
            _Handler,
        )

        port = _server.server_address[1]

        _write_state(port)

        _server_thread = threading.Thread(
            target=_server.serve_forever,
            name="JARVIS-Blender-Bridge",
            daemon=True,
        )
        _server_thread.start()

        print(
            f"[JARVIS] Blender bridge ready on "
            f"{_HOST}:{port}",
            flush=True,
        )

    except OSError as error:
        _server = None
        _server_thread = None

        print(
            f"[JARVIS] Blender bridge could not start: {error}",
            flush=True,
        )


def register():
    """Start the persistent JARVIS bridge."""
    if not bpy.app.timers.is_registered(_run_pending):
        bpy.app.timers.register(
            _run_pending,
            first_interval=0.1,
            persistent=True,
        )

    _start_server()


def unregister():
    """Stop the bridge and remove its connection state."""
    if bpy.app.timers.is_registered(_run_pending):
        bpy.app.timers.unregister(_run_pending)

    _stop_server()


atexit.register(_stop_server)

if __name__ == "__main__":
    register()
