$ErrorActionPreference = "Stop"

$InstallerDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $InstallerDir
$DistDir = Join-Path $RepoRoot "dist\JARVIS"
$ModelDir = Join-Path $RepoRoot "model"

if (-not (Test-Path $ModelDir -PathType Container)) {
    throw "Missing Vosk model directory: $ModelDir"
}

$Python = (Get-Command python -ErrorAction Stop).Source

& $Python -c "import anthropic, PyInstaller; print('Build dependencies OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies are missing. Install installer\requirements-build.txt and requirements.txt first."
}

$PyInstaller = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    (Join-Path $InstallerDir "JARVIS.spec")
)

Write-Host "[JARVIS build] Building PyInstaller bundle..."
& $Python @PyInstaller
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

if (-not (Test-Path (Join-Path $DistDir "JARVIS.exe") -PathType Leaf)) {
    throw "PyInstaller completed without producing JARVIS.exe."
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

Write-Host "[JARVIS build] Building Windows installer..."
& $ISCC (Join-Path $InstallerDir "JARVIS.iss")
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
