"""Subject-aware modelling briefs and measured visual refinement.

These patches do not replace the Blender pipeline. They tighten four points
that limit reconstruction fidelity on non-architectural subjects:

1. The reference/multi-view briefs assume the subject is a building.
2. The single-image path generates code from prose alone, never seeing the
   reference image.
3. Every modelling call runs at low reasoning effort.
4. The refinement loop applies a fixed number of passes with no measurement
   and no way to notice that a pass made the model worse.

Install after blender.py is importable, next to routing_guard.install():

    from actions import blender_fidelity
    blender_fidelity.install()
"""

import hashlib
import io
import json
import os
import re
import time

from PIL import Image, ImageChops, ImageOps

_installed = False

_EFFORT = os.environ.get("JARVIS_BLENDER_EFFORT", "medium")
_MASK_SIZE = 256
_MIN_EXTENT = 0.01
_MAX_EXTENT = 0.985
_MAX_PASSES = 2


# ---------------------------------------------------------------------------
# Subject-aware briefs
# ---------------------------------------------------------------------------

_CLASSIFY_BLOCK = """
FIRST, decide which class the subject belongs to and state it on the first
line as: CLASS: <one of architecture | character | vehicle | product | organic>

Then use the schema for that class and ignore the others.

--- character (people, figures, armoured suits, robots, mascots) ---
- IDENTITY: what the figure is, and its most recognisable single feature
- POSE: stance, weight distribution, limb positions, head direction
- PROPORTIONS: total height in head-lengths; shoulder width, waist, hip and
  limb lengths expressed as fractions of total height
- BODY MASSING: torso, pelvis, upper/lower arm, thigh/calf, hands, feet, head
  described as solids with rough cross-section shape (round, boxy, tapered)
- SILHOUETTE LANDMARKS: the 6-10 outline features that make it recognisable
  from a distance with all detail removed
- PANELLING: major surface divisions, seams, plates, armour segmentation
- FACE / HEAD: helmet or head shape, eye/visor shape and placement
- SIGNATURE ELEMENTS: emblems, glowing parts, attachments, asymmetries
- COLOUR BLOCKING: which body regions carry which colour, with rough coverage
  percentages

--- vehicle (cars, aircraft, ships, mechs) ---
- IDENTITY, STANCE, PROPORTIONS (length:width:height)
- BODY MASSING, GREENHOUSE/CABIN, WHEEL OR THRUSTER PLACEMENT
- SILHOUETTE LANDMARKS, PANELLING, SIGNATURE ELEMENTS, COLOUR BLOCKING

--- product (props, objects, furniture, devices) ---
- IDENTITY, PROPORTIONS, PRIMARY MASSES, PROFILE CURVES
- SILHOUETTE LANDMARKS, SURFACE DIVISIONS, SIGNATURE ELEMENTS, MATERIALS

--- organic (animals, plants, creatures) ---
- IDENTITY, POSE, PROPORTIONS, BODY MASSING, SILHOUETTE LANDMARKS
- SURFACE DETAIL, SIGNATURE ELEMENTS, COLOUR BLOCKING

--- architecture (buildings, structures) ---
- SUBJECT, MASSING, PROPORTIONS, FACADE, REPETITION, SYMMETRY, DEPTH, ROOF
- MATERIALS, SILHOUETTE LANDMARKS
"""

_MEASUREMENT_BLOCK = """
ALWAYS end with a MEASUREMENTS block, whatever the class.

Normalise so the subject's full height is exactly 1.0. Give every major part
as a JSON object on one line, in this exact form, with no extra prose:

MEASUREMENTS:
{"parts": [
  {"name": "torso", "centre": [x, y, z], "size": [w, d, h], "shape": "box|cylinder|sphere|cone|capsule"},
  ...
]}

x is left(-)/right(+), y is front(-)/back(+), z is bottom(0) to top(1.0).
Estimate honestly from the image. Approximate numbers are far more useful
than omitted ones. Include every part that affects the silhouette.
"""

_COMMON_TAIL = """
Be concrete about proportions, counts, spacing and relationships.
Do not invent hidden geometry as fact.
Do not write Blender Python.
"""

_REFERENCE_PROMPT = (
    "Return a compact but information-dense modelling specification for the "
    "subject in this image.\n"
    + _CLASSIFY_BLOCK
    + "\nAlso cover CAMERA: approximate viewpoint, elevation and framing.\n"
    + "\nAnd UNCERTAINTY: only details genuinely hidden or ambiguous.\n"
    + _MEASUREMENT_BLOCK
    + _COMMON_TAIL
)

_MULTIVIEW_PROMPT = (
    "You are analysing multiple reference photographs of the SAME physical "
    "subject for accurate 3D reconstruction.\n\n"
    "The contact sheet is labelled with the actual view names.\n"
    "Cross-reference ALL supplied views before making conclusions.\n"
    "Produce ONE unified modelling specification.\n"
    + _CLASSIFY_BLOCK
    + "\nUse front and rear views for longitudinal structure, and left and "
    "right views for lateral depth, where those views exist.\n"
    "Do not assume an unseen side is identical unless symmetry is strongly "
    "supported.\n"
    "State which geometry is confirmed by more than one view.\n"
    + _MEASUREMENT_BLOCK
    + _COMMON_TAIL
)

_SCRIPT_TAIL = """

SUBJECT-CLASS CONSTRUCTION RULES

Read the CLASS line in the brief and follow the matching rules.

If CLASS is character or organic:
- Build from the MEASUREMENTS block. Place each part at its stated centre and
  size, then refine. Do not freehand the proportions.
- Construct the figure as separate named body-part objects: Head, Torso,
  Pelvis, UpperArm_L, LowerArm_L, Hand_L, Thigh_L, Calf_L, Foot_L and their
  right-hand mirrors. Name them exactly so they stay editable.
- Use a Mirror modifier on an armature-centred origin rather than modelling
  both sides by hand.
- Use tapered, subdivided and bevelled primitives. A limb is a tapered
  cylinder or capsule with a Subdivision Surface modifier, never a raw cube.
- Get total height, head-length count and shoulder width right before adding
  any panelling or detail. Recognition comes from proportion and silhouette.
- Then add the SILHOUETTE LANDMARKS as real geometry. These matter more than
  surface detail.
- Then apply COLOUR BLOCKING with separate materials per region.
- Add emissive materials for any glowing signature elements.

If CLASS is vehicle or product:
- Build from MEASUREMENTS, then shape profiles with bevels, curves and
  Subdivision Surface. Keep panel gaps as real recessed geometry.

If CLASS is architecture:
- Prioritise roofline, entrances, windows, columns, stairs, balconies,
  pediments and cornices, using arrays and linked duplicates for repetition.

FOR EVERY CLASS
- Prioritise recognisable silhouette and correct proportion over small detail.
- Do not replace an important feature with an arbitrary cube or flat plane.
- Name every object meaningfully so later modification can find it.
- Preserve consistent scale between all elements.
- Do not stop after the primary masses; continue through the secondary
  features that affect recognition.
"""


# ---------------------------------------------------------------------------
# Silhouette measurement
# ---------------------------------------------------------------------------

def _flood_mask(image, tolerance):
    """Mark background by flooding inward from the frame border."""
    from PIL import ImageDraw

    work = image.copy()
    width, height = work.size
    sentinel = (255, 0, 255)

    seeds = (
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    )

    for seed in seeds:
        if work.getpixel(seed) != sentinel:
            ImageDraw.floodfill(
                work,
                seed,
                sentinel,
                thresh=tolerance,
            )

    flat = Image.new("RGB", work.size, sentinel)

    return ImageChops.difference(
        work,
        flat,
    ).convert("L").point(
        lambda value: 255 if value > 8 else 0
    )


def _flat_mask(image, tolerance):
    """Mark background by distance from the averaged corner colour."""
    width, height = image.size
    pixels = image.load()

    corners = [
        pixels[0, 0],
        pixels[width - 1, 0],
        pixels[0, height - 1],
        pixels[width - 1, height - 1],
    ]

    background = tuple(
        sum(corner[channel] for corner in corners) // 4
        for channel in range(3)
    )

    flat = Image.new("RGB", image.size, background)

    return ImageChops.difference(
        image,
        flat,
    ).convert("L").point(
        lambda value: 255 if value > tolerance else 0
    )


def _extent(mask):
    """Fraction of the frame the mask's bounding box covers."""
    bbox = mask.getbbox()

    if not bbox:
        return 0.0

    return (
        (bbox[2] - bbox[0])
        * (bbox[3] - bbox[1])
        / float(mask.width * mask.height)
    )


_SILHOUETTE_CACHE = {}


def _silhouette(image_bytes):
    """Estimate a normalised binary silhouette from a photo or render."""
    # The same reference views are measured on every pass; flood filling
    # them once is worth the few kilobytes.
    key = hashlib.sha256(image_bytes).hexdigest()

    if key in _SILHOUETTE_CACHE:
        return _SILHOUETTE_CACHE[key]

    with Image.open(io.BytesIO(image_bytes)) as source:
        if "A" in source.getbands():
            alpha = source.convert("RGBA").getchannel("A")
            mask = alpha.point(lambda value: 255 if value > 24 else 0)
        else:
            image = source.convert("RGB")
            image = ImageOps.contain(
                image,
                (512, 512),
                method=Image.Resampling.LANCZOS,
            )

            # Try several tolerances and keep the one that isolates the
            # subject most tightly. Taking the first merely-acceptable
            # result risks scoring a background as if it were the subject.
            best = None

            for tolerance in (16, 32, 48, 72, 96):
                candidate = _flood_mask(image, tolerance)
                extent = _extent(candidate)

                if extent <= _MIN_EXTENT:
                    continue

                if best is None or extent < best[0]:
                    best = (extent, candidate)

            mask = best[1] if best else _flat_mask(image, 20)

    bbox = mask.getbbox()

    if not bbox:
        return None, None

    cropped = mask.crop(bbox)
    histogram = cropped.histogram()

    coverage = (
        histogram[255]
        / float(cropped.width * cropped.height)
    )

    aspect = cropped.width / float(cropped.height or 1)

    normalised = cropped.resize(
        (_MASK_SIZE, _MASK_SIZE),
        Image.Resampling.NEAREST,
    )

    source_area = float(mask.width * mask.height)

    result = (normalised, {
        "aspect": aspect,
        "coverage": coverage,
        "extent": (cropped.width * cropped.height) / source_area,
    })

    _SILHOUETTE_CACHE[key] = result

    return result


def _metrics(reference_bytes, render_bytes):
    """Compare normalised silhouettes. Returns None, loudly, when unreliable."""
    try:
        reference_mask, reference_info = _silhouette(reference_bytes)
        render_mask, render_info = _silhouette(render_bytes)
    except Exception as error:
        print(
            f"[JARVIS] silhouette measurement failed: {error}",
            flush=True,
        )
        return None

    pairs = (
        ("reference", reference_mask, reference_info),
        ("render", render_mask, render_info),
    )

    for label, mask, info in pairs:
        if mask is None:
            print(
                f"[JARVIS] no silhouette score: the {label} has no "
                "detectable subject against its background",
                flush=True,
            )
            return None

        if info["extent"] >= _MAX_EXTENT:
            print(
                f"[JARVIS] no silhouette score: the {label} subject fills "
                f"{info['extent'] * 100:.0f}% of the frame — a floor plane "
                "or a busy background is being read as the subject",
                flush=True,
            )
            return None

        if info["extent"] <= _MIN_EXTENT:
            print(
                f"[JARVIS] no silhouette score: the {label} subject is only "
                f"{info['extent'] * 100:.1f}% of the frame",
                flush=True,
            )
            return None

    left = reference_mask.convert("1")
    right = render_mask.convert("1")

    intersection = ImageChops.logical_and(
        left,
        right,
    ).histogram()[255]

    union = ImageChops.logical_or(
        left,
        right,
    ).histogram()[255]

    if not union:
        return None

    return {
        "iou": intersection / float(union),
        "reference_aspect": reference_info["aspect"],
        "render_aspect": render_info["aspect"],
        "aspect_ratio": (
            render_info["aspect"] / (reference_info["aspect"] or 1e-6)
        ),
        "reference_fill": reference_info["coverage"],
        "render_fill": render_info["coverage"],
    }


_ENSURE_RENDERABLE = """
import bpy
import math
from mathutils import Vector

scene = bpy.context.scene

meshes = [
    obj
    for obj in scene.objects
    if obj.type == "MESH"
]

if not meshes:
    raise RuntimeError(
        "The generated script produced no mesh objects. The scene is empty."
    )

if scene.camera is None:
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

    centre = (minimum + maximum) * 0.5
    size = max((maximum - minimum).length, 1.0)

    data = bpy.data.cameras.new("JARVIS_Camera")
    camera = bpy.data.objects.new("JARVIS_Camera", data)
    scene.collection.objects.link(camera)

    camera.location = centre + Vector((
        0.0,
        -size * 1.4,
        size * 0.3,
    ))

    camera.rotation_euler = (
        centre - camera.location
    ).to_track_quat("-Z", "Y").to_euler()

    scene.camera = camera

if not any(obj.type == "LIGHT" for obj in scene.objects):
    light_data = bpy.data.lights.new("JARVIS_Light", type="SUN")
    light = bpy.data.objects.new("JARVIS_Light", light_data)
    scene.collection.objects.link(light)
    light.location = (0.0, 0.0, 10.0)

# An alpha channel gives an exact subject mask, so JARVIS can measure the
# silhouette instead of guessing the background colour.
scene.render.film_transparent = True
scene.render.image_settings.color_mode = "RGBA"

# A ground plane fills the frame and destroys that mask. Hide obvious ones
# from rendering only; nothing is deleted.
spans = []

for obj in meshes:
    box = [obj.matrix_world @ Vector(c) for c in obj.bound_box]

    width = max(
        max(p.x for p in box) - min(p.x for p in box),
        max(p.y for p in box) - min(p.y for p in box),
    )

    spans.append((obj, width, max(p.z for p in box) - min(p.z for p in box)))

typical = sorted(span for _o, span, _h in spans)[len(spans) // 2]
hidden = []

for obj, width, height in spans:
    if width > typical * 2.5 and height < width * 0.05:
        obj.hide_render = True
        hidden.append(obj.name)

if hidden:
    print("JARVIS hid ground geometry: " + ", ".join(hidden))

# Frame the subject consistently so the reviewer never has to spend a
# priority on the camera.
visible = [obj for obj in meshes if not obj.hide_render]

if visible:
    corners = []

    for obj in visible:
        corners.extend(
            obj.matrix_world @ Vector(corner)
            for corner in obj.bound_box
        )

    minimum = Vector((
        min(p.x for p in corners),
        min(p.y for p in corners),
        min(p.z for p in corners),
    ))

    maximum = Vector((
        max(p.x for p in corners),
        max(p.y for p in corners),
        max(p.z for p in corners),
    ))

    centre = (minimum + maximum) * 0.5
    extent = max((maximum - minimum).z, (maximum - minimum).x, 1e-3)

    camera = scene.camera

    # A longer lens flattens the perspective towards the elevation views
    # the reference photographs were taken at.
    camera.data.lens = 85.0

    distance = (extent * 1.15) / (2.0 * math.tan(camera.data.angle * 0.5))

    camera.location = Vector((
        centre.x,
        centre.y - distance,
        centre.z,
    ))

    camera.rotation_euler = (
        centre - camera.location
    ).to_track_quat("-Z", "Y").to_euler()
"""


def _wait_for_bridge(blender, timeout=90.0):
    """Blender takes time to boot and register its bridge port."""
    deadline = time.monotonic() + timeout
    waited = False

    while time.monotonic() < deadline:
        try:
            if blender.running():
                if waited:
                    print(
                        "[JARVIS] Blender bridge is up.",
                        flush=True,
                    )
                return True
        except Exception:
            pass

        if not waited:
            print(
                "[JARVIS] waiting for the Blender bridge to start...",
                flush=True,
            )
            waited = True

        time.sleep(2.0)

    return False


def _ensure_renderable(blender):
    """Fail loudly on an empty scene; supply a camera when one is missing."""
    if not _wait_for_bridge(blender):
        print(
            "[JARVIS] the Blender bridge never came up; "
            "skipping visual refinement.",
            flush=True,
        )
        return False

    last_error = None

    for attempt in range(3):
        try:
            blender._bridge_execute(_ENSURE_RENDERABLE)
            return True
        except Exception as error:
            last_error = error

            # A scene with no meshes will not fix itself; only retry
            # transport failures.
            if "no mesh objects" in str(error):
                break

            time.sleep(3.0)

    print(
        f"[JARVIS] scene is not renderable: {last_error}",
        flush=True,
    )

    return False


def _score(metrics):
    """Combine shape overlap with proportion into one comparable number.

    Normalised IoU deliberately ignores framing, so it cannot see a render
    that has the right outline at the wrong width. The aspect penalty
    supplies that half.
    """
    if not metrics:
        return None

    ratio = metrics["aspect_ratio"] or 1e-6
    penalty = min(ratio, 1.0 / ratio)

    return metrics["iou"] * penalty


def _metrics_block(metrics):
    """Turn measurements into concrete instructions for the QA reviewer."""
    if not metrics:
        return ""

    aspect = metrics["aspect_ratio"]

    if aspect > 1.08:
        proportion = (
            f"The render is {(aspect - 1.0) * 100:.0f}% too WIDE for its "
            f"height compared with the reference."
        )
    elif aspect < 0.93:
        proportion = (
            f"The render is {(1.0 - aspect) * 100:.0f}% too NARROW for its "
            f"height compared with the reference."
        )
    else:
        proportion = "Overall width-to-height proportion is close."

    fill = metrics["render_fill"] - metrics["reference_fill"]

    if fill > 0.08:
        bulk = "The render is too bulky and solid inside its outline."
    elif fill < -0.08:
        bulk = "The render is too thin or has too many gaps in its outline."
    else:
        bulk = "Overall bulk within the outline is close."

    return (
        "\n\nMEASURED SILHOUETTE ANALYSIS (computed, not estimated):\n"
        f"- Normalised silhouette overlap: {metrics['iou']:.2f} "
        "(1.00 is a perfect shape match)\n"
        f"- {proportion}\n"
        f"- {bulk}\n"
        "Treat these measurements as ground truth. Your PRIORITY 1 issue must "
        "address the largest measured discrepancy above when one exists.\n"
    )


# ---------------------------------------------------------------------------
# Provider wrappers
# ---------------------------------------------------------------------------

# The Anthropic SDK rejects non-streaming requests whose max_tokens implies
# a run longer than ten minutes. Staying under that ceiling buys room for
# thinking tokens without needing streaming support in providers.py.
_EFFORT_TOKENS = {
    "medium": 16000,
    "high": 20000,
}

# Only calls that already ask for a large budget are at risk of being
# starved by thinking tokens. Raising a small analysis call to 24k gets it
# rejected outright by the provider.
_SCALE_ABOVE = 8000


def _with_effort(name, original):
    """Raise reasoning effort, and the token ceiling that pays for it.

    A thinking model spends part of max_tokens on internal reasoning. Raising
    effort without raising the ceiling on a large generation call makes it
    think until the budget is gone and return nothing.
    """
    floor = _EFFORT_TOKENS.get(_EFFORT)

    def wrapped(*args, **kwargs):
        original_effort = kwargs.get("reasoning_effort")
        original_tokens = kwargs.get("max_tokens")

        if kwargs.get("reasoning_effort") == "low":
            kwargs["reasoning_effort"] = _EFFORT

            requested = kwargs.get("max_tokens") or 0

            if floor and requested >= _SCALE_ABOVE:
                kwargs["max_tokens"] = max(requested, floor)

        result = original(*args, **kwargs)

        if not result:
            payload = ""

            if len(args) > 1 and isinstance(args[1], (bytes, bytearray)):
                payload = f", image={len(args[1])} bytes"
            elif len(args) > 1 and args[1] is None:
                payload = ", image=None"

            print(
                f"[JARVIS] {name} returned nothing "
                f"(max_tokens={kwargs.get('max_tokens')}, "
                f"effort={kwargs.get('reasoning_effort')}{payload})",
                flush=True,
            )

            # An upgraded call that comes back empty is worth one retry at
            # the original settings before the whole action fails.
            if kwargs.get("reasoning_effort") != original_effort:
                print(
                    f"[JARVIS] retrying {name} at the original settings",
                    flush=True,
                )

                retry = dict(kwargs)
                retry["reasoning_effort"] = original_effort
                retry["max_tokens"] = original_tokens

                result = original(*args, **retry)

                if result:
                    print(
                        f"[JARVIS] {name} retry succeeded",
                        flush=True,
                    )

        return result

    wrapped._jarvis_effort_patched = True

    return wrapped


def _loud_validate(original):
    """Print why a generated script was rejected, then re-raise."""
    def wrapped(source):
        try:
            return original(source)
        except SyntaxError as error:
            lines = (source or "").splitlines()
            line = ""

            if error.lineno and 0 < error.lineno <= len(lines):
                line = lines[error.lineno - 1].strip()

            print(
                f"[JARVIS] generated script syntax error at line "
                f"{error.lineno}: {error.msg}",
                flush=True,
            )

            if line:
                print(f"[JARVIS]   {line}", flush=True)

            print(
                f"[JARVIS]   script was {len(lines)} lines, "
                f"{len(source or '')} chars",
                flush=True,
            )

            raise
        except ValueError as error:
            print(
                f"[JARVIS] generated script rejected: {error}",
                flush=True,
            )
            raise

    return wrapped


def _grounded_scene_script(blender, original):
    """Always show the reference image to the model that writes the code."""
    def wrapped(brief, reference_image=None):
        if reference_image is None:
            try:
                current = blender.images.current_original_bytes()

                if current:
                    with Image.open(io.BytesIO(current)) as image:
                        buffer = io.BytesIO()
                        image.convert("RGB").save(buffer, format="PNG")
                        reference_image = buffer.getvalue()

                    print(
                        "[JARVIS] attaching reference image to script "
                        "generation.",
                        flush=True,
                    )
            except Exception as error:
                print(
                    f"[JARVIS] could not attach reference image: {error}",
                    flush=True,
                )

        return original(brief, reference_image=reference_image)

    return wrapped


# ---------------------------------------------------------------------------
# Measured refinement loops
# ---------------------------------------------------------------------------

def _scored_refine_current_scene(blender):
    """Refine until the measured silhouette stops improving."""
    def refine(brief):
        reference_bytes = blender.images.current_original_bytes()

        if not reference_bytes:
            print(
                "[JARVIS] no reference image available for visual refinement",
                flush=True,
            )
            return

        if not _ensure_renderable(blender):
            return

        best = None

        for pass_number in range(1, _MAX_PASSES + 1):
            preview = blender._bridge_render_preview()
            metrics = _metrics(reference_bytes, preview["bytes"])

            if metrics:
                score = _score(metrics)

                print(
                    f"[JARVIS] silhouette score {score:.3f} "
                    f"(shape {metrics['iou']:.3f}, "
                    f"aspect {metrics['aspect_ratio']:.2f})",
                    flush=True,
                )

                if best is not None and score < best - 0.02:
                    print(
                        f"[JARVIS] last pass reduced the match "
                        f"({best:.3f} -> {score:.3f}); "
                        "stopping refinement.",
                        flush=True,
                    )
                    break

                best = max(best or 0.0, score)

            print(
                f"[JARVIS] visual refinement pass "
                f"{pass_number}/{_MAX_PASSES}",
                flush=True,
            )

            review, issues = blender._review_preview(
                reference_bytes,
                preview["bytes"],
                brief + _metrics_block(metrics),
            )

            if not review:
                print(
                    "[JARVIS] visual QA returned no report; stopping.",
                    flush=True,
                )
                return

            if review == "NO_CRITICAL_MISMATCHES":
                print(
                    "[JARVIS] visual QA found no critical mismatches.",
                    flush=True,
                )
                return

            comparison = blender._comparison_image(
                reference_bytes,
                preview["bytes"],
            )

            scene_text = json.dumps(
                blender.modeling_context(),
                indent=2,
            )

            script = blender._generate_refinement_script(
                brief,
                scene_text,
                review + _metrics_block(metrics),
                comparison,
            )

            blender._bridge_execute(script)

            print(
                f"[JARVIS] refinement pass {pass_number} applied.",
                flush=True,
            )

        blender._bridge_render_preview()

        print(
            "[JARVIS] final visual refinement render completed.",
            flush=True,
        )

    return refine


def _scored_refine_multiview_scene(blender):
    """Multi-view refinement, scored against the front reference view."""
    def refine(brief, views):
        if not _ensure_renderable(blender):
            return

        primary = "front" if "front" in views else next(iter(views))
        best = None

        for pass_number in range(1, _MAX_PASSES + 1):
            renders = blender._render_multiviews(views)
            metrics = _metrics(views[primary], renders[primary])

            if metrics:
                score = _score(metrics)

                print(
                    f"[JARVIS] {primary} silhouette score {score:.3f} "
                    f"(shape {metrics['iou']:.3f}, "
                    f"aspect {metrics['aspect_ratio']:.2f})",
                    flush=True,
                )

                if best is not None and score < best - 0.02:
                    print(
                        f"[JARVIS] last pass reduced the match "
                        f"({best:.3f} -> {score:.3f}); "
                        "stopping refinement.",
                        flush=True,
                    )
                    break

                best = max(best or 0.0, score)

            print(
                f"[JARVIS] multi-view visual refinement pass "
                f"{pass_number}/{_MAX_PASSES}",
                flush=True,
            )

            review, issues, comparison = blender._review_multiview(
                views,
                renders,
                brief + _metrics_block(metrics),
            )

            if review == "NO_CRITICAL_MISMATCHES":
                print(
                    "[JARVIS] multi-view visual QA found no critical "
                    "mismatches.",
                    flush=True,
                )
                break

            scene_text = json.dumps(
                blender.modeling_context(),
                indent=2,
            )

            script = blender._generate_refinement_script(
                brief,
                scene_text,
                review + _metrics_block(metrics),
                comparison,
            )

            blender._bridge_execute(script)

            print(
                f"[JARVIS] multi-view refinement pass "
                f"{pass_number} applied.",
                flush=True,
            )

        blender._render_multiviews(views)

    return refine


# ---------------------------------------------------------------------------

def _relaxed_multiview_request(original):
    """Accept the natural ways of asking for a multi-view model."""
    trigger = re.compile(
        r"\b(?:"
        r"(?:\d+|two|three|four|five|six|multiple|several|multi[- ]?|all)"
        r"\s+"
        r"(?:reference\s+)?(?:views?|angles?|photos?|images?|sides?|shots?)"
        r"|multi[- ]?view"
        r"|reference\s+set"
        r")\b",
        re.I,
    )

    def wrapped(request):
        if original(request):
            return True

        return bool(trigger.search(request or ""))

    return wrapped


def _tolerant_find_reference_view(blender, original):
    """Find front.png, but also iron-man-front.jpg or front_view.png."""
    suffixes = (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
    )

    def wrapped(folder, view):
        exact = original(folder, view)

        if exact is not None:
            return exact

        try:
            candidates = sorted(folder.iterdir())
        except OSError:
            return None

        for path in candidates:
            if not path.is_file():
                continue

            if path.suffix.casefold() not in suffixes:
                continue

            tokens = set(
                re.split(
                    r"[^a-z0-9]+",
                    path.stem.casefold(),
                )
            )

            if view in tokens:
                return path

        return None

    return wrapped


def _relaxed_reference_set(blender):
    """Require a front view plus any two others, rather than front AND back."""
    def has_reference_set(request):
        if not blender._is_multiview_request(request):
            return False

        subject = blender._multiview_subject(request)
        folder = blender._reference_set_path(subject)

        if not folder.is_dir():
            return False

        available = {
            view
            for view in blender._MULTIVIEW_SUPPORTED_VIEWS
            if blender._find_reference_view(folder, view)
        }

        return (
            "front" in available
            and len(available) >= blender._MULTIVIEW_MIN_VIEWS
        )

    return has_reference_set


def _relaxed_load_references(blender, original):
    """Load whatever supported views exist, needing only front plus two."""
    def wrapped(subject):
        folder = blender._reference_set_path(subject)

        if not folder.is_dir():
            raise RuntimeError(
                f"Reference set not found: {folder}"
            )

        views = {}

        for view in blender._MULTIVIEW_SUPPORTED_VIEWS:
            path = blender._find_reference_view(folder, view)

            if path is None:
                continue

            if not blender.images.supports_path(path):
                raise RuntimeError(
                    f"Invalid {view} reference image: {path}"
                )

            views[view] = path.read_bytes()

        if "front" not in views:
            raise RuntimeError(
                "A multi-view reference set must contain a front view. "
                f"Name the files front/back/left/right inside {folder}."
            )

        if len(views) < blender._MULTIVIEW_MIN_VIEWS:
            raise RuntimeError(
                "A multi-view reference set needs at least "
                f"{blender._MULTIVIEW_MIN_VIEWS} views. "
                f"Found: {', '.join(sorted(views)) or 'none'}."
            )

        print(
            f"[JARVIS] loaded reference views: "
            f"{', '.join(sorted(views))}",
            flush=True,
        )

        return views

    return wrapped


# ---------------------------------------------------------------------------

_CAMERA_OVERRIDE = """

CAMERA HANDLING: JARVIS positions, frames and levels the camera automatically
before every render, identically for every view. Do NOT report camera issues.
Do NOT emit TYPE: camera. Spend every priority on geometry, proportion,
repetition and materials instead.

OUTPUT FORMAT: return ONLY the PRIORITY blocks. No preamble, no heading, no
explanation of what you are skipping, no closing summary, no blank commentary
between blocks. The response must begin with "PRIORITY: 1".
"""


_QA_BLOCK = re.compile(
    r"PRIORITY:\s*\d+\s*\n"
    r"TYPE:\s*(geometry|proportion|repetition|material|camera|depth)\s*\n"
    r"LOCATION:\s*(.+?)\s*\n"
    r"PROBLEM:\s*(.+?)\s*\n"
    r"ACTION:\s*(.+?)"
    r"(?=\nPRIORITY:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _salvaged_parse(original):
    """Keep a good QA report that arrived wrapped in commentary.

    The strict parser discards the whole report when more than a tenth of it
    is unstructured text, which throws away four usable priorities because
    of one introductory sentence.
    """
    def wrapped(review):
        try:
            return original(review)
        except ValueError as error:
            text = (review or "").strip()
            rebuilt = []

            for match in _QA_BLOCK.finditer(text):
                fields = [group.strip() for group in match.groups()]

                if not all(fields):
                    continue

                rebuilt.append(
                    f"PRIORITY: {len(rebuilt) + 1}\n"
                    f"TYPE: {fields[0].casefold()}\n"
                    f"LOCATION: {fields[1]}\n"
                    f"PROBLEM: {fields[2]}\n"
                    f"ACTION: {fields[3]}"
                )

                if len(rebuilt) == 5:
                    break

            if not rebuilt:
                raise

            print(
                f"[JARVIS] salvaged {len(rebuilt)} QA priorities from an "
                f"unstructured report ({error})",
                flush=True,
            )

            return original("\n".join(rebuilt))

    return wrapped


def install():
    """Apply fidelity patches once, after blender.py is importable."""
    global _installed

    if _installed:
        return

    from actions import blender

    blender._REFERENCE_PROMPT = _REFERENCE_PROMPT
    blender._MULTIVIEW_PROMPT = _MULTIVIEW_PROMPT
    blender._SCRIPT_PROMPT = blender._SCRIPT_PROMPT.replace(
        "MODELLING BRIEF:",
        _SCRIPT_TAIL + "\nMODELLING BRIEF:",
    )

    blender._REVIEW_PROMPT = blender._REVIEW_PROMPT + _CAMERA_OVERRIDE

    # Old cached briefs were written by the architecture-only prompt.
    blender._REFERENCE_CACHE_VERSION = "2"

    blender.vision = _with_effort("vision", blender.vision)
    blender.vision_chat = _with_effort("vision_chat", blender.vision_chat)
    blender.chat = _with_effort("chat", blender.chat)

    blender._generate_scene_script = _grounded_scene_script(
        blender,
        blender._generate_scene_script,
    )

    blender._validate_script = _loud_validate(blender._validate_script)

    blender._parse_visual_review = _salvaged_parse(
        blender._parse_visual_review,
    )

    blender._refine_current_scene = _scored_refine_current_scene(blender)
    blender._refine_multiview_scene = _scored_refine_multiview_scene(blender)

    blender._is_multiview_request = _relaxed_multiview_request(
        blender._is_multiview_request,
    )

    blender._find_reference_view = _tolerant_find_reference_view(
        blender,
        blender._find_reference_view,
    )

    blender._load_multiview_references = _relaxed_load_references(
        blender,
        blender._load_multiview_references,
    )

    blender.has_reference_set = _relaxed_reference_set(blender)

    _installed = True

    print(
        f"[JARVIS] Blender fidelity patches installed "
        f"(reasoning effort: {_EFFORT}).",
        flush=True,
    )
