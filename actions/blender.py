"""Create Blender scenes from JARVIS reference-image modelling briefs."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from providers import chat, vision

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
- Use only bpy, math, and mathutils.
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
- Use only bpy, math, mathutils, and random if imports are needed.
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

_ALLOWED_IMPORTS = frozenset({
    "bpy",
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
_BRIDGE_DIR = Path(files.root()) / ".blender-bridges"
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


def _bridge_execute(script):
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
                timeout=10,
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

        except ValueError:
            # Security-policy violations are never repaired by asking the
            # model to try again.
            raise

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


def _generate_scene_script(brief):
    """Ask the configured LLM for valid, safe bpy code."""
    prompt = _SCRIPT_PROMPT
    print("[JARVIS] generating Blender scene script...", flush=True)
    for attempt in range(2):
        response = chat(
            [
                {
                    "role": "system",
                    "content": prompt,
                },
                {
                    "role": "user",
                    "content": brief,
                },
            ],
            temperature=0,
            max_tokens=12000,
            reasoning_effort="medium",
        )
        print("[JARVIS] Blender scene script received.", flush=True)

        script = _clean_generated_script(response)

        # Claude may still wrap code in a markdown fence despite being told
        # not to. Remove only the outer fence; never alter the code itself.
        if script.startswith("```") and script.endswith("```"):
            lines = script.splitlines()

            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            script = "\n".join(lines).strip()

        if not script:
            if attempt == 1:
                raise ValueError(
                    "Blender model did not return a usable Python script."
                )

            prompt = (
                _SCRIPT_PROMPT
                + "\n\nYour previous response was empty. "
                "Return the complete Blender Python script."
            )
            continue

        try:
            _validate_script(script)

        except SyntaxError as error:
            if attempt == 1:
                raise ValueError(
                    "Generated Blender script has invalid Python syntax: "
                    f"{error}"
                ) from error

            prompt = (
                _SCRIPT_PROMPT
                + "\n\nYour previous Blender script failed to parse as "
                f"Python: {error}. Rewrite the ENTIRE script correctly. "
                "Return only valid Python source. "
                "Do not use markdown fences."
            )
            continue

        except ValueError:
            # Security-policy violations are not repaired by asking the
            # model to try again; reject them immediately.
            raise

        return script

    raise ValueError("Could not generate a valid Blender script.")


def _write_runner(scene_script, output_path):
    """Wrap the generated scene code with JARVIS-owned save logic."""
    return (
        f"{scene_script.rstrip()}\n\n"
        "import bpy\n"
        f"bpy.ops.wm.save_as_mainfile(filepath={str(output_path)!r})\n"
    )


def create_from_reference(request=None):
    """Create and open a Blender model from the current reference image."""
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

    print(f"[JARVIS] Blender model saved to {output_path}")

    return True
