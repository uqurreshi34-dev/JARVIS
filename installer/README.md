# JARVIS Windows installer

This directory builds a per-user Windows installer from the current JARVIS source tree.

## Prerequisites

- A working JARVIS Python environment with `requirements.txt` installed.
- The local `model\` directory containing the Vosk model. It is intentionally not committed to Git.
- PyInstaller from `requirements-build.txt`.
- Inno Setup 7 with `ISCC.exe` available.

Install the build tools into the active JARVIS environment:

```powershell
python -m pip install -r requirements.txt
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
dist\JARVIS-Updater\
installer\output\JARVIS-Setup.exe
```

The PyInstaller bundle is deliberately one-folder rather than one-file so
large native and speech-model assets are installed once and reused at runtime.

`app_version.py` is the single application-version source. The build script
passes that version into Inno Setup so the packaged application and installer
stay in sync.

## Configuration

The installer never includes `.env`, API keys, user memory or other machine-
specific data. It installs `.env.example` beside `JARVIS.exe`.

Copy `.env.example` to `.env` in the installed JARVIS application directory
and fill in the credentials required by the features you use.

The normal JARVIS provider order remains controlled by `LLM_PROVIDER` and the
existing provider configuration; packaging does not select a provider.

## Automatic updates

The installer includes `JARVIS-Updater.exe` and registers it for the current
Windows user. The updater checks the latest published GitHub Release every six
hours while Windows is running.

Only a newer, non-prerelease GitHub Release is considered. The updater looks
for the `JARVIS-Setup.exe` release asset, verifies its SHA-256 digest against
the digest published by GitHub, then asks the user whether to install it.

When the user accepts, the downloaded installer performs the upgrade in the
same per-user installation directory. Inno Setup closes JARVIS when necessary,
updates the application files, and the installer relaunches JARVIS. User data
stored outside the installation directory is not replaced by the update.

If the user declines a particular version, that version is not prompted again
until a newer release is published.

To publish an update:

1. Bump `VERSION` in `app_version.py`.
2. Build `JARVIS-Setup.exe` with `installer\build.ps1`.
3. Create a published GitHub Release whose tag matches the application version,
   such as `v0.1.1`.
4. Upload the generated `JARVIS-Setup.exe` as a release asset using that exact
   filename.

The updater uses the public GitHub Releases API, so no GitHub token is needed
for public releases.

## External applications

Blender and Google Chrome are not bundled. JARVIS detects an installed Blender
and the dedicated `JARVIS Chrome` shortcut starts a separate Chrome profile on
localhost port 9222, matching `actions\browser.py`.

Tripo remains an external API integration and needs `TRIPO_API_KEY`.

## Release signing

The generated installer and executable are unsigned by default. A production
release should be Authenticode-signed before distribution so Windows can show a
trusted publisher rather than an unknown-publisher warning.
