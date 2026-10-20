; Inno Setup script for Nox. Installs the embedded-Python payload produced by
; `installer/build.py` (build/app/ + build/runtime/) into a per-user LOCALAPPDATA location, with a
; Start-menu shortcut and an optional autostart entry. Never touches the user's vault (or any
; existing vault path); uninstall removes only the program files it installed.
;
; The install itself needs no administrator. One optional step does (task "sandboxrules", ticked by
; default): it starts `python -m nox.sandbox.wfp install` elevated - one UAC prompt - which adds
; Windows Filtering Platform rules that keep Nox's sandboxed commands off this computer's loopback
; services and the LAN (docs/SECURITY.md, "Command sandbox on Windows"). Uninstall removes them
; again (a second UAC prompt). Declining the prompt is fine: Nox then reports its sandbox as weaker
; and runs shell commands only if the user accepts that.
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
; Remove the sandbox network rules (elevated: one UAC prompt). Recorded only when the task was
; ticked at install time. Removes every filter of Nox's WFP provider, its sublayer and the provider.
Filename: "{app}\runtime\python.exe"; Parameters: "-m nox.sandbox.wfp remove"; WorkingDir: "{app}\app"; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; Tasks: sandboxrules; RunOnceId: "NoxSandboxRulesRemove"
; Remove the read rights Nox granted its AppContainer sandbox on the runtime folders (recorded in a
; manifest; exactly those ACEs, nothing else). Runs before the runtime itself is deleted.
Filename: "{app}\runtime\python.exe"; Parameters: "-m nox.cli sandbox reset"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "NoxSandboxReset"

[UninstallDelete]
; Belt-and-braces: only ever delete inside {app} (program files). The user's vault, database,
; data folder and any config outside {app} are never named here and are never touched -
; uninstall must leave the vault and all user data exactly as it was.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app\__pycache__"

[CustomMessages]
english.SandboxRulesGroup=Security:
german.SandboxRulesGroup=Sicherheit:
english.SandboxRulesTask=Keep sandboxed commands away from this computer's local services and the home network (asks for administrator rights once)
german.SandboxRulesTask=Sandbox von lokalen Diensten dieses Computers und vom Heimnetz fernhalten (fragt einmal nach Administratorrechten)
english.SandboxRulesFailed=The sandbox network rules were not added (%1).%n%nNox works without them, but on this Windows build it then runs shell commands only if you accept the weaker sandbox in its settings. To add the rules later, run this setup again or run "nox sandbox firewall install" from an administrator prompt.
german.SandboxRulesFailed=Die Netzwerkregeln der Sandbox wurden nicht angelegt (%1).%n%nNox funktioniert auch ohne sie, fuehrt Shell-Befehle auf diesem Windows-Build dann aber nur aus, wenn du die schwaechere Sandbox in den Einstellungen akzeptierst. Spaeter nachholen: dieses Setup erneut ausfuehren oder "nox sandbox firewall install" in einer Administrator-Eingabeaufforderung.

[Code]
{ No vault path is ever written or read by the installer itself - first-start config (vault path,
  name, language, optional Twitch/mic-camera/AI backend) is handled by nox's own first-start flow,
  not by this script, so the installer stays honest about what it actually configures. }

{ The one elevated step: `python -m nox.sandbox.wfp install`, started with the "runas" verb so
  Windows shows a UAC prompt for this process alone. The shell hands back no exit code, so the
  step writes its result to a status file: "0" on the first line means every rule is in place.
  Returns '' on success, otherwise why not. }
function AddSandboxRules(): String;
var
  StatusFile: String;
  Status: AnsiString;
  FirstLine: String;
  ErrorCode: Integer;
begin
  Result := '';
  StatusFile := ExpandConstant('{tmp}\nox-sandbox-rules.txt');
  DeleteFile(StatusFile);
  if not ShellExec('runas', ExpandConstant('{app}\runtime\python.exe'),
      '-m nox.sandbox.wfp install --status-file "' + StatusFile + '"',
      ExpandConstant('{app}\app'), SW_HIDE, ewWaitUntilTerminated, ErrorCode) then
  begin
    Result := SysErrorMessage(ErrorCode);
    Exit;
  end;
  if not LoadStringFromFile(StatusFile, Status) then
  begin
    Result := 'no result from the rule setup';
    Exit;
  end;
  FirstLine := String(Status);
  if Pos(#10, FirstLine) > 0 then
    FirstLine := Copy(FirstLine, 1, Pos(#10, FirstLine) - 1);
  if Trim(FirstLine) <> '0' then
    Result := Trim(String(Status));
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
