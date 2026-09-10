$ErrorActionPreference = "Stop"

$InstallerDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $InstallerDir
$DistDir = Join-Path $RepoRoot "dist\JARVIS"
$UpdaterExe = Join-Path $RepoRoot "dist\JARVIS-Updater.exe"
$ModelDir = Join-Path $RepoRoot "model"
$VersionFile = Join-Path $RepoRoot "app_version.py"

if (-not (Test-Path $ModelDir -PathType Container)) {
    throw "Missing Vosk model directory: $ModelDir"
}

if (-not (Test-Path $VersionFile -PathType Leaf)) {
    throw "Missing application version file: $VersionFile"
}

$VersionText = Get-Content $VersionFile -Raw
if ($VersionText -notmatch 'VERSION\s*=\s*["'']([^"'']+)["'']') {
    throw "Could not read VERSION from $VersionFile"
}

$AppVersion = $Matches[1]

$Python = (Get-Command python -ErrorAction Stop).Source

& $Python -c "import anthropic, PyInstaller; print('Build dependencies OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies are missing. Install installer\requirements-build.txt and requirements.txt first."
}

$JARVISSpec = Join-Path $InstallerDir "JARVIS.spec"
$UpdaterSpec = Join-Path $InstallerDir "JARVIS-Updater.spec"

$JARVISArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    $JARVISSpec
)

Write-Host "[JARVIS build] Building JARVIS bundle v$AppVersion..."
& $Python @JARVISArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed for JARVIS."
}

if (-not (Test-Path (Join-Path $DistDir "JARVIS.exe") -PathType Leaf)) {
    throw "PyInstaller completed without producing JARVIS.exe."
}

$UpdaterArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    $UpdaterSpec
)

Write-Host "[JARVIS build] Building updater bundle v$AppVersion..."
& $Python @UpdaterArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed for the updater."
}

if (-not (Test-Path $UpdaterExe -PathType Leaf)) {
    throw "PyInstaller completed without producing $UpdaterExe."
}

$IsccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    (Join-Path ${env:ProgramFiles} "Inno Setup 7\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe")
) | Where-Object { $_ -and (Test-Path $_ -PathType Leaf) }

$ISCC = $IsccCandidates | Select-Object -First 1

if (-not $ISCC) {
    throw "Inno Setup 7 compiler not found. Install it before building the installer."
}

Write-Host "[JARVIS build] Building Windows installer v$AppVersion..."
& $ISCC "/DAppVersion=$AppVersion" (Join-Path $InstallerDir "JARVIS.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup build failed."
}

$OutputDir = Join-Path $InstallerDir "output"
$Installer = Join-Path $OutputDir "JARVIS-Setup.exe"

if (-not (Test-Path $Installer -PathType Leaf)) {
    throw "Inno Setup completed without producing $Installer"
}

Write-Host ""
Write-Host "[JARVIS build] Complete: $Installer"
