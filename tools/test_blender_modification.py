"""Regression tests for live Blender scene modification."""

import sys
from pathlib import Path
from unittest.mock import patch


# Running this file directly puts tools/ on sys.path rather than the JARVIS
# project root, so add the repository root before importing project modules.
ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import blender  # noqa: E402


def main():
    failures = []

    live_context = {
        "blend_path": (
            r"C:\JARVIS\models\Mosque-2026-08-26-215515.blend"
        ),
        "scene": "Scene",
        "objects": [
            {"name": "Dome", "type": "MESH"},
            {"name": "Minaret", "type": "MESH"},
        ],
        "active_object": "Dome",
        "selected_objects": [],
    }

    # Simulate a normal LLM response that incorrectly wraps the Python
    # in a Markdown code fence.
    generated_script = (
        "```python\n"
        "import bpy\n"
        "\n"
        "obj = bpy.data.objects.get(\"Dome\")\n"
        "\n"
        "if obj is not None:\n"
        "    obj.scale *= 1.1\n"
        "```\n"
    )

    bridge_result = {
        "ok": True,
    }

    with (
        patch.object(
            blender,
            "modeling_context",
            return_value=live_context,
        ) as context_mock,
        patch.object(
            blender.images,
            "current_original_bytes",
            return_value=None,
        ) as image_mock,
        patch.object(
            blender,
            "vision",
        ) as vision_mock,
        patch.object(
            blender,
            "chat",
            return_value=generated_script,
        ) as chat_mock,
        patch.object(
            blender,
            "_bridge_execute",
            return_value=bridge_result,
        ) as bridge_mock,
    ):
        result = blender.modify_current_scene(
            "Make the dome bigger"
        )

    if not result:
        failures.append(
            "modify_current_scene returned failure without a reference image"
        )

    if not context_mock.called:
        failures.append(
            "live Blender modeling_context was not inspected"
        )

    if not image_mock.called:
        failures.append(
            "reference-image state was not checked"
        )

    if vision_mock.called:
        failures.append(
            "vision was called even though no reference image exists"
        )

    if not chat_mock.called:
        failures.append(
            "LLM was not called to interpret the live Blender scene"
        )

    if not bridge_mock.called:
        failures.append(
            "generated Blender script was not sent to the bridge"
        )

    if bridge_mock.call_count != 1:
        failures.append(
            f"expected exactly one bridge execution, got "
            f"{bridge_mock.call_count}"
        )

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: Blender modification works from live scene context "
        "without requiring a reference image."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
