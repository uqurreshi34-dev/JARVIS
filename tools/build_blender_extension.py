"""Build the JARVIS Blender bridge into a zip for Blender's Install from Disk.

The bridge's source is blender_extension/jarvis_bridge/; the zip is made from
it whenever wanted, rather than kept in git, where a copy built once falls
behind the source it came from.

    python tools/build_blender_extension.py

writes dist/jarvis_bridge.zip (dist/ is not kept in git). In Blender: Edit >
Preferences > Get Extensions > the drop-down at the top right > Install from
Disk, and choose it.
"""

import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "blender_extension" / "jarvis_bridge"
OUTPUT = ROOT / "dist" / "jarvis_bridge.zip"

# Never part of the extension: Python's leftovers, and zips of earlier builds
# (Blender's own "extension build" leaves one beside the source each time).
_LEFT_OUT = ("__pycache__", ".pyc", ".zip")


def build(source=SOURCE, output=OUTPUT):
    """Zip [source]'s files, at the root of the archive as Blender expects. Returns the zip's path."""
    if not (source / "blender_manifest.toml").exists():
        raise FileNotFoundError(f"no blender_manifest.toml in {source}")

    output.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and not any(part in str(path) for part in _LEFT_OUT):
                archive.write(path, path.relative_to(source).as_posix())

    return output


if __name__ == "__main__":
    made = build()
    print(f"Built {made.relative_to(ROOT)}: in Blender, Install from Disk and choose it.")
    sys.exit(0)
