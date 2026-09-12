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
Source: "{#RepoRoot}\dist\{#UpdaterExeName}"; DestDir: "{app}"; Flags: ignoreversion
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
Filename: "{app}\{#UpdaterExeName}"; WorkingDir: "{app}"; Flags: nowait
Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Description: "Launch JARVIS"; Flags: nowait postinstall

[Code]
var
  NamePage: TInputQueryWizardPage;

function MemoryFileName(): String;
begin
  Result := ExpandConstant('{%USERPROFILE}\JARVIS\memory.txt');
end;

procedure InitializeWizard;
begin
  NamePage := CreateInputQueryPage(
    wpWelcome,
    'Personalise JARVIS',
    'What should I call you?',
    'Enter the name JARVIS should use when greeting you.');

  NamePage.Add('&Name:', False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;

  if Assigned(NamePage) and (PageID = NamePage.ID) then
    Result := FileExists(MemoryFileName());
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;

  if (CurPageID = NamePage.ID) and
     (Trim(NamePage.Values[0]) = '') then
  begin
    MsgBox(
      'Please enter a name for JARVIS.',
      mbError,
      MB_OK);

    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  MemoryFile: String;
  MemoryDir: String;
  Lines: TArrayOfString;
begin
  if CurStep <> ssInstall then
    Exit;

  MemoryFile := MemoryFileName();

  // Never overwrite an existing user's memory during an install or update.
  if FileExists(MemoryFile) then
    Exit;

  MemoryDir := ExtractFileDir(MemoryFile);

  if not ForceDirectories(MemoryDir) then
  begin
    MsgBox(
      'JARVIS could not create your user memory folder. '
      + 'The installation will continue, but the personalised greeting '
      + 'could not be saved.',
      mbError,
      MB_OK);

    Exit;
  end;

  SetArrayLength(Lines, 3);
  Lines[0] := '# What JARVIS knows about you. One fact per line.';
  Lines[1] := '# Edit or delete anything here; it is read at startup.';
  Lines[2] := 'name: ' + Trim(NamePage.Values[0]);

  if not SaveStringsToUTF8FileWithoutBOM(MemoryFile, Lines, False) then
  begin
    MsgBox(
      'JARVIS could not save your name. The installation will continue, '
      + 'but the personalised greeting could not be saved.',
      mbError,
      MB_OK);
  end;
end;
