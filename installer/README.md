# JARVIS Windows installer

This directory builds a per-user Windows installer from the current JARVIS source tree.

## Prerequisites

- A working JARVIS Python environment with `requirements.txt` installed.
- The local `model\` directory containing the Vosk model. It is intentionally not committed to Git.
- PyInstaller from `requirements-build.txt`.
- Inno Setup 7 with `ISCC.exe` available.

Install the build tool into the active JARVIS environment:

```powershell
python -m pip install -r installer\requirements-build.txt
```

## Build

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

The build produces:

```text
dist\JARVIS\
installer\output\JARVIS-Setup.exe
```

The PyInstaller bundle is deliberately one-folder rather than one-file so
large native and speech-model assets are installed once and reused at runtime.

## Configuration

The installer never includes `.env`, API keys, user memory or other machine-
specific data. It installs `.env.example` beside `JARVIS.exe`.

Copy `.env.example` to `.env` in the installed JARVIS application directory
and fill in the credentials required by the features you use.

The normal JARVIS provider order remains controlled by `LLM_PROVIDER` and the
existing provider configuration; packaging does not select a provider.

## External applications

Blender and Google Chrome are not bundled. JARVIS detects an installed Blender
and the dedicated `JARVIS Chrome` shortcut starts a separate Chrome profile on
localhost port 9222, matching `actions\browser.py`.

Tripo remains an external API integration and needs `TRIPO_API_KEY`.

## Release signing

The generated installer and executable are unsigned by default. A production
release should be Authenticode-signed before distribution so Windows can show a
trusted publisher rather than an unknown-publisher warning.
