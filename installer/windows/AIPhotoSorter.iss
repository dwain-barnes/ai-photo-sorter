#define MyAppName "AI Photo Sorter"
#define MyAppVersion "0.2.2"
#define MyAppPublisher "Local AI Tools"
#define MyAppExeName "AIPhotoSorter.exe"

[Setup]
AppId={{E5B6C632-B3C0-49B0-A85C-16F575DA76F4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\AI Photo Sorter
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\release
OutputBaseFilename=AIPhotoSorterSetup-0.2.2
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\AIPhotoSorter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "setup_local_ai_windows.ps1"; DestDir: "{app}\setup"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\*"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent; Check: ShouldLaunchApp

[Code]
var
  LocalAIReady: Boolean;

procedure KillRunningApp;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM AIPhotoSorter.exe /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeSetup: Boolean;
begin
  KillRunningApp;
  Result := True;
end;

function PrepareLocalAI: Boolean;
var
  ResultCode: Integer;
  PowerShell: String;
  ScriptPath: String;
  Args: String;
  LogPath: String;
begin
  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{app}\setup\setup_local_ai_windows.ps1');
  LogPath := ExpandConstant('{userappdata}\..\..\.ai_photo_sorter\llama_cpp\installer_setup.log');
  Args := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '"';

  WizardForm.StatusLabel.Caption :=
    'Preparing local AI. The model download is about 5.8 GB and can take a while.';

  if not Exec(PowerShell, Args, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('AI Photo Sorter was installed, but local AI setup could not be started.' + #13#10 + #13#10 +
      'Run this installer again to retry the automatic local AI setup.',
      mbError, MB_OK);
    Result := False;
    exit;
  end;

  if ResultCode <> 0 then
  begin
    MsgBox('AI Photo Sorter was installed, but local AI setup did not complete.' + #13#10 + #13#10 +
      'Setup log:' + #13#10 + LogPath + #13#10 + #13#10 +
      'Run this installer again to retry the automatic local AI setup.',
      mbError, MB_OK);
    Result := False;
    exit;
  end;

  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    LocalAIReady := PrepareLocalAI;
  end;
end;

function ShouldLaunchApp: Boolean;
begin
  Result := LocalAIReady;
end;
