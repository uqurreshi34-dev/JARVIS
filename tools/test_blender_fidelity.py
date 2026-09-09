"""Regression tests for subject-aware briefs and measured refinement.

Runs entirely offline. No LLM call, no Blender, no network.
"""

import io
import sys
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw


# Running this file directly puts tools/ on sys.path rather than the JARVIS
# project root, so add the repository root before importing project modules.
ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import blender, blender_fidelity  # noqa: E402


blender_fidelity.install()


def _png(width, height, box, fill=(30, 30, 30)):
    """Build a simple subject-on-white-background test image."""
    image = Image.new("RGB", (width, height), "white")

    ImageDraw.Draw(image).rectangle(
        box,
        fill=fill,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    return buffer.getvalue()


def _check_prompts(failures):
    if "CLASS:" not in blender._REFERENCE_PROMPT:
        failures.append(
            "single-image brief is still architecture-only"
        )

    if "MEASUREMENTS" not in blender._REFERENCE_PROMPT:
        failures.append(
            "single-image brief does not request normalised measurements"
        )

    if "CLASS:" not in blender._MULTIVIEW_PROMPT:
        failures.append(
            "multi-view brief is still architecture-only"
        )

    if "SUBJECT-CLASS CONSTRUCTION RULES" not in blender._SCRIPT_PROMPT:
        failures.append(
            "script prompt did not receive subject-class rules"
        )

    if blender._REFERENCE_CACHE_VERSION == "1":
        failures.append(
            "reference cache version was not bumped; stale briefs will "
            "be reused"
        )


def _check_effort(failures):
    """The wrapper must survive install and must upgrade 'low'."""
    for name in ("vision", "vision_chat", "chat"):
        function = getattr(blender, name)

        if not getattr(function, "_jarvis_effort_patched", False):
            failures.append(
                f"blender.{name} was not wrapped for reasoning effort"
            )

    seen = {}

    def record(*args, **kwargs):
        seen.update(kwargs)
        return "ok"

    wrapped = blender_fidelity._with_effort("test", record)
    wrapped("prompt", reasoning_effort="low", max_tokens=4000)

    if seen.get("max_tokens") != 4000:
        failures.append(
            "a small analysis budget was scaled up and will be rejected"
        )

    seen.clear()
    wrapped("prompt", reasoning_effort="low", max_tokens=12000)

    if seen.get("max_tokens") == 12000:
        failures.append(
            "a large generation budget was not scaled for thinking tokens"
        )

    if seen.get("reasoning_effort") == "low":
        failures.append(
            "the effort wrapper did not raise a 'low' call"
        )

    seen.clear()
    wrapped("prompt", reasoning_effort="high")

    if seen.get("reasoning_effort") != "high":
        failures.append(
            "the effort wrapper overwrote an explicitly high call"
        )


def _check_score(failures):
    """The regression signal must notice proportion, not just outline."""
    tall = blender_fidelity._score(
        {"iou": 1.0, "aspect_ratio": 1.09}
    )

    stretched = blender_fidelity._score(
        {"iou": 1.0, "aspect_ratio": 15.54}
    )

    if stretched >= tall:
        failures.append(
            "a wildly stretched render scored as well as a correct one; "
            "the score is still blind to proportion"
        )


def _check_image_grounding(failures):
    """The single-image path must show the reference to the code writer."""
    reference = _png(600, 600, (250, 100, 350, 500))

    with (
        patch.object(
            blender.images,
            "current_original_bytes",
            return_value=reference,
        ),
        patch.object(
            blender,
            "vision_chat",
            return_value="import bpy\n",
        ) as vision_mock,
        patch.object(
            blender,
            "chat",
            return_value="import bpy\n",
        ) as chat_mock,
    ):
        blender._generate_scene_script("CLASS: character\nbrief")

    if not vision_mock.called:
        failures.append(
            "single-image script generation still runs text-only"
        )

    if chat_mock.called:
        failures.append(
            "single-image script generation fell back to the text model"
        )


def _check_metrics(failures):
    reference = _png(600, 600, (250, 100, 350, 500))
    identical = _png(600, 600, (250, 100, 350, 500))
    too_wide = _png(600, 600, (150, 100, 450, 500))
    full_frame = _png(600, 600, (0, 0, 599, 599))

    match = blender_fidelity._metrics(reference, identical)

    if not match or match["iou"] < 0.98:
        failures.append(
            "identical silhouettes did not score as a match"
        )

    wide = blender_fidelity._metrics(reference, too_wide)

    if not wide or wide["aspect_ratio"] < 2.0:
        failures.append(
            "an obviously too-wide render was not measured as too wide"
        )

    elif "too WIDE" not in blender_fidelity._metrics_block(wide):
        failures.append(
            "the measurement block did not report the width error"
        )

    if blender_fidelity._metrics(reference, full_frame) is not None:
        failures.append(
            "a failed background detection was scored instead of rejected"
        )


def _check_regression_stop(failures):
    """A pass that makes the model worse must stop the loop."""
    reference = _png(600, 600, (250, 100, 350, 500))

    renders = [
        {"bytes": _png(600, 600, (245, 100, 355, 500)), "mime": "image/png"},
        {"bytes": _png(600, 600, (100, 250, 500, 350)), "mime": "image/png"},
        {"bytes": _png(600, 600, (100, 250, 500, 350)), "mime": "image/png"},
        {"bytes": _png(600, 600, (100, 250, 500, 350)), "mime": "image/png"},
    ]

    review = (
        "PRIORITY: 1\n"
        "TYPE: proportion\n"
        "LOCATION: torso\n"
        "PROBLEM: too wide\n"
        "ACTION: narrow it\n"
    )

    with (
        patch.object(
            blender.images,
            "current_original_bytes",
            return_value=reference,
        ),
        patch.object(
            blender,
            "_bridge_render_preview",
            side_effect=renders,
        ),
        patch.object(
            blender,
            "_review_preview",
            return_value=(review, [{"priority": 1}]),
        ),
        patch.object(
            blender,
            "modeling_context",
            return_value={"objects": []},
        ),
        patch.object(
            blender,
            "_comparison_image",
            return_value=b"",
        ),
        patch.object(
            blender,
            "_generate_refinement_script",
            return_value="import bpy\n",
        ) as script_mock,
        patch.object(
            blender,
            "_bridge_execute",
            return_value={"ok": True},
        ) as execute_mock,
    ):
        blender._refine_current_scene("CLASS: character\nbrief")

    if execute_mock.call_count == 0:
        failures.append(
            "refinement never applied a pass"
        )

    if execute_mock.call_count >= blender_fidelity._MAX_PASSES:
        failures.append(
            f"refinement ran {execute_mock.call_count} passes without "
            f"noticing the silhouette got worse"
        )

    if script_mock.called:
        report = script_mock.call_args.args[2]

        if "MEASURED SILHOUETTE ANALYSIS" not in report:
            failures.append(
                "measurements were not passed to the refinement model"
            )


def main():
    failures = []

    _check_prompts(failures)
    _check_effort(failures)
    _check_score(failures)
    _check_image_grounding(failures)
    _check_metrics(failures)
    _check_regression_stop(failures)

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: subject-aware briefs, image-grounded generation, and "
        "measured refinement all active."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
