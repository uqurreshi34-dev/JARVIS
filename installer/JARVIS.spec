# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for the Windows JARVIS bundle."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPEC).resolve().parent.parent
MODEL_DIR = ROOT / "model"

if not MODEL_DIR.is_dir():
    raise SystemExit(
        "The Vosk model directory is missing. Expected: "
        f"{MODEL_DIR}"
    )


datas = [
    (str(ROOT / "actions" / "project_setup_recipes.json"), "actions"),
    (str(MODEL_DIR), "model"),
]

hiddenimports = [
    "anthropic",
    "anthropic.lib.foundry",
    "vosk",
    "whisper",
    "pyttsx3.drivers.sapi5",
    "pythoncom",
    "win32api",
    "win32con",
    "win32event",
    "win32gui",
    "win32process",
    "win32timezone",
    "win32com.client",
]

# These packages contain runtime-loaded modules or data that are commonly not
# discovered completely by static import analysis.
for package in (
    "PyQt6",
    "vosk",
    "whisper",
    "anthropic",
    "selenium",
    "pyttsx3",
    "comtypes",
    "pywinauto",
    "docx",
    "reportlab",
):
    try:
        hiddenimports.extend(collect_submodules(package))
        datas.extend(collect_data_files(package))
    except Exception as error:
        print(f"[JARVIS build] optional collection skipped for {package}: {error}")


# The main package graph brings in the application's Python modules. The
# explicit data entries above preserve the files that Python opens directly.
a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
