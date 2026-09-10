#define AppName "JARVIS"
#ifndef AppVersion
#define AppVersion "0.1.0"
#endif
#define AppPublisher "JARVIS"
#define AppExeName "JARVIS.exe"
#define UpdaterExeName "JARVIS-Updater.exe"
#define RepoRoot ".."

[Setup]
AppId={{8B3DCE1A-2D9E-4F39-9D8A-7D5F1F8A5E51}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\JARVIS
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=JARVIS-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#RepoRoot}\dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\dist\JARVIS-Updater\{#UpdaterExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\installer\jarvis-chrome.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\installer\.env.example"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JARVIS Updater"; ValueData: "{app}\{#UpdaterExeName}"; Flags: uninsdeletevalue

[Icons]
Name: "{group}\JARVIS"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\JARVIS"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\JARVIS Chrome"; Filename: "{app}\jarvis-chrome.bat"; WorkingDir: "{app}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Run]
Filename: "{app}\{UpdaterExeName}"; WorkingDir: "{app}"; Flags: nowait
Filename: "{app}\{AppExeName}"; WorkingDir: "{app}"; Description: "Launch JARVIS"; Flags: nowait postinstall
