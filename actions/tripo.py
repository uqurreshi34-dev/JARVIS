"""Tripo API integration for JARVIS 3D reconstruction."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://openapi.tripo3d.ai/v3"
MODEL_H31 = "v3.1-20260211"
MODEL_SEGMENTATION_V2 = "v2.0-20260430"

TERMINAL_STATUSES = {"success", "failed", "cancelled"}

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 300.0

_SUPPORTED_VIEWS = ("front", "left", "back", "right")

_VIEW_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
)


def _find_view_file(root: Path, view: str) -> Path | None:
    """Find one named reference view, case-insensitively."""
    root = Path(root)

    if not root.is_dir():
        return None

    for path in root.iterdir():
        if not path.is_file():
            continue

        if path.suffix.casefold() not in _VIEW_EXTENSIONS:
            continue

        if path.stem.casefold() == view.casefold():
            return path

    return None


def find_reference_views(reference_root: Path) -> dict[str, Path]:
    """
    Discover the supported views present in a reference set.

    Front is required. At least two views are required.
    Unsupported extra images are ignored.
    """
    reference_root = Path(reference_root)

    views = {}

    for view in _SUPPORTED_VIEWS:
        path = _find_view_file(reference_root, view)

        if path is not None:
            views[view] = path

    if "front" not in views:
        raise TripoError(
            f"Reference set is missing the required front view: "
            f"{reference_root}"
        )

    if len(views) < 2:
        raise TripoError(
            f"Reference set needs at least two supported views: "
            f"{reference_root}"
        )

    return views


class TripoError(RuntimeError):
    """Raised when a Tripo API operation fails."""


def _api_key() -> str:
    key = os.environ.get("TRIPO_API_KEY", "").strip()

    if not key:
        raise TripoError(
            "TRIPO_API_KEY is not set. "
            "Set the Tripo API key as an environment variable."
        )

    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _check_response(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TripoError(
            f"Tripo returned non-JSON HTTP {response.status_code}: "
            f"{response.text[:500]}"
        ) from exc

    if not response.ok:
        raise TripoError(
            f"Tripo HTTP {response.status_code}: {payload}"
        )

    if payload.get("code") not in (0, None):
        raise TripoError(
            f"Tripo API error {payload.get('code')}: "
            f"{payload.get('message') or payload}"
        )

    return payload


def get_balance() -> dict[str, Any]:
    """Return the current Tripo API account balance."""
    response = requests.get(
        f"{BASE_URL}/account/balance",
        headers=_headers(),
        timeout=30,
    )
    return _check_response(response)


def upload_image(path: Path) -> str:
    """Upload an image and return its Tripo file_token."""
    path = Path(path)

    if not path.is_file():
        raise TripoError(f"Reference image does not exist: {path}")

    with path.open("rb") as handle:
        response = requests.post(
            f"{BASE_URL}/files",
            headers={"Authorization": f"Bearer {_api_key()}"},
            files={"file": (path.name, handle)},
            timeout=60,
        )

    payload = _check_response(response)

    try:
        return str(payload["data"]["file_token"])
    except (KeyError, TypeError) as exc:
        raise TripoError(
            f"Tripo upload response did not contain file_token: {payload}"
        ) from exc


def create_multiview_task(
    *,
    front: str,
    left: str | None = None,
    back: str | None = None,
    right: str | None = None,
    texture: bool = True,
    pbr: bool = True,
    texture_quality: str | None = None,
    geometry_quality: str | None = None,
) -> str:
    """Create an H-Series multiview-to-model task from 2–4 views."""
    tokens = {
        "front": front,
        "left": left,
        "back": back,
        "right": right,
    }

    inputs = [
        {view: token}
        for view in _SUPPORTED_VIEWS
        if (token := tokens.get(view))
    ]

    if "front" not in tokens or not front:
        raise TripoError(
            "Tripo multiview generation requires a front view."
        )

    if len(inputs) < 2:
        raise TripoError(
            "Tripo multiview generation requires at least two views."
        )

    payload: dict[str, Any] = {
        "inputs": inputs,
        "model": MODEL_H31,
        "texture": texture,
        "pbr": pbr,
    }

    if texture_quality:
        payload["texture_quality"] = texture_quality

    if geometry_quality:
        payload["geometry_quality"] = geometry_quality

    response = requests.post(
        f"{BASE_URL}/generation/multiview-to-model",
        headers=_headers(),
        json=payload,
        timeout=60,
    )

    result = _check_response(response)

    try:
        return str(result["data"]["task_id"])
    except (KeyError, TypeError) as exc:
        raise TripoError(
            f"Tripo task response did not contain task_id: {result}"
        ) from exc


def get_task(task_id: str) -> dict[str, Any]:
    """Return the current state of a Tripo task."""
    response = requests.get(
        f"{BASE_URL}/tasks/{task_id}",
        headers=_headers(),
        timeout=30,
    )
    payload = _check_response(response)

    try:
        data = payload["data"]
    except KeyError as exc:
        raise TripoError(
            f"Tripo task response did not contain data: {payload}"
        ) from exc

    if not isinstance(data, dict):
        raise TripoError(
            f"Unexpected Tripo task payload: {payload}"
        )

    return data


def create_mesh_segmentation_task(
    source_task_id: str,
    *,
    granularity: str = "balanced",
    split_by_connectivity: bool = True,
) -> str:
    """
    Create a semantic mesh-segmentation task from an existing Tripo
    3D-generation task.

    Uses Tripo's v2 semantic segmentation model.
    """
    if not source_task_id.strip():
        raise TripoError(
            "A source Tripo task ID is required for segmentation."
        )

    if granularity not in {"simple", "balanced", "detailed"}:
        raise TripoError(
            f"Invalid segmentation granularity: {granularity}"
        )

    payload: dict[str, Any] = {
        "model": MODEL_SEGMENTATION_V2,
        "input": source_task_id,
        "segmentation_granularity": granularity,
        "split_by_connectivity": split_by_connectivity,
    }

    response = requests.post(
        f"{BASE_URL}/mesh/segment",
        headers=_headers(),
        json=payload,
        timeout=60,
    )

    result = _check_response(response)

    try:
        return str(result["data"]["task_id"])
    except (KeyError, TypeError) as exc:
        raise TripoError(
            "Tripo segmentation response did not contain task_id: "
            f"{result}"
        ) from exc


def generate_segmented_model(
    *,
    source_task_id: str,
    output_path: Path,
    granularity: str = "balanced",
    split_by_connectivity: bool = True,
) -> Path:
    """
    Segment an existing Tripo model semantically and download the
    resulting GLB.
    """
    segmentation_task_id = create_mesh_segmentation_task(
        source_task_id,
        granularity=granularity,
        split_by_connectivity=split_by_connectivity,
    )

    print(
        f"[JARVIS] Tripo segmentation task: "
        f"{segmentation_task_id}",
        flush=True,
    )

    task = wait_for_task(
        segmentation_task_id
    )

    model_url = (
        (task.get("output") or {})
        .get("model_url")
    )

    if not model_url:
        raise TripoError(
            "Tripo segmentation completed without a model URL: "
            f"{task}"
        )

    print(
        "[JARVIS] Tripo semantic segmentation complete; "
        "downloading segmented GLB...",
        flush=True,
    )

    return download_model(
        str(model_url),
        output_path,
    )


def wait_for_task(
    task_id: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Poll a Tripo task until it reaches a terminal state."""
    started = time.monotonic()

    while True:
        task = get_task(task_id)
        status = str(task.get("status", "")).lower()

        if status in TERMINAL_STATUSES:
            if status != "success":
                raise TripoError(
                    "Tripo task failed: "
                    f"status={status}, "
                    f"error_code={task.get('error_code')}, "
                    f"error_message={task.get('error_message')}"
                )

            return task

        if time.monotonic() - started >= timeout:
            raise TripoError(
                f"Timed out waiting for Tripo task {task_id} "
                f"after {timeout:.0f}s."
            )

        time.sleep(poll_interval)


def download_model(
    model_url: str,
    output_path: Path,
) -> Path:
    """Download a generated GLB immediately from Tripo's expiring URL."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with requests.get(model_url, stream=True, timeout=120) as response:
        if not response.ok:
            raise TripoError(
                f"Failed to download Tripo model: "
                f"HTTP {response.status_code}"
            )

        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    return output_path


def generate_multiview_model(
    *,
    front: Path,
    back: Path,
    left: Path,
    right: Path,
    output_path: Path,
    texture: bool = True,
    pbr: bool = True,
    texture_quality: str | None = None,
    geometry_quality: str | None = None,
) -> Path:
    """
    Upload four reference views, generate an H3.1 model,
    wait for completion, and download the GLB.
    """
    front_token = upload_image(front)
    back_token = upload_image(back)
    left_token = upload_image(left)
    right_token = upload_image(right)

    task_id = create_multiview_task(
        front=front_token,
        back=back_token,
        left=left_token,
        right=right_token,
        texture=texture,
        pbr=pbr,
        texture_quality=texture_quality,
        geometry_quality=geometry_quality,
    )

    task = wait_for_task(task_id)

    output = task.get("output") or {}
    model_url = output.get("model_url")

    if not model_url:
        raise TripoError(
            f"Successful Tripo task has no model_url: {task}"
        )

    return download_model(
        str(model_url),
        output_path,
    )


def upload_multiview_views(
    *,
    front: Path,
    left: Path | None = None,
    back: Path | None = None,
    right: Path | None = None,
) -> dict[str, str]:
    """
    Upload whichever supported reference views are supplied.

    Front is required. At least one additional view must be supplied.
    """
    paths = {
        "front": front,
        "left": left,
        "back": back,
        "right": right,
    }

    if front is None:
        raise TripoError("A front reference image is required.")

    tokens = {}

    for view in _SUPPORTED_VIEWS:
        path = paths.get(view)

        if path is None:
            continue

        tokens[view] = upload_image(path)

    if len(tokens) < 2:
        raise TripoError(
            "At least two reference views are required."
        )

    return tokens


def generate_reference_set(
    *,
    subject: str,
    reference_root: Path,
    output_path: Path,
) -> Path:
    """
    Generate an H3.1 model from whatever supported views are present
    in the subject's reference set.
    """
    reference_root = Path(reference_root)

    views = find_reference_views(
        reference_root
    )

    print(
        "[JARVIS] Tripo reference views: "
        + ", ".join(view.upper() for view in views),
        flush=True,
    )

    tokens = upload_multiview_views(
        front=views["front"],
        left=views.get("left"),
        back=views.get("back"),
        right=views.get("right"),
    )

    print(
        "[JARVIS] Tripo references uploaded.",
        flush=True,
    )

    task_id = create_multiview_task(
        front=tokens["front"],
        left=tokens.get("left"),
        back=tokens.get("back"),
        right=tokens.get("right"),
        texture=True,
        pbr=True,
        texture_quality="detailed",
        geometry_quality="detailed",
    )

    print(
        f"[JARVIS] Tripo H3.1 task: {task_id}",
        flush=True,
    )

    task = wait_for_task(
        task_id
    )

    model_url = (
        (task.get("output") or {})
        .get("model_url")
    )

    if not model_url:
        raise TripoError(
            f"Tripo completed without a model URL: {task}"
        )

    print(
        "[JARVIS] Tripo reconstruction complete; "
        "downloading GLB...",
        flush=True,
    )

    return download_model(
        str(model_url),
        output_path,
    )


def generate_segmented_reference_set(
    *,
    subject: str,
    reference_root: Path,
    output_path: Path,
) -> Path:
    """
    Generate a detailed H3.1 model from the subject reference set,
    then semantically segment that generated model.

    Final output is the segmented GLB intended for Blender.
    """
    reference_root = Path(reference_root)

    views = find_reference_views(
        reference_root
    )

    print(
        "[JARVIS] Tripo reference views: "
        + ", ".join(view.upper() for view in views),
        flush=True,
    )

    tokens = upload_multiview_views(
        front=views["front"],
        left=views.get("left"),
        back=views.get("back"),
        right=views.get("right"),
    )

    print(
        "[JARVIS] Tripo references uploaded.",
        flush=True,
    )

    generation_task_id = create_multiview_task(
        front=tokens["front"],
        left=tokens.get("left"),
        back=tokens.get("back"),
        right=tokens.get("right"),
        texture=True,
        pbr=True,
        texture_quality="detailed",
        geometry_quality="detailed",
    )

    print(
        f"[JARVIS] Tripo H3.1 task: {generation_task_id}",
        flush=True,
    )

    generation_task = wait_for_task(
        generation_task_id
    )

    generation_output = generation_task.get("output") or {}
    generation_url = generation_output.get("model_url")

    if not generation_url:
        raise TripoError(
            "Tripo H3.1 completed without a model URL."
        )

    # Keep the detailed generation locally as a useful backup.
    detailed_path = Path(output_path).with_name(
        f"{Path(output_path).stem}-detailed.glb"
    )

    download_model(
        str(generation_url),
        detailed_path,
    )

    print(
        "[JARVIS] detailed H3.1 model downloaded.",
        flush=True,
    )

    segmentation_task_id = create_mesh_segmentation_task(
        generation_task_id,
        granularity="balanced",
        split_by_connectivity=True,
    )

    print(
        f"[JARVIS] Tripo segmentation task: "
        f"{segmentation_task_id}",
        flush=True,
    )

    segmentation_task = wait_for_task(
        segmentation_task_id
    )

    segmentation_output = (
        segmentation_task.get("output") or {}
    )

    segmented_url = segmentation_output.get(
        "model_url"
    )

    if not segmented_url:
        raise TripoError(
            "Tripo segmentation completed without a model URL."
        )

    segmented_path = download_model(
        str(segmented_url),
        output_path,
    )

    print(
        f"[JARVIS] segmented model downloaded: "
        f"{segmented_path}",
        flush=True,
    )

    return segmented_path
