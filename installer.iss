; Inno Setup script for Bling Reborn. Build with build.ps1 (it passes /DAppVersion=...).
; Per-user install: no admin rights needed, installs to %LOCALAPPDATA%\Programs\BlingReborn.
;
; Up to 1.1.0 the app was called Blink Sync. The AppId is unchanged so this upgrades that install in
; place (one entry in Settings > Apps); the old folder, shortcuts and startup entry are tidied up.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Bling Reborn"
#define AppExe "BlingReborn.exe"
#define OldAppName "Blink Sync"
#define OldAppExe "BlinkSync.exe"
#define OldRunValue "BlinkSync"
#define RunValue "BlingReborn"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
AppId={{6F3B1E2A-7C4D-4E8B-9A51-2D0C8B7E4F19}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppPublisherURL=https://github.com/shoey79/bling-reborn
AppSupportURL=https://github.com/shoey79/bling-reborn/issues
DefaultDirName={localappdata}\Programs\BlingReborn
UsePreviousAppDir=no
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=BlingReborn-Setup-{#AppVersion}
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
Name: "startup"; Description: "Start {#AppName} when I sign in to Windows"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; clear the previous version's libraries so upgrades don't leave stale files behind
Type: filesandordirs; Name: "{app}\_internal"
; tidy up after Blink Sync (<= 1.1.0): its program folder and shortcuts
Type: filesandordirs; Name: "{localappdata}\Programs\BlinkSync"
Type: files; Name: "{autoprograms}\{#OldAppName}.lnk"
Type: files; Name: "{autodesktop}\{#OldAppName}.lnk"

[Files]
Source: "build\pyinstaller\dist\BlingReborn\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; same value the app's "Start with Windows" setting uses
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#RunValue}"; ValueData: """{app}\{#AppExe}"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[Code]
var
  HadOldStartup: Boolean;

procedure StopApp();
var
  ResultCode: Integer;
begin
  { the tray app has no window to close politely, so end it (and the old Blink Sync) before replacing files }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#OldAppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  HadOldStartup := RegValueExists(HKCU, '{#RunKey}', '{#OldRunValue}');
  StopApp();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { replace Blink Sync's startup entry, keeping the user's choice to start with Windows }
    if HadOldStartup then
    begin
      RegDeleteValue(HKCU, '{#RunKey}', '{#OldRunValue}');
      RegWriteStringValue(HKCU, '{#RunKey}', '{#RunValue}', '"' + ExpandConstant('{app}\{#AppExe}') + '"');
    end;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  StopApp();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Creds, OldCreds: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { also covers the value being set from the app's own settings window }
    RegDeleteValue(HKCU, '{#RunKey}', '{#RunValue}');
    RegDeleteValue(HKCU, '{#RunKey}', '{#OldRunValue}');
    Creds := ExpandConstant('{localappdata}\BlingReborn\credentials.json');
    OldCreds := ExpandConstant('{localappdata}\BlinkSync\credentials.json');
    if (FileExists(Creds) or FileExists(OldCreds)) and not UninstallSilent() then
      if MsgBox('Also remove your saved Blink sign-in?' + #13#10#13#10 +
                'Your settings and downloaded videos are kept either way.',
                mbConfirmation, MB_YESNO) = IDYES then
      begin
        DeleteFile(Creds);
        DeleteFile(OldCreds);
      end;
  end;
end;
