; SP-16 / ST-10-02: Inno Setup script for Nox. Installs the embedded-Python payload produced by
; `installer/build.py` (build/app/ + build/runtime/) into a per-user LOCALAPPDATA location, with a
; Start-menu shortcut and an optional autostart entry. Never touches E:\Nox\vault (or any existing
; vault path); uninstall removes only the program files it installed.
;
; Build with (once Inno Setup is installed):
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\nox.iss
; (or "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" for an all-users install of Inno Setup itself)
; Output: installer\out\Nox-Setup-{#MyAppVersion}.exe (installer\out\ is git-ignored).

#define MyAppName "Nox"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "Nox Project"
#define MyAppExeName "pythonw.exe"
#define BuildAppDir "..\build\app"
#define BuildRuntimeDir "..\build\runtime"

[Setup]
AppId={{6F2F6C6E-2B6E-4B5B-9C2E-3B9E6B2C6A1D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user, no elevation: LOCALAPPDATA install per SP-16 outline / FR-15.4 (no admin rights needed).
DefaultDirName={localappdata}\Programs\Nox
DefaultGroupName=Nox
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=out
OutputBaseFilename=Nox-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; No telemetry, no phone-home ever (FR-15.4) - this installer does not touch the network itself;
; `build.py` already fetched everything it needs before ISCC ever runs.
DisableWelcomePage=no
WizardStyle=modern
SetupIconFile=
UninstallDisplayName={#MyAppName}
ChangesEnvironment=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "autostart"; Description: "Nox beim Anmelden automatisch starten / Start Nox automatically at logon"; GroupDescription: "Autostart:"; Flags: unchecked

[Files]
; The embedded Python runtime (interpreter + stdlib + installed deps from requirements.lock).
Source: "{#BuildRuntimeDir}\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
; nox's own source, config, plugins, and the two built web UIs (pet + dashboard).
Source: "{#BuildAppDir}\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m nox.supervisor"; WorkingDir: "{app}\app"; IconFilename: "{app}\runtime\pythonw.exe"
Name: "{group}\{cm:UninstallProgram,Nox}"; Filename: "{uninstallexe}"
Name: "{userstartup}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m nox.supervisor"; WorkingDir: "{app}\app"; Tasks: autostart

[Run]
; Optional immediate first start after install finishes (unchecked by default - never surprise-run).
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m nox.supervisor"; WorkingDir: "{app}\app"; Description: "{cm:LaunchProgram,Nox}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; Belt-and-braces: only ever delete inside {app} (program files). E:\Nox\vault, E:\Nox\database,
; E:\Nox\data and any user config outside {app} are never named here and are never touched -
; uninstall must leave the vault and all user data exactly as it was (FR-15.4).
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app\__pycache__"

[Code]
{ No vault path is ever written or read by the installer itself - first-start config (vault path,
  name, language, optional Twitch/mic-camera/AI backend) is handled by nox's own first-start flow
  (ST-10-03), not by this script, so the installer stays honest about what it actually configures. }
