"""Create Blender scenes from JARVIS reference-image modelling briefs."""

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import secrets
import time
import urllib.request

from providers import chat, vision

from actions import files, images


_REFERENCE_PROMPT = """
Analyse the supplied reference image as a 3D modelling reference.

Describe only what would matter to a Blender artist or procedural modeller:
- the main object's identity and overall form
- major components and their approximate proportions
- symmetry and repeated structures
- visible surfaces and likely materials
- important silhouette features
- useful modelling priorities
- important uncertainty caused by the viewing angle or missing sides

Do not write Blender Python.
Do not invent hidden geometry as fact.
Keep the result concise and practical.
"""


_SCRIPT_PROMPT = """
You are an expert procedural Blender artist.

Write a complete Blender 5.2 Python script using bpy that creates a
recognisable first-pass 3D model from the modelling brief below.

Requirements:
- Build the described object as actual editable Blender geometry.
- Prioritise recognisable silhouette and major forms over tiny details.
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
- Use only bpy, math, and mathutils if imports are needed.
- Every line must be valid Python 3 syntax.
- Do not invent modules or conditional-import expressions.

MODELLING BRIEF:
"""

_ALLOWED_IMPORTS = frozenset({
    "bpy",
    "math",
    "mathutils",
})


_FORBIDDEN_CALLS = frozenset({
    "exec",
    "eval",
    "open",
    "__import__",
    "compile",
})


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
_BRIDGE_SCRIPT = Path(__file__).with_name("blender_bridge.py")
_BRIDGE_STATE = Path(files.root()) / ".blender-bridge.json"


def _bridge_state():
    """Read the current JARVIS↔Blender connection details."""
    try:
        return json.loads(
            _BRIDGE_STATE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None


def _bridge_request(endpoint):
    """Call the authenticated local Blender bridge."""
    state = _bridge_state()

    if not state:
        return None

    request = urllib.request.Request(
        f"http://{_BRIDGE_HOST}:{int(state['port'])}{endpoint}",
        headers={
            "X-JARVIS-Token": state["token"],
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


def _launch_blender_gui(output_path):
    """Open a .blend and attach the JARVIS bridge."""
    try:
        _BRIDGE_STATE.unlink()
    except FileNotFoundError:
        pass

    token = secrets.token_urlsafe(32)

    environment = os.environ.copy()
    environment["JARVIS_BLENDER_TOKEN"] = token
    environment["JARVIS_BLENDER_PORT"] = "0"
    environment["JARVIS_BLENDER_STATE"] = str(_BRIDGE_STATE)

    process = subprocess.Popen(
        [
            _find_blender(),
            str(output_path),
            "--python",
            str(_BRIDGE_SCRIPT),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait only for the bridge to become ready. This is startup readiness,
    # not an arbitrary limit on Blender modelling work.
    while True:
        if running():
            return True

        if process.poll() is not None:
            raise RuntimeError(
                "Blender closed before JARVIS could connect to it."
            )

        time.sleep(0.2)


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
            if isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_CALLS:
                    raise ValueError(
                        f"Generated Blender script uses disallowed call: "
                        f"{node.func.id}"
                    )

    return True


def analyse_current_reference(request=None):
    """Analyse the current JARVIS image as a modelling reference."""
    image_bytes = images.current_original_bytes()

    if not image_bytes:
        return None

    mime = images.current_mime() or "image/png"
    request_text = (request or "").strip()

    prompt = _REFERENCE_PROMPT

    if request_text:
        prompt += f"\n\nUser request: {request_text}"

    return vision(
        prompt,
        image_bytes,
        mime=mime,
        max_tokens=3000,
    )


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
            max_tokens=6000,
            reasoning_effort="low",
        )
        print("[JARVIS] Blender scene script received.", flush=True)

        script = (response or "").strip()

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

    brief = analyse_current_reference(request)

    if not brief:
        raise RuntimeError(
            "The reference image could not be analysed."
        )

    scene_script = _generate_scene_script(brief)

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

    _launch_blender_gui(output_path)

    print(f"[JARVIS] Blender model saved to {output_path}")

    return True
