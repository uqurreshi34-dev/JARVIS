"""Tripo API integration for JARVIS 3D reconstruction."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://openapi.tripo3d.ai/v3"
MODEL_H31 = "v3.1-20260211"

TERMINAL_STATUSES = {"success", "failed", "cancelled"}

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 300.0


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
    back: str,
    left: str,
    right: str | None = None,
    texture: bool = True,
    pbr: bool = True,
    texture_quality: str | None = None,
    geometry_quality: str | None = None,
) -> str:
    """Create an H-Series multiview-to-model task."""
    inputs = [
        {"front": front},
        {"back": back},
        {"left": left},
    ]

    if right:
        inputs.append({"right": right})

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
    back: Path,
    left: Path,
) -> dict[str, str]:
    """
    Upload the three reference views required for the first JARVIS
    H3.1 multiview experiment.

    This does NOT create a generation task.
    It only returns Tripo file_tokens.
    """
    return {
        "front": upload_image(front),
        "back": upload_image(back),
        "left": upload_image(left),
    }


def create_ironman_multiview_task() -> str:
    """
    Create the first JARVIS H3.1 Iron Man reconstruction task.

    Uses the three uploaded reference views:
    front, back, left.

    Expected cost:
        40 credits for detailed texture
        +20 credits for detailed geometry
        = 60 credits
    """
    return create_multiview_task(
        front="file_a3503c1b-f440-4a27-ac66-e578e12c17e6",
        back="file_4ac11b56-ce19-4ae6-bb4e-46339af9e7b0",
        left="file_881522dd-06cc-4a88-a913-636a31703138",
        texture=True,
        pbr=True,
        texture_quality="detailed",
        geometry_quality="detailed",
    )


def generate_reference_set(
    *,
    subject: str,
    reference_root: Path,
    output_path: Path,
) -> Path:
    """
    Generate a Tripo H3.1 model from the available front/back/left
    reference images and download the resulting GLB.

    This is the JARVIS orchestration layer for the first Tripo test.
    """
    reference_root = Path(reference_root)

    front = reference_root / "front.png"
    back = reference_root / "back.png"
    left = reference_root / "left.png"

    missing = [
        str(path)
        for path in (front, back, left)
        if not path.is_file()
    ]

    if missing:
        raise TripoError(
            "Missing required reference images: "
            + ", ".join(missing)
        )

    print(
        f"[JARVIS] uploading Tripo references for {subject}...",
        flush=True,
    )

    tokens = upload_multiview_views(
        front=front,
        back=back,
        left=left,
    )

    print(
        "[JARVIS] Tripo references uploaded.",
        flush=True,
    )

    print(
        "[JARVIS] starting H3.1 multiview reconstruction...",
        flush=True,
    )

    task_id = create_multiview_task(
        front=tokens["front"],
        back=tokens["back"],
        left=tokens["left"],
        texture=True,
        pbr=True,
        texture_quality="detailed",
        geometry_quality="detailed",
    )

    print(
        f"[JARVIS] Tripo task started: {task_id}",
        flush=True,
    )

    task = wait_for_task(task_id)

    output = task.get("output") or {}
    model_url = output.get("model_url")

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
