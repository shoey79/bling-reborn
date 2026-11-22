; Inno Setup script for Blink Sync. Build with build.ps1 (it passes /DAppVersion=...).
; Per-user install: no admin rights needed, installs to %LOCALAPPDATA%\Programs\BlinkSync.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Blink Sync"
#define AppExe "BlinkSync.exe"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
AppId={{6F3B1E2A-7C4D-4E8B-9A51-2D0C8B7E4F19}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\BlinkSync
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=BlinkSync-Setup-{#AppVersion}
SetupIconFile=build\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no

[Tasks]
Name: "startup"; Description: "Start Blink Sync when I sign in to Windows"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; clear the previous version's libraries so upgrades don't leave stale files behind
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "build\pyinstaller\dist\BlinkSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; same value the app's "Start with Windows" setting uses
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "BlinkSync"; ValueData: """{app}\{#AppExe}"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start Blink Sync now"; Flags: nowait postinstall skipifsilent

[Code]
procedure StopApp();
var
  ResultCode: Integer;
begin
  { the tray app has no window to close politely, so end it before replacing its files }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopApp();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Creds: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { also covers the value being set from the app's own settings window }
    RegDeleteValue(HKCU, '{#RunKey}', 'BlinkSync');
    Creds := ExpandConstant('{localappdata}\BlinkSync\credentials.json');
    if FileExists(Creds) and not UninstallSilent() then
      if MsgBox('Also remove your saved Blink sign-in?' + #13#10#13#10 +
                'Your settings and downloaded videos are kept either way.',
                mbConfirmation, MB_YESNO) = IDYES then
        DeleteFile(Creds);
  end;
end;
