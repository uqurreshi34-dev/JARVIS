"""Reference-image analysis for future Blender modelling."""

from providers import vision

from actions import images


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

Do not write Blender Python code.
Do not invent hidden geometry as fact.
Keep the result concise and practical.
"""


def analyse_current_reference(request=None):
    """Analyse the current JARVIS image as a future Blender reference."""
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
