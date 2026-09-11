"""Background Windows updater for the packaged JARVIS application.

The helper is installed separately from JARVIS.exe so an update can replace
both the application and the helper without the running JARVIS process having
to overwrite itself.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

from app_version import VERSION


DEFAULT_UPDATE_REPOSITORY = "uqurreshi34-dev/JARVIS-Releases"

REPOSITORY = os.environ.get(
    "JARVIS_UPDATE_REPOSITORY",
    DEFAULT_UPDATE_REPOSITORY,
).strip()

RELEASE_API = (
    "https://api.github.com/repos/"
    f"{REPOSITORY}/releases/latest"
)

ASSET_NAME = "JARVIS-Setup.exe"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 180
STATE_FILE = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home())
    / "JARVIS"
    / "update-state.json"
)

MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
MB_YESNO = 0x00000004
MB_ICONQUESTION = 0x00000020
MB_TOPMOST = 0x00040000
IDYES = 6


def _message(text: str, *, question: bool = False) -> bool:
    """Show a small native Windows update prompt."""
    style = (
        MB_YESNO | MB_ICONQUESTION
        if question
        else MB_OK | MB_ICONINFORMATION
    ) | MB_TOPMOST

    try:
        result = ctypes.windll.user32.MessageBoxW(
            0,
            text,
            "JARVIS Update",
            style,
        )
        return result == IDYES
    except Exception:
        return False


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _latest_release() -> dict | None:
    request = Request(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "JARVIS-Updater",
        },
    )

    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    if payload.get("draft") or payload.get("prerelease"):
        return None

    tag = str(payload.get("tag_name") or "").strip()

    if tag.startswith(("v", "V")):
        tag = tag[1:]

    try:
        release_version = Version(tag)
        current_version = Version(VERSION)
    except InvalidVersion:
        return None

    if release_version <= current_version:
        return None

    assets = payload.get("assets")

    if not isinstance(assets, list):
        return None

    for asset in assets:
        if not isinstance(asset, dict):
            continue

        if asset.get("name") != ASSET_NAME:
            continue

        digest = str(asset.get("digest") or "").strip().lower()
        download_url = str(
            asset.get("browser_download_url") or ""
        ).strip()

        if not digest.startswith("sha256:") or not download_url:
            return None

        return {
            "version": str(release_version),
            "download_url": download_url,
            "digest": digest.split(":", 1)[1],
        }

    return None


def _already_declined(version: str) -> bool:
    return _read_state().get("declined_version") == version


def _mark_declined(version: str) -> None:
    state = _read_state()
    state["declined_version"] = version
    _write_state(state)


def _download(release: dict) -> Path | None:
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="jarvis-update-"))
        destination = temp_dir / ASSET_NAME

        request = Request(
            release["download_url"],
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "JARVIS-Updater",
            },
        )

        digest = hashlib.sha256()

        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            with destination.open("wb") as handle:
                while True:
                    block = response.read(1024 * 1024)

                    if not block:
                        break

                    handle.write(block)
                    digest.update(block)

        if digest.hexdigest().casefold() != release["digest"].casefold():
            print("[JARVIS updater] installer digest verification failed")
            try:
                destination.unlink()
                temp_dir.rmdir()
            except OSError:
                pass
            return None

        return destination

    except (OSError, HTTPError, URLError):
        return None


def _install(installer: Path) -> bool:
    app_dir = Path(sys_executable()).resolve().parent

    try:
        subprocess.Popen(
            [
                str(installer),
                "/SP-",
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/CLOSEAPPLICATIONS",
                "/NORESTART",
                "/DIR=" + str(app_dir),
            ],
            cwd=str(installer.parent),
            close_fds=True,
        )
    except OSError as error:
        print(f"[JARVIS updater] could not launch installer: {error}")
        return False

    return True


def sys_executable() -> str:
    """Return the helper executable path, not a Python interpreter path."""
    import sys

    return sys.executable


def check_once() -> bool:
    """Check for one newer release and offer to install it."""
    release = _latest_release()

    if not release:
        return False

    version = release["version"]

    if _already_declined(version):
        return False

    if not _message(
        f"JARVIS {version} is available. Update now?",
        question=True,
    ):
        _mark_declined(version)
        return False

    _message(
        f"JARVIS {version} will download and install now. "
        "JARVIS may restart during the update."
    )

    installer = _download(release)

    if not installer:
        _message(
            "I couldn't download or verify the JARVIS update. "
            "Your current version has not been changed."
        )
        return False

    return _install(installer)


def main() -> None:
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
