; Inno Setup script for Nox. Installs the embedded-Python payload produced by
; `installer/build.py` (build/app/ + build/runtime/) into a per-user LOCALAPPDATA location, with a
; Start-menu shortcut and an optional autostart entry. Never touches the user's vault (or any
; existing vault path); uninstall removes only the program files it installed.
;
; The install itself needs no administrator. One optional step does (task "sandboxrules", ticked by
; default): one UAC prompt for the system's own Windows PowerShell, which adds Windows Filtering
; Platform rules that keep Nox's sandboxed commands off this computer's loopback services, the LAN
; and the internet (docs/SECURITY.md, "Command sandbox on Windows"). That step never runs Nox's own
; Python or any other code from {app}, which the user's account can change: a short bootstrap reads
; the self-contained helper script into memory, checks its SHA-256 against the value pinned into
; this installer at build time (build\sandbox-rules\sandbox_rules.iss), and runs only those bytes.
; The helper keeps a copy in the admin-only "%ProgramFiles%\Nox Sandbox Rules" folder and reports to
; HKLM\SOFTWARE\Nox\SandboxRules. Uninstall removes the rules again (a second UAC prompt) whenever
; any are there - also ones an older build or its command line added. Declining the prompt is fine:
; Nox then reports its sandbox as weaker and runs shell commands only if the user accepts that.
;
; Licenses: this installer bundles the `voice` extra (Piper with espeak-ng, GPL-3.0-or-later), so
; it is distributed under GPL-3.0-or-later as a whole. The wizard shows the notice and written
; source offer (LicenseFile), and LICENSE, NOTICE, COPYING, THIRD_PARTY_LICENSES.md and SOURCE.txt
; are installed into {app} (all collected by build.py into build\legal).
;
; Build with (once Inno Setup is installed):
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\nox.iss
; (or "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" for an all-users install of Inno Setup itself)
; Output: installer\out\Nox-Setup-{#MyAppVersion}.exe (installer\out\ is git-ignored).

#define MyAppName "Nox"
#define MyAppVersion "0.5.0"
#define MyAppPublisher "Nox Project"
#define MyAppExeName "pythonw.exe"
#define BuildAppDir "..\build\app"
#define BuildRuntimeDir "..\build\runtime"
#define BuildLegalDir "..\build\legal"
#define BuildSandboxRulesDir "..\build\sandbox-rules"

[Setup]
AppId={{6F2F6C6E-2B6E-4B5B-9C2E-3B9E6B2C6A1D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user: LOCALAPPDATA install (no admin rights needed). Only the optional sandbox-rules step
; asks for elevation, for itself alone (see the header and [Code]).
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
; No telemetry, no phone-home ever - this installer does not touch the network itself;
; `build.py` already fetched everything it needs before ISCC ever runs.
DisableWelcomePage=no
WizardStyle=modern
SetupIconFile=
UninstallDisplayName={#MyAppName}
ChangesEnvironment=no
; The combined license notice and the written source offer (GPL-3.0 section 6), shown before
; anything is installed.
LicenseFile={#BuildLegalDir}\INSTALLER_LICENSE.txt
; A fresh uninstall log on every install: the log of an older build may hold an [UninstallRun]
; entry that started {app}\runtime\python.exe elevated, and that must never run again.
UninstallLogMode=overwrite

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
; Ticked by default: the first thing somebody does after installing is look for it, and a
; start menu entry alone means knowing it is called Nox before you can find out that it is.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Nox beim Anmelden automatisch starten / Start Nox automatically at logon"; GroupDescription: "Autostart:"; Flags: unchecked
; Ticked by default: without these rules, Nox's sandbox is weaker on Windows builds that do not
; enforce AppContainer loopback isolation, and shell commands stay off until the user accepts that.
Name: "sandboxrules"; Description: "{cm:SandboxRulesTask}"; GroupDescription: "{cm:SandboxRulesGroup}"

[Files]
; The embedded Python runtime (interpreter + stdlib + installed deps from requirements.lock).
Source: "{#BuildRuntimeDir}\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
; nox's own source, config, plugins, and the two built web UIs (pet + dashboard).
Source: "{#BuildAppDir}\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
; The license texts and the written source offer.
Source: "{#BuildLegalDir}\*"; DestDir: "{app}"; Flags: ignoreversion
; The sandbox-rules helper. Only ever run after its SHA-256 matched the pinned one ([Code]).
Source: "{#BuildSandboxRulesDir}\nox-sandbox-rules.ps1"; DestDir: "{app}\sandbox"; Flags: ignoreversion

[Icons]
; Every shortcut starts the launcher, not `-m nox.supervisor`: the launcher puts `{app}\app` on
; `sys.path` itself and reports a failed start (%LOCALAPPDATA%\Nox\startup-error.log and a
; message box) instead of `pythonw` failing silently. Installing over an older version replaces
; these shortcuts, the autostart entry included.
Name: "{group}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\runtime\pythonw.exe"
Name: "{group}\{cm:UninstallProgram,Nox}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\runtime\pythonw.exe"; Tasks: desktopicon
Name: "{userstartup}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; Tasks: autostart

[Run]
; Optional immediate first start after install finishes (unchecked by default - never surprise-run).
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; Description: "{cm:LaunchProgram,Nox}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; The sandbox network rules are removed in [Code] (CurUninstallStepChanged), before this runs.
; Remove the read rights Nox granted its AppContainer sandbox on the runtime folders (recorded in a
; manifest; exactly those ACEs, nothing else). Runs before the runtime itself is deleted.
Filename: "{app}\runtime\python.exe"; Parameters: "-m nox.cli sandbox reset"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "NoxSandboxReset"

[UninstallDelete]
; Belt-and-braces: only ever delete inside {app} (program files). The user's vault, database,
; data folder and any config outside {app} are never named here and are never touched -
; uninstall must leave the vault and all user data exactly as it was.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\sandbox"

[CustomMessages]
english.SandboxRulesGroup=Security:
german.SandboxRulesGroup=Sicherheit:
english.SandboxRulesTask=Keep sandboxed commands away from this computer's local services and the home network (asks for administrator rights once)
german.SandboxRulesTask=Sandbox von lokalen Diensten dieses Computers und vom Heimnetz fernhalten (fragt einmal nach Administratorrechten)
english.SandboxRulesFailed=The sandbox network rules were not added (%1).%n%nNox works without them, but on this Windows build it then runs shell commands only if you accept the weaker sandbox in its settings. To add the rules later, run this setup again.
german.SandboxRulesFailed=Die Netzwerkregeln der Sandbox wurden nicht angelegt (%1).%n%nNox funktioniert auch ohne sie, fuehrt Shell-Befehle auf diesem Windows-Build dann aber nur aus, wenn du die schwaechere Sandbox in den Einstellungen akzeptierst. Spaeter nachholen: dieses Setup erneut ausfuehren.

[Code]
{ No vault path is ever written or read by the installer itself - first-start config (vault path,
  name, language, optional Twitch/mic-camera/AI backend) is handled by nox's own first-start flow,
  not by this script, so the installer stays honest about what it actually configures. }

#include "..\build\sandbox-rules\sandbox_rules.iss"

const
  SandboxStatusKey = 'SOFTWARE\Nox\SandboxRules';

{ The system's own 64-bit Windows PowerShell, by full path (never one found on PATH). }
function SandboxPowerShell(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function SandboxProtectedCopy(): String;
begin
  Result := ExpandConstant('{commonpf64}\Nox Sandbox Rules\nox-sandbox-rules.ps1');
end;

function SandboxAppCopy(): String;
begin
  Result := ExpandConstant('{app}\sandbox\nox-sandbox-rules.ps1');
end;

{ Runs the helper through the bootstrap: elevated ("runas", one UAC prompt) or not. Returns False
  when it could not be started at all (ErrorCode says why). }
function RunSandboxHelper(const Action, Paths: String; Elevated: Boolean; var ErrorCode: Integer): Boolean;
var
  Redirect: Boolean;
begin
  Redirect := EnableFsRedirection(False);
  try
    if Elevated then
      Result := ShellExec('runas', SandboxPowerShell(), SandboxRulesParams(Action, Paths),
        ExpandConstant('{sys}'), SW_HIDE, ewWaitUntilTerminated, ErrorCode)
    else
      Result := Exec(SandboxPowerShell(), SandboxRulesParams(Action, Paths),
        ExpandConstant('{sys}'), SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  finally
    EnableFsRedirection(Redirect);
  end;
end;

function SandboxSequence(): Cardinal;
begin
  if not RegQueryDWordValue(HKLM64, SandboxStatusKey, 'Sequence', Result) then
    Result := 0;
end;

{ The one elevated step. The shell hands back no exit code, so the helper reports to
  HKLM\SOFTWARE\Nox\SandboxRules (only administrators can write there): a new Sequence and
  Code 0 mean every rule is in place. Returns '' on success, otherwise why not. }
function AddSandboxRules(): String;
var
  Before, Code: Cardinal;
  ErrorCode: Integer;
  Message: String;
begin
  Result := '';
  Before := SandboxSequence();
  if not RunSandboxHelper('install', SandboxPsQuote(SandboxAppCopy()), True, ErrorCode) then
  begin
    Result := SysErrorMessage(ErrorCode);
    Exit;
  end;
  if SandboxSequence() = Before then
  begin
    Result := 'no result from the rule setup';
    Exit;
  end;
  if not RegQueryDWordValue(HKLM64, SandboxStatusKey, 'Code', Code) or (Code <> 0) then
  begin
    if not RegQueryStringValue(HKLM64, SandboxStatusKey, 'Message', Message) then
      Message := 'the rule setup failed';
    Result := Message;
  end;
end;

{ Whether Nox's rules may be in place: the helper's folder or status key exists, or the helper -
  not elevated - finds Nox's WFP provider (rules an older build or its command line added). An
  answer it cannot give counts as "maybe". }
function SandboxRulesMayExist(): Boolean;
var
  ErrorCode: Integer;
begin
  Result := FileExists(SandboxProtectedCopy()) or RegKeyExists(HKLM64, SandboxStatusKey);
  if not Result then
    Result := not RunSandboxHelper('status', SandboxPsQuote(SandboxAppCopy()), False, ErrorCode)
      or (ErrorCode <> 1);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Error: String;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('sandboxrules') then
  begin
    Error := AddSandboxRules();
    if Error <> '' then
    begin
      Log('Sandbox network rules not added: ' + Error);
      SuppressibleMsgBox(FmtMessage(CustomMessage('SandboxRulesFailed'), [Error]),
        mbInformation, MB_OK, IDOK);
    end
    else
      Log('Sandbox network rules added');
  end;
end;

{ Uninstall removes the rules whenever they may be there - independent of the task, so rules an
  older build or "nox sandbox firewall install" added go too. It runs the protected copy when its
  SHA-256 matches, else the (equally checked) copy in the install folder; never anything unchecked. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ErrorCode: Integer;
begin
  if (CurUninstallStep = usUninstall) and SandboxRulesMayExist() then
  begin
    if RunSandboxHelper('uninstall', SandboxPsQuote(SandboxProtectedCopy()) + ','
        + SandboxPsQuote(SandboxAppCopy()), True, ErrorCode) then
      Log('Sandbox network rules: removal ran')
    else
      Log('Sandbox network rules not removed: ' + SysErrorMessage(ErrorCode));
  end;
end;
