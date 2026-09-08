"""Create Blender scenes from JARVIS reference-image modelling briefs."""

import ast
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from providers import chat, vision, vision_chat

from actions import files, images

_MODIFY_PROMPT = """
You are an expert procedural Blender artist modifying an EXISTING scene.

The user wants to change the current Blender model.

Treat the current Blender scene description as the authoritative state.

A reference image may be supplied as supplementary visual context when
available. It is optional and must never be required for a modification.

When no reference image is available, reason entirely from the live Blender
scene description.

Identify objects semantically from the live scene description rather than
assuming names, ordering, or previously selected objects.

Visibility can be changed naturally: objects may be hidden, shown, or isolated.

Visibility rules:
- When hiding or showing an object, ALWAYS use obj.hide_set(True) or
  obj.hide_set(False).
- When isolating objects, use obj.hide_set(...) for the visibility changes.
- Do NOT use obj.hide_viewport.
- Do NOT hide or unhide collections.
- Do NOT use local view.
- Do NOT use viewport-specific visibility mechanisms or operators.
- These rules are required so JARVIS can reliably record and restore the
  exact visibility state from before the last visibility operation.

When the user asks to restore what was hidden or undo a recent isolation, use the
visibility_restore state supplied with the current scene. Restore those objects
to exactly the visibility they had before the last visibility change.

Do not assume that every object was visible before isolation.

Generate a complete Python script using bpy that performs ONLY the requested
modification on the existing scene.

Rules:
- Do not rebuild the scene.
- Do not delete unrelated objects.
- Identify objects semantically from the supplied scene description.
- Preserve existing geometry unless the request requires changing it.
- Make the smallest sensible modification that fulfills the request.
- Use only bpy, bmesh, math, mathutils, and random.
- Do not import or use os, pathlib, subprocess, socket, requests, urllib,
  open, exec, eval, compile, or __import__.
- Do not save the .blend; the JARVIS Blender bridge saves it.
- Return only valid Python source.

CURRENT BLENDER SCENE:
"""


_REFERENCE_PROMPT = """
Return a compact but information-dense modelling specification.

Cover:

- SUBJECT: what the object/building is and its architectural style
- MASSING: overall footprint, height, wings, central mass, roof mass
- PROPORTIONS: approximate ratios between the major elements
- FACADE: windows, doors, columns, porticos, arches, balconies,
  pediments, stairs, trim, cornices, etc.
- REPETITION: repeated structures and approximate counts/rhythm
- SYMMETRY: bilateral or radial symmetry visible in the reference
- DEPTH: features that clearly project, recess, or overlap
- ROOF: roof shape, levels, dormers, chimneys, parapets, etc.
- MATERIALS: major visible material groups
- CAMERA: approximate viewpoint, elevation, orientation, and framing
- PRIORITIES: the 8–12 features that matter most for visual recognition
- UNCERTAINTY: only details genuinely hidden or ambiguous in the image

Be concrete about proportions, counts, spacing, and relationships when they
can reasonably be inferred.

Do not write Blender Python.
Do not invent hidden geometry as fact.
"""

_MULTIVIEW_SUPPORTED_VIEWS = (
    "front",
    "back",
    "left",
    "right",
)

_MULTIVIEW_MIN_VIEWS = 3

_MULTIVIEW_PROMPT = """
You are analysing multiple reference photographs of the SAME physical subject
for accurate 3D reconstruction.

The contact sheet is labelled with the actual view names.

Produce ONE unified modelling specification for the subject.

Cross-reference ALL supplied views before making conclusions.

Use the available views to establish:
- overall 3D massing
- width, height and depth proportions
- front, rear and side geometry
- features that project, recess, overlap, or continue around corners
- symmetry supported by the evidence
- repeated structures and counts
- materials and major colour relationships
- recognisable silhouette
- geometry confirmed by multiple views
- genuine remaining uncertainty

Use the front and rear views to establish longitudinal structure when those
views are available.

Use left and right views to establish lateral depth when those views are
available.

Do not assume an unseen side is identical unless symmetry is strongly
supported by the supplied views.

Do not invent hidden geometry as fact.

Be concrete about proportions, counts, spacing, and spatial relationships.

Do not write Blender Python.
"""


_SCRIPT_PROMPT = """
You are an expert procedural Blender artist creating a high-quality,
reference-faithful 3D reconstruction.

Write a complete Blender 5.2 Python script using bpy that creates the
strongest accurate model you can from the modelling brief below.

The goal is not a rough blockout. Reproduce the reference's distinctive
architecture, proportions, repeated elements, depth, silhouette, and visible
details as faithfully as practical.

Requirements:
- Build the described object as actual editable Blender geometry.
- Prioritise recognisable silhouette and major forms over tiny details.
- Keep the generated script concise; avoid lengthy comments, redundant helpers, and unnecessary boilerplate.
- Use sensible primitives, bevels, curves, modifiers, materials and linked
  duplicates where appropriate.
- Create useful materials matching the visible reference.
- Add a camera and lighting so the resulting scene is immediately viewable.
- Clear Blender's default scene first.
- Do not use external assets.
- Do not download anything.
- Do not read or write files.
- Do not use subprocess, os, pathlib, requests, sockets, or network access.
- Do not use exec, eval, open, or __import__.
- Do not save the .blend file; JARVIS will do that after the script runs.
- The script must be self-contained and executable with Blender 5.2.
- Return only the Python source code.
- Use only bpy, bmesh, math, mathutils, and random if imports are needed.
- Every line must be valid Python 3 syntax.
- Do not invent modules or conditional-import expressions.
- Match the reference's proportions and major spatial relationships before
  adding small decorative details.
- Model distinctive architectural features as real 3D geometry whenever they
  are visible in the reference.
- Do not replace important features with arbitrary cubes or flat placeholders.
- Use symmetry aggressively where the reference supports it.
- Use procedural repetition, linked duplicates, arrays, curves, or loops for
  repeated windows, columns, railings, arches, roof elements, and similar
  structures.
- Give important façade elements believable depth rather than leaving them
  as flat surfaces.
- Treat the roofline, entrances, windows, columns, stairs, balconies,
  pediments, cornices, and other defining features as high-priority geometry
  when present.
- Spend geometry on features that materially affect recognition from the
  reference camera.
- Preserve consistent scale between all architectural elements.
- Do not stop after creating only the primary masses; continue through the
  major secondary architectural features visible in the reference.
- Make the final scene look intentionally modelled, not like a primitive
  blockout.

MODELLING BRIEF:
"""

_REVIEW_PROMPT = """
You are the visual quality-control artist for a Blender reconstruction.

The supplied image is a comparison plate:
- LEFT = the original reference image
- RIGHT = the current Blender preview render

Compare the two images directly.

Your job is to diagnose the most important visible differences between the
current Blender result and the reference so another expert Blender artist
can correct them.

Do NOT praise the model.
Do NOT describe things that are already correct.
Do NOT give generic modelling advice.
Do NOT invent geometry that cannot be supported by the reference.

Prioritise the largest visual mismatches first.

Evaluate, in this order:
1. silhouette and overall proportions
2. major forms and their placement
3. distinctive geometry and missing features
4. repeated structures and their spacing/count
5. visible depth and shape
6. major materials and colour relationships
7. camera framing/viewpoint

Treat a camera mismatch as a camera problem. Do not tell the modeller to
change correct geometry just to compensate for an incorrect camera.

Return the 3 to 5 highest-impact actionable issues.
Return fewer when there are genuinely fewer important problems.

For every issue use EXACTLY this format:

PRIORITY: 1
TYPE: geometry | proportion | repetition | material | camera
LOCATION: specific part of the model
PROBLEM: one concrete visible mismatch
ACTION: one concrete change that should be made

Then continue with PRIORITY 2, PRIORITY 3, etc.

The issues must be ordered from the change most likely to improve visual
similarity to the change least likely to improve it.

Prefer measurable or spatial descriptions when possible, such as:
- too wide / too narrow
- too tall / too short
- too far left / right
- too deep / shallow
- too large / small
- missing
- too few / too many
- too closely / widely spaced

Do not ask for hidden or unseen sides of the object.

If the current model is already visually close and there are no important
remaining mismatches, return exactly:

NO_CRITICAL_MISMATCHES
"""

_REFINEMENT_PROMPT = """
You are an expert Blender artist performing a targeted visual refinement
of an EXISTING scene.

A first model has already been generated and rendered.

Use the supplied visual QA report to improve the existing scene.

Rules:
- Do NOT rebuild the scene.
- Do NOT delete correct unrelated geometry.
- Preserve everything that is already correct.
- Fix the highest-impact visual mismatches first.
- Match the reference proportions and major spatial relationships.
- Add missing distinctive geometry when the QA report identifies it.
- Correct incorrectly scaled or positioned geometry.
- Improve repeated structures using procedural repetition, linked duplicates,
  arrays, curves, or loops where appropriate.
- Correct camera framing when the QA report identifies a camera mismatch.
- Correct major materials when the QA report identifies a visible mismatch.
- Use actual editable Blender geometry.
- Use only bpy, bmesh, math, and mathutils.
- Do NOT import re.
- Do NOT use regular expressions.
- Treat EVERY PRIORITY in the VISUAL QA REPORT as a required correction.
- Address priorities in numerical order.
- Do not silently skip a priority because another correction is easier.
- Every listed priority must result in a concrete change to the existing
  Blender scene whenever the supplied scene and reference support that change.
- Preserve correct work from earlier priorities while applying later ones.
- Before returning the script, verify that you have addressed every listed
  priority.
- If a requested correction cannot safely be made from the available scene
  information, make the most defensible supported correction rather than
  silently ignoring the priority.
- Do NOT import any other module.
- The approved Blender imports are exactly: bpy, bmesh, math, mathutils, random.
- Prefer ordinary string operations, loops, lists, dictionaries, and
  direct Blender API calls instead of helper modules.
- Start with the Blender API work; do not add unnecessary imports.
- Do not use external assets.
- Do not download anything.
- Do not read or write files.
- Do not save the .blend; the JARVIS Blender bridge saves it.
- Return only valid Python source.

The goal is a visibly better match to the reference, not a new design.

ORIGINAL MODELLING BRIEF:
"""

_ALLOWED_IMPORTS = frozenset({
    "bpy",
    "bmesh",
    "math",
    "mathutils",
    "random",
})


_FORBIDDEN_CALLS = frozenset({
    "exec",
    "eval",
    "open",
    "__import__",
    "compile",
})

_FORBIDDEN_BPY_OPERATIONS = frozenset({
    ("bpy", "ops", "wm", "quit_blender"),
    ("bpy", "ops", "wm", "save_as_mainfile"),
})

_REFERENCE_CACHE_VERSION = "1"
_REFERENCE_CACHE_DIR = ".reference-analysis-cache"
_PREVIEW_WIDTH = 768
_PREVIEW_HEIGHT = 768
_RENDER_TIMEOUT = 60.0
_REFINEMENT_PASSES = 2

_RESTORE_WORDS = (
    "restore",
    "restore the",
    "bring back",
    "bring the",
    "undo isolation",
    "undo the isolation",
)


def _is_restore_request(request):
    """True when the user is asking to restore previous visibility."""
    text = re.sub(
        r"[^\w\s]",
        " ",
        (request or "").casefold(),
    )
    text = " ".join(text.split())

    return any(
        text == word
        or text.startswith(word + " ")
        for word in _RESTORE_WORDS
    )


def _reference_cache_path(image_bytes):
    """Return the on-disk cache path for this image and reference prompt."""
    root = files.root()

    if not root:
        return None

    digest = hashlib.sha256(
        _REFERENCE_CACHE_VERSION.encode("utf-8")
        + _REFERENCE_PROMPT.encode("utf-8")
        + image_bytes
    ).hexdigest()

    cache_dir = Path(root) / _REFERENCE_CACHE_DIR

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    return cache_dir / f"{digest}.txt"


def _load_cached_reference_analysis(image_bytes):
    """Return cached visual analysis for this exact image, if available."""
    path = _reference_cache_path(image_bytes)

    if not path:
        return None

    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    return text or None


def _save_cached_reference_analysis(image_bytes, analysis):
    """Persist a successful reference analysis for reuse."""
    path = _reference_cache_path(image_bytes)

    if not path or not analysis:
        return

    try:
        path.write_text(
            analysis,
            encoding="utf-8",
        )
    except OSError:
        pass


def _attribute_chain(node):
    """Return a dotted attribute chain such as ('bpy', 'ops', 'wm', 'quit_blender')."""
    parts = []

    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value

    if not isinstance(node, ast.Name):
        return ()

    parts.append(node.id)

    return tuple(reversed(parts))


def _find_blender():
    """Find Blender without depending on a particular installed version."""
    configured = (os.getenv("BLENDER_EXECUTABLE") or "").strip()

    if configured:
        path = Path(configured).expanduser()

        if path.is_file():
            return str(path)

    found = shutil.which("blender")

    if found:
        return found

    program_files = Path(
        os.getenv("ProgramFiles") or r"C:\Program Files"
    )

    root = program_files / "Blender Foundation"

    if not root.is_dir():
        return None

    candidates = sorted(
        root.glob("*/blender.exe"),
        reverse=True,
    )

    if candidates:
        return str(candidates[0])

    return None


def available():
    """True when Blender can be found."""
    return _find_blender() is not None


_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_DIR = Path.home() / "JARVIS" / ".blender-bridges"
_BRIDGE_STARTUP_TIMEOUT = 30.0


def _bridge_states():
    """Return available Blender bridge registrations, newest first."""
    try:
        states = list(_BRIDGE_DIR.glob("*.json"))
    except OSError:
        return ()

    return tuple(
        sorted(
            states,
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )


def _read_bridge_state(path):
    """Read one Blender bridge registration."""
    try:
        state = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None

    if not isinstance(state, dict):
        return None

    if not state.get("token") or not state.get("port"):
        return None

    return state


def _bridge_request(endpoint):
    """Call the first healthy authenticated Blender bridge."""
    for path in _bridge_states():
        state = _read_bridge_state(path)

        if not state:
            continue

        try:
            port = int(state["port"])
        except (TypeError, ValueError):
            continue

        request = urllib.request.Request(
            f"http://{_BRIDGE_HOST}:{port}{endpoint}",
            headers={
                "X-JARVIS-Token": str(state["token"]),
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=2,
            ) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        except Exception:
            continue

    return None


def running():
    """True when a Blender instance is connected to JARVIS."""
    response = _bridge_request("/health")

    return bool(response and response.get("ok"))


def scene_snapshot():
    """Return the current Blender scene, or raise when Blender is absent."""
    response = _bridge_request("/scene")

    if not response or not response.get("ok", True):
        raise RuntimeError(
            "I'm sorry, sir. Blender isn't running."
        )

    return response


def modeling_context():
    """Return the live Blender scene for LLM modelling decisions."""
    if not running():
        raise RuntimeError(
            "I'm sorry, sir. Blender isn't running."
        )

    scene = scene_snapshot()

    return {
        "blend_path": scene.get("blend_path"),
        "scene": scene.get("scene"),
        "active_object": scene.get("active_object"),
        "selected_objects": scene.get("selected_objects") or (),
        "visibility_restore": scene.get("visibility_restore") or {},
        "objects": tuple(
            {
                "name": obj.get("name"),
                "type": obj.get("type"),
                "visible": obj.get("visible"),
                "location": obj.get("location"),
                "dimensions": obj.get("dimensions"),
                "rotation": obj.get("rotation"),
                "scale": obj.get("scale"),
                "materials": obj.get("materials") or (),
                "parent": obj.get("parent"),
            }
            for obj in scene.get("objects") or ()
        ),
    }


def _launch_blender_gui(output_path):
    """Open a .blend and wait for the persistent bridge."""
    executable = _find_blender()

    if not executable:
        raise RuntimeError(
            "Blender could not be found."
        )

    process = subprocess.Popen(
        [
            executable,
            str(output_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + _BRIDGE_STARTUP_TIMEOUT

    while time.monotonic() < deadline:
        if running():
            return True

        if process.poll() is not None:
            raise RuntimeError(
                "Blender closed before the JARVIS bridge became available."
            )

        time.sleep(0.2)

    raise RuntimeError(
        "Blender opened, but the JARVIS Blender Bridge is not enabled."
    )


def _bridge_execute(
    script,
    restore_visibility=False,
    save=True,
):
    """Execute validated modelling code in the live Blender instance."""
    for path in _bridge_states():
        state = _read_bridge_state(path)

        if not state:
            continue

        try:
            port = int(state["port"])
        except (TypeError, ValueError):
            continue

        request = urllib.request.Request(
            f"http://{_BRIDGE_HOST}:{port}/execute",
            data=json.dumps(
                {
                    "script": script,
                    "restore_visibility": restore_visibility,
                    "save": save,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-JARVIS-Token": str(state["token"]),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=70,
            ) as response:
                payload = json.loads(
                    response.read().decode("utf-8")
                )

            if payload.get("ok"):
                return payload

            raise RuntimeError(
                payload.get("error", "Blender rejected the script.")
            )

        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")

            try:
                payload = json.loads(body)
                raise RuntimeError(
                    payload.get("error", str(error))
                ) from error
            except json.JSONDecodeError:
                raise RuntimeError(str(error)) from error

        except OSError:
            continue

    raise RuntimeError(
        "I'm sorry, sir. Blender isn't running."
    )


def _bridge_render_preview(view=None):
    """Render a fast visual preview from the live Blender scene."""
    started = time.monotonic()

    for path in _bridge_states():
        state = _read_bridge_state(path)

        if not state:
            continue

        try:
            port = int(state["port"])
        except (TypeError, ValueError):
            continue

        request = urllib.request.Request(
            f"http://{_BRIDGE_HOST}:{port}/render",
            data=json.dumps(
                {
                    "width": _PREVIEW_WIDTH,
                    "height": _PREVIEW_HEIGHT,
                    "view": view,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-JARVIS-Token": str(state["token"]),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=_RENDER_TIMEOUT + 10,
            ) as response:
                payload = json.loads(
                    response.read().decode("utf-8")
                )

            if not payload.get("ok"):
                raise RuntimeError(
                    payload.get(
                        "error",
                        "Blender could not render the preview.",
                    )
                )

            encoded = payload.get("image")

            if not encoded:
                raise RuntimeError(
                    "Blender returned no preview image."
                )

            try:
                image_bytes = base64.b64decode(
                    encoded,
                    validate=True,
                )
            except (ValueError, TypeError):
                raise RuntimeError(
                    "Blender returned an invalid preview image."
                )

            mime = payload.get("mime") or "image/png"

            print(
                f"[JARVIS] preview render took "
                f"{time.monotonic() - started:.1f}s",
                flush=True,
            )

            return {
                "bytes": image_bytes,
                "mime": mime,
            }

        except urllib.error.HTTPError as error:
            body = error.read().decode(
                "utf-8",
                errors="replace",
            )

            try:
                payload = json.loads(body)
                raise RuntimeError(
                    payload.get(
                        "error",
                        str(error),
                    )
                ) from error
            except json.JSONDecodeError:
                raise RuntimeError(
                    str(error)
                ) from error

        except OSError:
            continue

    raise RuntimeError(
        "Blender could not produce a preview render."
    )


def _comparison_image(reference_bytes, preview_bytes):
    """Create a side-by-side PNG for visual comparison."""
    target_width = _PREVIEW_WIDTH
    target_height = _PREVIEW_HEIGHT
    gap = 16

    with Image.open(io.BytesIO(reference_bytes)) as reference:
        reference_image = reference.convert("RGB")
        reference_image = ImageOps.contain(
            reference_image,
            (target_width, target_height),
            method=Image.Resampling.LANCZOS,
        )

    with Image.open(io.BytesIO(preview_bytes)) as preview:
        preview_image = preview.convert("RGB")
        preview_image = ImageOps.contain(
            preview_image,
            (target_width, target_height),
            method=Image.Resampling.LANCZOS,
        )

    canvas = Image.new(
        "RGB",
        (
            target_width * 2 + gap,
            target_height,
        ),
        "white",
    )

    reference_x = (
        target_width - reference_image.width
    ) // 2

    reference_y = (
        target_height - reference_image.height
    ) // 2

    preview_x = (
        target_width
        + gap
        + (target_width - preview_image.width) // 2
    )

    preview_y = (
        target_height - preview_image.height
    ) // 2

    canvas.paste(
        reference_image,
        (
            reference_x,
            reference_y,
        ),
    )

    canvas.paste(
        preview_image,
        (
            preview_x,
            preview_y,
        ),
    )

    output = io.BytesIO()

    canvas.save(
        output,
        format="PNG",
    )

    return output.getvalue()


def _is_multiview_request(request):
    """True when the user explicitly requests multiple reference views."""
    text = (request or "").casefold()

    return any(
        phrase in text
        for phrase in (
            "three views",
            "3 views",
            "four views",
            "4 views",
            "multiple views",
            "multi view",
            "multi-view",
            "multiple angles",
            "several views",
        )
    )


def _multiview_subject(request):
    """Extract the subject name from a multi-view modelling request."""
    text = " ".join(
        (request or "").strip().split()
    )

    text = re.sub(
        r"^\s*(?:model|make|build|create)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s+(?:in|using)\s+blender\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s+(?:from|using)\s+(?:these\s+)?"
        r"(?:(?:\d+|one|two|three|four|five|six)\s+)?"
        r"(?:multiple\s+|several\s+|various\s+|multi[- ]?)?"
        r"(?:views?|angles?|photos?|images?|reference\s+views?)"
        r"\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return text.strip(" .,") or "multiview"


def _reference_set_path(subject):
    """Return the matching JARVIS reference-set folder."""
    root = (
        Path.home()
        / "JARVIS"
        / "reference-sets"
    )

    requested = re.sub(
        r"[^a-z0-9]+",
        "",
        (subject or "").casefold(),
    )

    exact = root / _safe_stem(subject)

    if exact.is_dir():
        return exact

    try:
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue

            candidate_name = re.sub(
                r"[^a-z0-9]+",
                "",
                candidate.name.casefold(),
            )

            if candidate_name == requested:
                return candidate

    except OSError:
        pass

    return exact


def has_reference_set(request):
    """True when a usable multi-view reference set exists."""
    if not _is_multiview_request(request):
        return False

    subject = _multiview_subject(request)
    folder = _reference_set_path(subject)

    if not folder.is_dir():
        return False

    available_views = {
        view
        for view in _MULTIVIEW_SUPPORTED_VIEWS
        if _find_reference_view(folder, view)
    }

    if len(available_views) < _MULTIVIEW_MIN_VIEWS:
        return False

    if "front" not in available_views:
        return False

    if "back" not in available_views:
        return False

    return bool(
        {"left", "right"} & available_views
    )


def _find_reference_view(folder, view):
    """Find a local image for one named reference view."""
    for suffix in (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
    ):
        path = folder / f"{view}{suffix}"

        if path.is_file():
            return path

    return None


def _load_multiview_references(subject):
    """Load all available supported reference views."""
    folder = _reference_set_path(subject)

    if not folder.is_dir():
        raise RuntimeError(
            f"Reference set not found: {folder}"
        )

    views = {}

    for view in _MULTIVIEW_SUPPORTED_VIEWS:
        path = _find_reference_view(
            folder,
            view,
        )

        if path is None:
            continue

        if not images.supports_path(path):
            raise RuntimeError(
                f"Invalid {view} reference image: {path}"
            )

        views[view] = path.read_bytes()

    if len(views) < _MULTIVIEW_MIN_VIEWS:
        raise RuntimeError(
            "A multi-view reference set needs at least "
            f"{_MULTIVIEW_MIN_VIEWS} valid views."
        )

    if "front" not in views or "back" not in views:
        raise RuntimeError(
            "A multi-view reference set must contain front and back views."
        )

    if not {"left", "right"} & views.keys():
        raise RuntimeError(
            "A multi-view reference set must contain at least "
            "one side view."
        )

    return views


def _multiview_contact_sheet(views):
    """Create a labelled contact sheet for all available views."""
    from PIL import ImageDraw

    width = 768
    height = 768
    gap = 16
    columns = 2
    rows = (len(views) + columns - 1) // columns

    canvas = Image.new(
        "RGB",
        (
            width * columns + gap * (columns - 1),
            height * rows + gap * (rows - 1),
        ),
        "white",
    )

    draw = ImageDraw.Draw(canvas)

    for index, view in enumerate(views):
        with Image.open(
            io.BytesIO(views[view])
        ) as image:
            image = image.convert("RGB")
            image = ImageOps.contain(
                image,
                (
                    width,
                    height - 40,
                ),
                method=Image.Resampling.LANCZOS,
            )

        x_slot = index % columns
        y_slot = index // columns

        cell_x = x_slot * (width + gap)
        cell_y = y_slot * (height + gap)

        x = (
            cell_x
            + (width - image.width) // 2
        )

        y = (
            cell_y
            + 40
            + (height - 40 - image.height) // 2
        )

        canvas.paste(
            image,
            (
                x,
                y,
            ),
        )

        draw.text(
            (
                cell_x + 12,
                cell_y + 12,
            ),
            view.upper(),
            fill="black",
        )

    output = io.BytesIO()

    canvas.save(
        output,
        format="PNG",
    )

    return output.getvalue()


def _analyse_multiview_references(subject, views):
    """Produce one unified modelling brief from all supplied views."""
    comparison = _multiview_contact_sheet(
        views
    )

    view_names = ", ".join(
        view.upper()
        for view in views
    )

    started = time.monotonic()

    analysis = vision(
        _MULTIVIEW_PROMPT
        + "\n\nSUBJECT:\n"
        + subject
        + "\n\nSUPPLIED VIEWS:\n"
        + view_names,
        comparison,
        mime="image/png",
        max_tokens=4000,
        reasoning_effort="low",
    )

    print(
        f"[JARVIS] multi-view reference analysis took "
        f"{time.monotonic() - started:.1f}s",
        flush=True,
    )

    if not analysis:
        raise RuntimeError(
            "The multi-view references could not be analysed."
        )

    return analysis.strip()


def _multiview_camera_script(view):
    """Position Blender's existing camera for a named reference view."""
    angles = {
        "front": 0.0,
        "back": math.pi,
        "left": -math.pi / 2.0,
        "right": math.pi / 2.0,
    }

    if view not in angles:
        raise ValueError(
            f"Unsupported multi-view render: {view}"
        )

    return f"""
import bpy
import math
from mathutils import Matrix, Vector

scene = bpy.context.scene
camera = scene.camera

if camera is None:
    raise RuntimeError("Blender scene has no active camera.")

if "_jarvis_multiview_camera_state" not in scene:
    scene["_jarvis_multiview_camera_state"] = {{
        "location": tuple(camera.location),
        "rotation": tuple(camera.rotation_euler),
    }}

objects = [
    obj
    for obj in scene.objects
    if obj.type == "MESH" and obj.visible_get()
]

if not objects:
    raise RuntimeError("Blender scene contains no visible mesh objects.")

corners = []

for obj in objects:
    corners.extend(
        obj.matrix_world @ Vector(corner)
        for corner in obj.bound_box
    )

minimum = Vector((
    min(point.x for point in corners),
    min(point.y for point in corners),
    min(point.z for point in corners),
))

maximum = Vector((
    max(point.x for point in corners),
    max(point.y for point in corners),
    max(point.z for point in corners),
))

center = (minimum + maximum) * 0.5

base = camera.location - center
distance = max(base.length, 1.0)

horizontal = Vector((
    base.x,
    base.y,
    0.0,
))

if horizontal.length < 1e-6:
    horizontal = Vector((0.0, -1.0, 0.0))
else:
    horizontal.normalize()

rotation = Matrix.Rotation(
    {angles[view]},
    4,
    "Z",
)

direction = rotation @ horizontal

location = center + direction * distance
location.z = center.z + base.z

camera.location = location
camera.rotation_euler = (
    center - location
).to_track_quat(
    "-Z",
    "Y",
).to_euler()
""".strip()


def _multiview_restore_camera_script():
    """Restore the camera to its original generated position."""
    return """
import bpy

scene = bpy.context.scene
camera = scene.camera
state = scene.get("_jarvis_multiview_camera_state")

if camera is not None and state:
    camera.location = state["location"]
    camera.rotation_euler = state["rotation"]

if "_jarvis_multiview_camera_state" in scene:
    del scene["_jarvis_multiview_camera_state"]
""".strip()


def _render_multiviews(views):
    """Render the live model from every supplied reference viewpoint."""
    renders = {}

    for view in views:
        renders[view] = _bridge_render_preview(
            view=view,
        )["bytes"]

    return renders


def _multiview_qa_plate(views, renders):
    """Create labelled reference/render pairs for visual QA."""
    width = 512
    height = 512
    gap = 16
    columns = 2
    rows = len(views)

    canvas = Image.new(
        "RGB",
        (
            width * columns + gap,
            (height + 32) * rows + gap * (rows - 1),
        ),
        "white",
    )

    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)

    for row, view in enumerate(views):
        y = row * (height + 32 + gap)

        draw.text(
            (8, y + 8),
            view.upper(),
            fill="black",
        )

        for column, data in enumerate(
            (
                views[view],
                renders[view],
            )
        ):
            with Image.open(
                io.BytesIO(data)
            ) as image:
                image = image.convert("RGB")
                image = ImageOps.contain(
                    image,
                    (
                        width,
                        height,
                    ),
                    method=Image.Resampling.LANCZOS,
                )

            x = column * width

            cell_x = (
                x
                + (width - image.width) // 2
            )

            cell_y = (
                y
                + 32
                + (height - image.height) // 2
            )

            canvas.paste(
                image,
                (
                    cell_x,
                    cell_y,
                ),
            )

    output = io.BytesIO()

    canvas.save(
        output,
        format="PNG",
    )

    return output.getvalue()


def _review_multiview(views, renders, brief):
    """Run the same structured visual QA against all view pairs."""
    comparison = _multiview_qa_plate(
        views,
        renders,
    )

    prompt = (
        _REVIEW_PROMPT
        + "\n\n"
        + "THIS IS A MULTI-VIEW COMPARISON PLATE.\n"
        + "Each row contains a labelled reference view and the corresponding "
        + "current Blender render.\n"
        + "Inspect every supplied view before ranking the 3 to 5 highest-impact "
        + "global corrections.\n"
        + "When an issue belongs to only one view, name that view explicitly "
        + "in LOCATION.\n"
        + "\nORIGINAL MODELLING BRIEF:\n"
        + brief
    )

    review = vision(
        prompt,
        comparison,
        mime="image/png",
        max_tokens=1200,
        reasoning_effort="low",
    )

    review = (review or "").strip()

    issues = _parse_visual_review(
        review
    )

    print(
        "[JARVIS] multi-view visual QA report:\n"
        + review,
        flush=True,
    )

    return review, issues, comparison


def _refine_multiview_scene(brief, views):
    """Inspect and refine the same Blender model from multiple views."""
    for pass_number in range(
        1,
        _REFINEMENT_PASSES + 1,
    ):
        print(
            f"[JARVIS] multi-view visual refinement pass "
            f"{pass_number}/{_REFINEMENT_PASSES}",
            flush=True,
        )

        renders = _render_multiviews(
            views
        )

        review, issues, comparison = _review_multiview(
            views,
            renders,
            brief,
        )

        if review == "NO_CRITICAL_MISMATCHES":
            print(
                "[JARVIS] multi-view visual QA found no critical mismatches.",
                flush=True,
            )
            break

        context = modeling_context()

        scene_text = json.dumps(
            context,
            indent=2,
        )

        script = _generate_refinement_script(
            brief,
            scene_text,
            review,
            comparison,
        )

        _bridge_execute(script)

        print(
            f"[JARVIS] multi-view refinement pass "
            f"{pass_number} applied.",
            flush=True,
        )

    _render_multiviews(
        views
    )

    print(
        "[JARVIS] final multi-view refinement render completed.",
        flush=True,
    )


def _create_from_reference_set(request):
    """Create one Blender model from front/back/left references."""
    subject = _multiview_subject(request)

    print(
        f"[JARVIS] loading multi-view reference set for "
        f"{subject}...",
        flush=True,
    )

    views = _load_multiview_references(
        subject
    )

    brief = _analyse_multiview_references(
        subject,
        views,
    )

    reference_image = _multiview_contact_sheet(
        views
    )

    started = time.monotonic()

    scene_script = _generate_scene_script(
        brief,
        reference_image=reference_image,
    )

    print(
        f"[JARVIS] scene script generation took "
        f"{time.monotonic() - started:.1f}s",
        flush=True,
    )

    executable = _find_blender()

    if not executable:
        raise RuntimeError(
            "Blender could not be found. "
            "Set BLENDER_EXECUTABLE if needed."
        )

    models_dir = Path(files.root()) / "models"
    models_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        models_dir
        / f"{_safe_stem(subject)}.blend"
    )

    with tempfile.TemporaryDirectory(
        prefix="jarvis-blender-"
    ) as temporary:

        runner_path = Path(temporary) / "scene.py"

        runner_path.write_text(
            _write_runner(
                scene_script,
                output_path,
            ),
            encoding="utf-8",
        )

        blender_started = time.monotonic()

        completed = subprocess.run(
            [
                executable,
                "--background",
                "--python",
                str(runner_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        print(
            f"[JARVIS] Blender generation took "
            f"{time.monotonic() - blender_started:.1f}s",
            flush=True,
        )

    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "Blender returned a non-zero exit code."
        )

        raise RuntimeError(detail)

    if not output_path.is_file():
        raise RuntimeError(
            "Blender finished, but the .blend file was not created."
        )

    _launch_blender_gui(
        output_path
    )

    try:
        _refine_multiview_scene(
            brief,
            views,
        )
    except Exception as error:
        print(
            f"[JARVIS] multi-view visual refinement skipped: {error}",
            flush=True,
        )

    print(
        f"[JARVIS] multi-view Blender model saved to "
        f"{output_path}",
        flush=True,
    )

    return True


def _parse_visual_review(review):
    """Validate and parse the structured visual QA report."""
    text = (review or "").strip()

    if text == "NO_CRITICAL_MISMATCHES":
        return ()

    pattern = re.compile(
        r"PRIORITY:\s*(\d+)\s*"
        r"\nTYPE:\s*(geometry|proportion|repetition|material|camera|depth)\s*"
        r"\nLOCATION:\s*(.+?)\s*"
        r"\nPROBLEM:\s*(.+?)\s*"
        r"\nACTION:\s*(.+?)"
        r"(?=\nPRIORITY:|\Z)",
        re.IGNORECASE | re.DOTALL,
    )

    matches = list(pattern.finditer(text))

    if not matches:
        raise ValueError(
            "Visual QA report did not contain any valid priority blocks."
        )

    issues = []

    for match in matches:
        priority = int(match.group(1))
        issue_type = match.group(2).casefold()
        location = match.group(3).strip()
        problem = match.group(4).strip()
        action = match.group(5).strip()

        if not location or not problem or not action:
            raise ValueError(
                f"Visual QA priority {priority} contains an empty field."
            )

        issues.append(
            {
                "priority": priority,
                "type": issue_type,
                "location": location,
                "problem": problem,
                "action": action,
            }
        )

    priorities = [
        issue["priority"]
        for issue in issues
    ]

    expected = list(
        range(
            1,
            len(priorities) + 1,
        )
    )

    if priorities != expected:
        raise ValueError(
            "Visual QA priorities must be consecutive starting at 1."
        )

    if len(issues) > 5:
        raise ValueError(
            "Visual QA returned more than five priorities."
        )

    covered_length = sum(
        match.end() - match.start()
        for match in matches
    )

    if covered_length < len(text) * 0.9:
        raise ValueError(
            "Visual QA report contains unexpected unstructured text."
        )

    return tuple(issues)


def _review_preview(reference_bytes, preview_bytes, brief):
    """Ask vision to compare the reference against the current render."""
    comparison = _comparison_image(
        reference_bytes,
        preview_bytes,
    )

    base_prompt = (
        _REVIEW_PROMPT
        + "\n\nORIGINAL MODELLING BRIEF:\n"
        + brief
    )

    prompt = base_prompt
    last_error = None

    for attempt in range(2):
        started = time.monotonic()

        review = vision(
            prompt,
            comparison,
            mime="image/png",
            max_tokens=1200,
            reasoning_effort="low",
        )

        print(
            f"[JARVIS] visual QA took "
            f"{time.monotonic() - started:.1f}s",
            flush=True,
        )

        review = (review or "").strip()

        try:
            issues = _parse_visual_review(review)

        except ValueError as error:
            last_error = error

            if attempt == 1:
                raise RuntimeError(
                    "Visual QA returned an invalid report twice: "
                    f"{error}"
                ) from error

            print(
                "[JARVIS] visual QA report was invalid; "
                "requesting a corrected report.",
                flush=True,
            )

            prompt = (
                base_prompt
                + "\n\n"
                "Your previous visual QA report was invalid because: "
                f"{error}\n\n"
                "Return the COMPLETE report again.\n"
                "Use ONLY the required PRIORITY / TYPE / LOCATION / "
                "PROBLEM / ACTION format.\n"
                "Do not leave any field blank.\n"
                "Do not add commentary before or after the report."
            )
            continue

        print(
            "[JARVIS] visual QA report:\n"
            + (
                review
                if review
                else "NO_CRITICAL_MISMATCHES"
            ),
            flush=True,
        )

        return (
            review,
            issues,
        )

    raise RuntimeError(
        "Visual QA could not produce a valid report."
    ) from last_error


def _generate_refinement_script(
    brief,
    scene_text,
    review,
    comparison_image,
):
    """Generate and, when necessary, repair targeted bpy changes."""
    issues = _parse_visual_review(review)

    checklist = "\n".join(
        (
            f"PRIORITY {issue['priority']}: "
            f"{issue['type']} — "
            f"{issue['location']} — "
            f"{issue['action']}"
        )
        for issue in issues
    )

    user_content = (
        "ORIGINAL MODELLING BRIEF:\n"
        + brief
        + "\n\nCURRENT BLENDER SCENE:\n"
        + scene_text
        + "\n\nVISUAL QA REPORT:\n"
        + review
        + "\n\nMANDATORY FIX CHECKLIST:\n"
        + checklist
    )

    prompt = _REFINEMENT_PROMPT

    response = vision_chat(
        [
            {
                "role": "system",
                "content": prompt,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        comparison_image,
        mime="image/png",
        max_tokens=12000,
        reasoning_effort="medium",
    )

    script = _clean_generated_script(response)

    if not script:
        raise ValueError(
            "Blender visual refinement did not return "
            "a usable Python script."
        )

    try:
        _validate_script(script)

    except SyntaxError as error:
        print(
            "[JARVIS] initial Blender refinement failed syntax "
            "validation; repairing the existing refinement script.",
            flush=True,
        )

        repair_prompt = (
            "You are repairing an EXISTING Blender Python refinement script.\n\n"
            "The script was generated to improve an existing Blender 5.2 "
            "scene, but it contains invalid Python syntax.\n\n"
            f"Python syntax error: {error}\n\n"
            "Repair the EXISTING script rather than redesigning the scene.\n"
            "Preserve all intended modelling changes.\n"
            "Do not remove correct changes merely to make the script shorter.\n"
            "Return the COMPLETE corrected Python source.\n"
            "Return ONLY Python source.\n"
            "Do not use Markdown fences.\n"
            "Use only bpy, math, and mathutils.\n"
            "Do not import any other module.\n"
            "Make sure every parenthesis, bracket, quote, function, loop, "
            "conditional, and block is completely closed.\n\n"
            "ORIGINAL MODELLING BRIEF:\n"
            + brief
            + "\n\n"
            "MANDATORY FIX CHECKLIST:\n"
            + checklist
            + "\n\n"
            "BROKEN REFINEMENT SCRIPT:\n"
            + script
        )

        repaired = vision_chat(
            [
                {
                    "role": "system",
                    "content": repair_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Repair this exact script and return only the "
                        "complete corrected Python source."
                    ),
                },
            ],
            comparison_image,
            mime="image/png",
            max_tokens=12000,
            reasoning_effort="low",
        )

        script = _clean_generated_script(repaired)

        if not script:
            raise ValueError(
                "Blender refinement repair did not return "
                "a usable Python script."
            )

        try:
            _validate_script(script)

        except SyntaxError as repair_error:
            raise ValueError(
                "Generated Blender refinement remained syntactically "
                f"invalid after repair: {repair_error}"
            ) from repair_error

        except ValueError:
            raise

    except ValueError as error:
        message = str(error)

        if (
            "Generated Blender script imports disallowed module:"
            not in message
        ):
            # Security-policy violations other than imports are never
            # repaired automatically.
            raise

        print(
            "[JARVIS] Blender refinement used an unapproved import; "
            "repairing the existing refinement script.",
            flush=True,
        )

        repair_prompt = (
            "You are repairing an EXISTING Blender Python refinement script.\n\n"
            "The refinement script is otherwise intended to correct an "
            "existing scene, but it imports a module outside JARVIS's "
            "approved Blender import set.\n\n"
            f"Validation error: {message}\n\n"
            "Repair the EXISTING script. Do not redesign the scene.\n"
            "Preserve every intended visual correction in the script.\n"
            "Remove or replace only the unapproved import and the code "
            "that depends on it.\n"
            "Use only these approved imports: bpy, bmesh, math, "
            "mathutils, random.\n"
            "Do not import any other module.\n"
            "Do not use regular expressions or external libraries.\n"
            "Return ONLY the complete corrected Python source.\n"
            "Do not use Markdown fences.\n\n"
            "ORIGINAL MODELLING BRIEF:\n"
            + brief
            + "\n\n"
            "MANDATORY FIX CHECKLIST:\n"
            + checklist
            + "\n\n"
            "BROKEN REFINEMENT SCRIPT:\n"
            + script
        )

        repaired = vision_chat(
            [
                {
                    "role": "system",
                    "content": repair_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Repair this exact script and return only the "
                        "complete corrected Python source."
                    ),
                },
            ],
            comparison_image,
            mime="image/png",
            max_tokens=12000,
            reasoning_effort="low",
        )

        script = _clean_generated_script(repaired)

        if not script:
            raise ValueError(
                "Blender refinement import repair did not return "
                "a usable Python script."
            )

        try:
            _validate_script(script)

        except SyntaxError as repair_error:
            raise ValueError(
                "Generated Blender refinement remained syntactically "
                f"invalid after import repair: {repair_error}"
            ) from repair_error

        except ValueError as repair_error:
            raise ValueError(
                "Generated Blender refinement still violates the "
                f"import/security policy after repair: {repair_error}"
            ) from repair_error

    return script


def _refine_current_scene(brief):
    """Iteratively inspect and improve the live Blender model."""
    reference_bytes = images.current_original_bytes()

    if not reference_bytes:
        print(
            "[JARVIS] no reference image available for visual refinement",
            flush=True,
        )
        return

    for pass_number in range(
        1,
        _REFINEMENT_PASSES + 1,
    ):
        print(
            f"[JARVIS] visual refinement pass "
            f"{pass_number}/{_REFINEMENT_PASSES}",
            flush=True,
        )

        preview = _bridge_render_preview()

        review, issues = _review_preview(
            reference_bytes,
            preview["bytes"],
            brief,
        )

        if not review:
            print(
                "[JARVIS] visual QA returned no report; "
                "stopping refinement.",
                flush=True,
            )
            return

        if review == "NO_CRITICAL_MISMATCHES":
            print(
                "[JARVIS] visual QA found no critical mismatches.",
                flush=True,
            )
            return

        context = modeling_context()

        scene_text = json.dumps(
            context,
            indent=2,
        )

        comparison = _comparison_image(
            reference_bytes,
            preview["bytes"],
        )

        script = _generate_refinement_script(
            brief,
            scene_text,
            review,
            comparison,
        )

        _bridge_execute(script)

        print(
            f"[JARVIS] refinement pass "
            f"{pass_number} applied.",
            flush=True,
        )

    _bridge_render_preview()

    print(
        "[JARVIS] final visual refinement render completed.",
        flush=True,
    )


def _generate_modification_script(request, scene_text, reference_analysis=None):
    """Ask the configured LLM for valid, safe bpy code for a scene change."""
    prompt_parts = [
        "USER REQUEST:\n"
        + request,
        "CURRENT BLENDER SCENE:\n"
        + scene_text,
    ]

    if reference_analysis:
        prompt_parts.append(
            "REFERENCE IMAGE ANALYSIS:\n"
            + reference_analysis
        )

    user_content = "\n\n".join(prompt_parts)
    prompt = _MODIFY_PROMPT

    for attempt in range(2):
        response = chat(
            [
                {
                    "role": "system",
                    "content": prompt,
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            temperature=0,
            max_tokens=4000,
            reasoning_effort="low",
        )

        script = _clean_generated_script(response)

        if not script:
            if attempt == 1:
                raise ValueError(
                    "Blender modification did not return a usable Python script."
                )

            prompt = (
                _MODIFY_PROMPT
                + "\n\nYour previous response was empty. "
                "Return the complete Blender Python script."
            )
            continue

        try:
            _validate_script(script)

        except SyntaxError as error:
            if attempt == 1:
                raise ValueError(
                    "Generated Blender modification has invalid Python syntax: "
                    f"{error}"
                ) from error

            prompt = (
                _MODIFY_PROMPT
                + "\n\nYour previous Blender modification failed to parse "
                f"as Python: {error}. Rewrite the ENTIRE modification script "
                "correctly. Return only valid Python source. "
                "Do not use markdown fences."
            )
            continue

        except ValueError as error:
            message = str(error)

            if (
                "Generated Blender script imports disallowed module:"
                not in message
            ):
                # Security-policy violations other than imports are never
                # repaired automatically.
                raise

            if attempt == 1:
                raise ValueError(
                    "Generated Blender modification still uses a "
                    f"disallowed import after repair: {message}"
                )

            prompt = (
                _MODIFY_PROMPT
                + "\n\nYour previous modification used an unapproved "
                f"import: {message}\n\n"
                "Repair the EXISTING modification script rather than "
                "redesigning it.\n"
                "Use only these approved imports: bpy, bmesh, math, "
                "mathutils, random.\n"
                "Do not import any other module.\n"
                "Return the COMPLETE corrected Python source.\n"
                "Return only Python source with no Markdown fences."
            )
            continue

        return script

    raise ValueError(
        "Could not generate a valid Blender modification script."
    )


def modify_current_scene(request):
    """Interpret and apply a natural-language modification to Blender."""
    context = modeling_context()
    scene_text = json.dumps(
        context,
        indent=2,
    )

    restore_state = context.get("visibility_restore") or {}

    if _is_restore_request(request):
        if not restore_state:
            raise RuntimeError(
                "There is no saved Blender visibility state to restore."
            )

        script = (
            "import bpy\n"
            f"restore_state = {restore_state!r}\n"
            "for name, visible in restore_state.items():\n"
            "    obj = bpy.context.scene.objects.get(name)\n"
            "    if obj is not None:\n"
            "        obj.hide_set(not visible)\n"
        )

        result = _bridge_execute(
            script,
            restore_visibility=True,
        )

        if not result.get("ok"):
            raise RuntimeError(
                result.get(
                    "error",
                    "Blender rejected the visibility restore.",
                )
            )

        return True

    image_bytes = images.current_original_bytes()
    reference_analysis = None

    if image_bytes:
        reference_analysis = analyse_current_reference()

    script = _generate_modification_script(
        request,
        scene_text,
        reference_analysis,
    )

    result = _bridge_execute(script)

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "error",
                "Blender rejected the modification.",
            )
        )

    return True


def _safe_stem(value):
    """Turn an image title into a safe Blender filename stem."""
    stem = re.sub(r"[^\w\s-]", "", (value or "")).strip()
    stem = re.sub(r"\s+", "-", stem)

    return stem or "model"


def _validate_script(source):
    """Reject generated code that attempts unrelated system access."""
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]

                if root not in _ALLOWED_IMPORTS:
                    raise ValueError(
                        f"Generated Blender script imports disallowed "
                        f"module: {alias.name}"
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError(
                    "Generated Blender script may not use relative imports."
                )

            module = node.module or ""
            root = module.split(".", 1)[0]

            if root not in _ALLOWED_IMPORTS:
                raise ValueError(
                    f"Generated Blender script imports disallowed "
                    f"module: {module}"
                )

        elif isinstance(node, ast.Call):
            chain = _attribute_chain(node.func)

            if chain in _FORBIDDEN_BPY_OPERATIONS:
                raise ValueError(
                    "Generated Blender script uses a JARVIS-owned "
                    f"Blender operation: {'.'.join(chain)}"
                )

            if isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_CALLS:
                    raise ValueError(
                        f"Generated Blender script uses disallowed call: "
                        f"{node.func.id}"
                    )

    return True


def analyse_current_reference(request=None):
    """Analyse the current JARVIS image, reusing cached analysis when possible."""
    image_bytes = images.current_original_bytes()

    if not image_bytes:
        return None

    analysis = _load_cached_reference_analysis(image_bytes)

    if analysis:
        print("[JARVIS] using cached reference analysis.", flush=True)

    else:
        print("[JARVIS] analysing reference image...", flush=True)

        mime = images.current_mime() or "image/png"

        analysis = vision(
            _REFERENCE_PROMPT,
            image_bytes,
            mime=mime,
            max_tokens=3000,
        )

        if not analysis:
            return None

        _save_cached_reference_analysis(
            image_bytes,
            analysis,
        )

    request_text = (request or "").strip()

    if request_text:
        return (
            analysis
            + f"\n\nUser request: {request_text}"
        )

    return analysis


def _clean_generated_script(source):
    """Remove an optional outer Markdown code fence without changing code."""
    script = (source or "").strip()

    if not (
        script.startswith("```")
        and script.endswith("```")
    ):
        return script

    lines = script.splitlines()

    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]

    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines).strip()


def _generate_scene_script(
    brief,
    reference_image=None,
):
    """Ask the configured LLM for valid, safe bpy code."""
    prompt = _SCRIPT_PROMPT

    print(
        "[JARVIS] generating Blender scene script...",
        flush=True,
    )

    started = time.monotonic()

    generation_messages = [
        {
            "role": "system",
            "content": prompt,
        },
        {
            "role": "user",
            "content": brief,
        },
    ]

    if reference_image is not None:
        generation_messages[1]["content"] = (
            brief
            + "\n\n"
            "REFERENCE-GROUNDING RULE:\n"
            "The supplied reference views are the primary visual authority.\n"
            "Construct the model from what is actually visible in those views.\n"
            "Do not rely on remembered or canonical appearance of the subject.\n"
            "Cross-check the supplied views and make one consistent 3D model.\n"
        )

        response = vision_chat(
            generation_messages,
            reference_image,
            mime="image/png",
            max_tokens=12000,
            reasoning_effort="low",
        )

    else:
        response = chat(
            generation_messages,
            temperature=0,
            max_tokens=12000,
            reasoning_effort="low",
        )

    generation_time = time.monotonic() - started

    print(
        f"[JARVIS] Blender scene script received "
        f"({generation_time:.1f}s).",
        flush=True,
    )

    script = _clean_generated_script(response)

    if not script:
        raise ValueError(
            "Blender model did not return a usable Python script."
        )

    try:
        _validate_script(script)

    except SyntaxError as error:
        print(
            "[JARVIS] initial Blender script failed syntax validation; "
            "repairing the existing script.",
            flush=True,
        )

        repair_prompt = (
            "You are repairing an EXISTING Blender Python script.\n\n"
            "The script below was generated for a Blender 5.2 scene, "
            "but it contains invalid Python syntax.\n\n"
            f"Python syntax error: {error}\n\n"
            "Repair the existing script rather than redesigning the scene.\n"
            "Preserve all existing modelling work and intended geometry.\n"
            "Do not simplify or remove correct geometry unless required "
            "to repair the syntax.\n"
            "Return the COMPLETE corrected Python source.\n"
            "Do not return explanations.\n"
            "Do not use Markdown fences.\n"
            "Use only bpy, math, mathutils, and random if imports are needed.\n"
            "Do not import any other module.\n"
            "Make sure every parenthesis, bracket, quote, function, loop, "
            "conditional, and block is completely closed before returning.\n\n"
            "ORIGINAL MODELLING BRIEF:\n"
            + brief
            + "\n\nBROKEN BLENDER SCRIPT:\n"
            + script
        )

        repair_started = time.monotonic()

        repaired = chat(
            [
                {
                    "role": "system",
                    "content": repair_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Repair the script above and return only the "
                        "complete corrected Python source."
                    ),
                },
            ],
            temperature=0,
            max_tokens=12000,
            reasoning_effort="low",
        )

        print(
            f"[JARVIS] Blender script repair received "
            f"({time.monotonic() - repair_started:.1f}s).",
            flush=True,
        )

        script = _clean_generated_script(repaired)

        if not script:
            raise ValueError(
                "Blender script repair did not return a usable Python script."
            )

        try:
            _validate_script(script)

        except SyntaxError as repair_error:
            raise ValueError(
                "Generated Blender script remained syntactically invalid "
                f"after repair: {repair_error}"
            ) from repair_error

        except ValueError:
            raise

    except ValueError as error:
        message = str(error)

        if (
            "Generated Blender script imports disallowed module:"
            not in message
        ):
            # Security-policy violations other than imports are never
            # repaired automatically.
            raise

        print(
            "[JARVIS] Blender scene script used an unapproved import; "
            "repairing the existing script.",
            flush=True,
        )

        repair_prompt = (
            "You are repairing an EXISTING Blender Python script.\n\n"
            "The script is otherwise valid, but it imports a module that "
            "is not in JARVIS's approved Blender import set.\n\n"
            f"Validation error: {message}\n\n"
            "Repair the EXISTING script. Do not redesign the scene.\n"
            "Preserve the intended geometry and modelling work.\n"
            "Remove or replace the unapproved import and any code that "
            "depends on it.\n"
            "Use only these approved imports: bpy, bmesh, math, "
            "mathutils, random.\n"
            "Do not import any other module.\n"
            "Do not use regular expressions or external libraries.\n"
            "Return ONLY the complete corrected Python source.\n"
            "Do not use Markdown fences.\n\n"
            "ORIGINAL MODELLING BRIEF:\n"
            + brief
            + "\n\n"
            "SCRIPT TO REPAIR:\n"
            + script
        )

        repaired = chat(
            [
                {
                    "role": "system",
                    "content": repair_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Repair this exact script and return only the "
                        "complete corrected Python source."
                    ),
                },
            ],
            temperature=0,
            max_tokens=12000,
            reasoning_effort="low",
        )

        script = _clean_generated_script(repaired)

        if not script:
            raise ValueError(
                "Blender import repair did not return a usable Python script."
            )

        try:
            _validate_script(script)

        except SyntaxError as repair_error:
            raise ValueError(
                "Generated Blender script remained syntactically invalid "
                f"after import repair: {repair_error}"
            ) from repair_error

        except ValueError as repair_error:
            raise ValueError(
                "Generated Blender script still violates the import/security "
                f"policy after repair: {repair_error}"
            ) from repair_error

    print(
        "[JARVIS] Blender scene script accepted.",
        flush=True,
    )

    return script


def _write_runner(scene_script, output_path):
    """Wrap the generated scene code with JARVIS-owned save logic."""
    return (
        f"{scene_script.rstrip()}\n\n"
        "import bpy\n"
        f"bpy.ops.wm.save_as_mainfile(filepath={str(output_path)!r})\n"
    )


def open_external_glb(
    glb_path,
    subject="model",
):
    """
    Import an externally generated GLB into Blender, save it as a .blend,
    render it, and leave the same Blender GUI process open.

    This is intentionally generic: the subject only controls the output
    filename. No model-specific logic lives here.
    """
    glb_path = Path(glb_path)

    if not glb_path.is_file():
        raise RuntimeError(
            f"Generated GLB was not found: {glb_path}"
        )

    executable = _find_blender()

    if not executable:
        raise RuntimeError(
            "Blender could not be found. "
            "Set BLENDER_EXECUTABLE if needed."
        )

    models_dir = Path(files.root()) / "models"
    models_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    stem = _safe_stem(subject)
    blend_path = models_dir / f"{stem}-tripo.blend"
    render_path = models_dir / f"{stem}-tripo.png"
    status_path = models_dir / f"{stem}-tripo.status"

    try:
        status_path.unlink()
    except FileNotFoundError:
        pass

    runner = f"""
import bpy
import traceback
from mathutils import Vector

GLB_PATH = {str(glb_path)!r}
BLEND_PATH = {str(blend_path)!r}
RENDER_PATH = {str(render_path)!r}
STATUS_PATH = {str(status_path)!r}

def write_status(value):
    try:
        with open(STATUS_PATH, "w", encoding="utf-8") as handle:
            handle.write(value)
    except Exception as error:
        print("[JARVIS] could not write Tripo status:", error)

try:
    print("[JARVIS] preparing Blender for Tripo model...", flush=True)

    # Remove Blender's default objects without requiring background mode.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    print("[JARVIS] importing Tripo GLB...", flush=True)

    result = bpy.ops.import_scene.gltf(
        filepath=GLB_PATH
    )

    print(
        "[JARVIS] Tripo GLB import result:",
        result,
        flush=True,
    )

    meshes = [
        obj
        for obj in bpy.context.scene.objects
        if obj.type == "MESH"
    ]

    if not meshes:
        raise RuntimeError(
            "Tripo GLB imported but contains no mesh objects."
        )

    corners = []

    for obj in meshes:
        corners.extend(
            obj.matrix_world @ Vector(corner)
            for corner in obj.bound_box
        )

    minimum = Vector((
        min(point.x for point in corners),
        min(point.y for point in corners),
        min(point.z for point in corners),
    ))

    maximum = Vector((
        max(point.x for point in corners),
        max(point.y for point in corners),
        max(point.z for point in corners),
    ))

    center = (minimum + maximum) * 0.5

    size = max(
        maximum.x - minimum.x,
        maximum.y - minimum.y,
        maximum.z - minimum.z,
        1.0,
    )

    scene = bpy.context.scene

    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 900
    scene.render.resolution_y = 900
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.filepath = RENDER_PATH
    scene.world.color = (0.055, 0.055, 0.055)

    camera_data = bpy.data.cameras.new(
        "JARVIS Camera"
    )

    camera = bpy.data.objects.new(
        "JARVIS Camera",
        camera_data,
    )

    scene.collection.objects.link(camera)
    scene.camera = camera

    camera.location = (
        center.x,
        center.y - size * 2.4,
        center.z + size * 0.12,
    )

    camera.rotation_euler = (
        center - camera.location
    ).to_track_quat(
        "-Z",
        "Y",
    ).to_euler()

    camera.data.lens = 55

    def add_area(name, location, energy, size_value):
        data = bpy.data.lights.new(
            name,
            type="AREA",
        )

        data.energy = energy
        data.shape = "DISK"
        data.size = size_value

        light = bpy.data.objects.new(
            name,
            data,
        )

        scene.collection.objects.link(light)

        light.location = location
        light.rotation_euler = (
            center - light.location
        ).to_track_quat(
            "-Z",
            "Y",
        ).to_euler()

        return light

    add_area(
        "JARVIS Key",
        (
            center.x - size,
            center.y - size,
            center.z + size,
        ),
        1400,
        size,
    )

    add_area(
        "JARVIS Fill",
        (
            center.x + size,
            center.y - size * 0.5,
            center.z + size * 0.4,
        ),
        800,
        size * 0.8,
    )

    add_area(
        "JARVIS Rim",
        (
            center.x,
            center.y + size,
            center.z + size * 0.8,
        ),
        1100,
        size * 0.7,
    )

    print(
        "[JARVIS] saving Blender file...",
        flush=True,
    )

    bpy.ops.wm.save_as_mainfile(
        filepath=BLEND_PATH
    )

    print(
        "[JARVIS] Blender file saved.",
        flush=True,
    )

    print(
        "[JARVIS] rendering Tripo model...",
        flush=True,
    )

    bpy.ops.render.render(
        write_still=True
    )

    print(
        "[JARVIS] Blender render completed.",
        flush=True,
    )

    # Put the 3D viewport into camera/rendered mode when the window is ready.
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "CAMERA"
            area.spaces.active.shading.type = "RENDERED"

    def show_render():
        try:
            bpy.ops.render.view_show(
                "INVOKE_DEFAULT"
            )
        except Exception as error:
            print(
                "[JARVIS] render window could not be opened:",
                error,
                flush=True,
            )

        return None

    bpy.app.timers.register(
        show_render,
        first_interval=0.75,
    )

    write_status("READY")

    print(
        "[JARVIS] Tripo model ready in Blender.",
        flush=True,
    )

except Exception as error:
    traceback_text = traceback.format_exc()

    print(
        "[JARVIS] Tripo Blender handoff failed:",
        traceback_text,
        flush=True,
    )

    write_status(
        "ERROR\\n"
        + str(error)
        + "\\n"
        + traceback_text
    )

    raise
""".strip()

    print(
        f"[JARVIS] opening Blender for Tripo model: {glb_path}",
        flush=True,
    )

    started = time.monotonic()

    process = subprocess.Popen(
        [
            executable,
            "--python-expr",
            f"exec({runner!r})",
        ],
        stdout=None,
        stderr=None,
    )

    print(
        f"[JARVIS] Blender GUI process started "
        f"(pid={process.pid}).",
        flush=True,
    )

    deadline = time.monotonic() + 60.0

    while time.monotonic() < deadline:
        if status_path.is_file():
            status = status_path.read_text(
                encoding="utf-8",
            )

            if status == "READY":
                if not blend_path.is_file():
                    raise RuntimeError(
                        "Blender reported success but the .blend "
                        f"file does not exist: {blend_path}"
                    )

                if not render_path.is_file():
                    raise RuntimeError(
                        "Blender reported success but the render "
                        f"does not exist: {render_path}"
                    )

                print(
                    f"[JARVIS] Tripo Blender handoff completed "
                    f"in {time.monotonic() - started:.1f}s.",
                    flush=True,
                )

                return True

            if status.startswith("ERROR"):
                raise RuntimeError(
                    "Blender failed during the Tripo handoff:\\n\\n"
                    + status
                )

        if process.poll() is not None:
            raise RuntimeError(
                "Blender closed before the Tripo handoff completed."
            )

        time.sleep(0.25)

    raise RuntimeError(
        "Timed out waiting for Blender to finish the Tripo handoff."
    )


def create_from_reference(request=None):
    """Create and open a Blender model from the current reference image."""
    if _is_multiview_request(request):
        return _create_from_reference_set(request)

    if not images.has_image():
        return False

    executable = _find_blender()

    if not executable:
        raise RuntimeError(
            "Blender could not be found. Set BLENDER_EXECUTABLE if needed."
        )

    analysis_started = time.monotonic()

    brief = analyse_current_reference(request)

    print(
        f"[JARVIS] reference analysis took "
        f"{time.monotonic() - analysis_started:.1f}s",
        flush=True,
    )

    if not brief:
        raise RuntimeError(
            "The reference image could not be analysed."
        )

    script_started = time.monotonic()
    scene_script = _generate_scene_script(brief)
    print(
        f"[JARVIS] scene script generation took "
        f"{time.monotonic() - script_started:.1f}s",
        flush=True,
    )

    models_dir = Path(files.root()) / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    stem = _safe_stem(images.current_title())
    output_path = models_dir / f"{stem}.blend"

    with tempfile.TemporaryDirectory(
        prefix="jarvis-blender-"
    ) as temporary:

        runner_path = Path(temporary) / "scene.py"
        runner_path.write_text(
            _write_runner(scene_script, output_path),
            encoding="utf-8",
        )
        blender_started = time.monotonic()
        completed = subprocess.run(
            [
                executable,
                "--background",
                "--python",
                str(runner_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        print(
            f"[JARVIS] Blender generation took "
            f"{time.monotonic() - blender_started:.1f}s",
            flush=True,
        )

    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "Blender returned a non-zero exit code."
        )

        raise RuntimeError(detail)

    if not output_path.is_file():
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        detail_parts = [
            f"Expected output: {output_path}",
        ]

        if stdout:
            detail_parts.append(
                f"BLENDER STDOUT:\n{stdout}"
            )

        if stderr:
            detail_parts.append(
                f"BLENDER STDERR:\n{stderr}"
            )

        raise RuntimeError(
            "Blender finished, but the .blend file was not created.\n\n"
            + "\n\n".join(detail_parts)
        )

    _launch_blender_gui(output_path)

    try:
        _refine_current_scene(brief)
    except Exception as error:
        print(
            f"[JARVIS] visual refinement skipped: {error}",
            flush=True,
        )

    print(
        f"[JARVIS] Blender model saved to {output_path}"
    )

    return True
