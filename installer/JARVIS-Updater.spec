# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for the JARVIS updater helper."""

from pathlib import Path


ROOT = Path(SPEC).resolve().parent.parent


a = Analysis(
    [str(ROOT / "installer" / "updater_helper.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "packaging.version",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="JARVIS-Updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
)
