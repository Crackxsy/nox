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
; Two modes (a wizard page, or /MODE=node for silent installs): the Nox of this home ("core"), or
; a node - this PC as the hands of a Nox elsewhere in the home. A node gets no dashboards, a
; per-user logon task "Nox Node" (least privilege, registered by the user's own Python, never
; elevated, never a service) and its own notification ID; uninstall ends and removes the task.
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
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Check: IsCoreMode
Name: "autostart"; Description: "Nox beim Anmelden automatisch starten / Start Nox automatically at logon"; GroupDescription: "Autostart:"; Flags: unchecked; Check: IsCoreMode
; Ticked by default: without these rules, Nox's sandbox is weaker on Windows builds that do not
; enforce AppContainer loopback isolation, and shell commands stay off until the user accepts that.
; A node runs shell commands in the same sandbox, so it is offered in both modes.
Name: "sandboxrules"; Description: "{cm:SandboxRulesTask}"; GroupDescription: "{cm:SandboxRulesGroup}"

[Files]
; The embedded Python runtime (interpreter + stdlib + installed deps from requirements.lock).
Source: "{#BuildRuntimeDir}\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
; nox's own source, config, plugins and both launchers. The two built web UIs (pet + dashboard)
; only for the core: a node shows no dashboard of its own.
Source: "{#BuildAppDir}\*"; Excludes: "\ui"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildAppDir}\ui\*"; DestDir: "{app}\app\ui"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist; Check: IsCoreMode
; `nox node ...` in a terminal, for the node (pair later, status, log, allow).
Source: "nox-node.cmd"; DestDir: "{app}"; Flags: ignoreversion; Check: IsNodeMode
; The license texts and the written source offer.
Source: "{#BuildLegalDir}\*"; DestDir: "{app}"; Flags: ignoreversion
; The sandbox-rules helper. Only ever run after its SHA-256 matched the pinned one ([Code]).
Source: "{#BuildSandboxRulesDir}\nox-sandbox-rules.ps1"; DestDir: "{app}\sandbox"; Flags: ignoreversion

[Icons]
; Every shortcut starts a launcher, not `-m nox.supervisor`: the launcher puts `{app}\app` on
; `sys.path` itself and reports a failed start (%LOCALAPPDATA%\Nox\startup-error.log and a
; message box) instead of `pythonw` failing silently. Installing over an older version replaces
; these shortcuts, the autostart entry included.
Name: "{group}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\runtime\pythonw.exe"; Check: IsCoreMode
Name: "{group}\{cm:UninstallProgram,Nox}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\runtime\pythonw.exe"; Tasks: desktopicon
Name: "{userstartup}\Nox"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; Tasks: autostart
; Node: "Nox Node" starts the logon task now (never a second copy). Its AppUserModelID is the one
; the node's notifications carry (nox.node.appid), so a toast says "Nox", not "Windows PowerShell".
Name: "{group}\Nox Node"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I ""{app}\app\start_node.pyw"" --start"; WorkingDir: "{app}\app"; IconFilename: "{app}\runtime\pythonw.exe"; AppUserModelID: "Nox.Node"; Check: IsNodeMode

[Registry]
; The node's notification sender, per user (no administrator). The 64-bit view explicitly: the
; WFP status key once landed in WOW6432Node because a 32-bit view was used by accident.
Root: HKCU64; Subkey: "Software\Classes\AppUserModelId\Nox.Node"; ValueType: string; ValueName: "DisplayName"; ValueData: "Nox"; Flags: uninsdeletekey; Check: IsNodeMode
; A core installed over a node: the node's notification ID goes with it.
Root: HKCU64; Subkey: "Software\Classes\AppUserModelId\Nox.Node"; Flags: deletekey dontcreatekey; Check: IsCoreMode

[Run]
; Everything here that runs code from {app} (which the user's account can change) runs as the
; original, non-elevated user (runasoriginaluser) - also when Setup itself was started elevated.
; installer/test: tests/unit/installer/test_install_start.py checks that rule.
; Optional immediate first start after install finishes (unchecked by default - never surprise-run).
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\start_nox.pyw"""; WorkingDir: "{app}\app"; Description: "{cm:LaunchProgram,Nox}"; Flags: nowait postinstall skipifsilent unchecked runasoriginaluser; Check: IsCoreMode
; A core installed over a node: the node's logon task goes (the uninstall log of the node install
; is overwritten by this one, UninstallLogMode=overwrite, so uninstalling could not remove it).
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""Nox Node"""; Flags: runhidden waituntilterminated runasoriginaluser; Check: IsCoreMode
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""Nox Node"" /F"; Flags: runhidden waituntilterminated runasoriginaluser; Check: IsCoreMode
; Node: a new base drops the versions the node installed itself (floor and failure record stay),
; then the per-user logon task ("Nox Node", least privilege, as this user).
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I ""{app}\app\start_node.pyw"" --reset-overlays"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated runasoriginaluser; Check: IsNodeMode
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I ""{app}\app\start_node.pyw"" --autostart on"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated runasoriginaluser; StatusMsg: "{cm:NodeTaskStatus}"; Check: IsNodeMode
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I ""{app}\app\start_node.pyw"" --start"; WorkingDir: "{app}\app"; Description: "{cm:NodeStartNow}"; Flags: nowait postinstall skipifsilent runhidden runasoriginaluser; Check: IsNodeMode

[UninstallRun]
; The sandbox network rules are removed in [Code] (CurUninstallStepChanged), before this runs.
; Node: stop it and remove its logon task first - the system's schtasks, as this user.
; In both modes: a task left by an earlier node install goes too (nothing happens without one).
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""Nox Node"""; Flags: runhidden waituntilterminated; RunOnceId: "NoxNodeEnd"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""Nox Node"" /F"; Flags: runhidden waituntilterminated; RunOnceId: "NoxNodeTask"
; Remove the read rights Nox granted its AppContainer sandbox on the runtime folders (recorded in a
; manifest; exactly those ACEs, nothing else). Runs before the runtime itself is deleted.
Filename: "{app}\runtime\python.exe"; Parameters: "-I -m nox.cli sandbox reset"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "NoxSandboxReset"

[UninstallDelete]
; Belt-and-braces: only ever delete program files. The user's vault, database, data folder and any
; config outside {app} are never named here and are never touched - uninstall must leave the vault
; and all user data exactly as it was. The one exception is program code too: the versions a node
; installed itself from signed bundles; its identity, key and audit trail next to them stay.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\sandbox"
Type: filesandordirs; Name: "{localappdata}\Nox\node\app"

[CustomMessages]
english.SandboxRulesGroup=Security:
german.SandboxRulesGroup=Sicherheit:
english.SandboxRulesTask=Keep sandboxed commands away from this computer's local services and the home network (asks for administrator rights once)
german.SandboxRulesTask=Sandbox von lokalen Diensten dieses Computers und vom Heimnetz fernhalten (fragt einmal nach Administratorrechten)
english.SandboxRulesFailed=The sandbox network rules were not added (%1).%n%nNox works without them, but on this Windows build it then runs shell commands only if you accept the weaker sandbox in its settings. To add the rules later, run this setup again.
german.SandboxRulesFailed=Die Netzwerkregeln der Sandbox wurden nicht angelegt (%1).%n%nNox funktioniert auch ohne sie, fuehrt Shell-Befehle auf diesem Windows-Build dann aber nur aus, wenn du die schwaechere Sandbox in den Einstellungen akzeptierst. Spaeter nachholen: dieses Setup erneut ausfuehren.
english.SandboxRulesUninstallAsk=%1%n%nRemoving them needs administrator rights once: Windows will ask next. Remove Nox's sandbox network rules now?%n%n(If you say no, they stay until you run an administrator prompt with the command shown by "nox sandbox firewall remove".)
german.SandboxRulesUninstallAsk=%1%n%nZum Entfernen braucht es einmal Administratorrechte: Windows fragt gleich danach. Die Netzwerkregeln der Nox-Sandbox jetzt entfernen?%n%n(Bei Nein bleiben sie bestehen, bis du den Befehl von "nox sandbox firewall remove" in einer Administrator-Eingabeaufforderung ausfuehrst.)
english.SandboxRulesUninstallFound=This setup did not add sandbox network rules itself, but Windows still has rules from Nox (from an older version or added by hand).
german.SandboxRulesUninstallFound=Dieses Setup hat selbst keine Netzwerkregeln angelegt, Windows hat aber noch Regeln von Nox (aus einer aelteren Version oder von Hand angelegt).
english.SandboxRulesUninstallUnknown=This setup did not add sandbox network rules itself, and whether Windows still has rules from Nox could not be checked.
german.SandboxRulesUninstallUnknown=Dieses Setup hat selbst keine Netzwerkregeln angelegt, und ob Windows noch Regeln von Nox hat, liess sich nicht pruefen.
english.ModeCaption=How will this PC be used?
german.ModeCaption=Wofuer ist dieser PC?
english.ModeDescription=Nox runs once per home. Other PCs connect to it as nodes.
german.ModeDescription=Nox laeuft einmal pro Zuhause. Weitere PCs verbinden sich als Knoten damit.
english.ModeCore=Nox runs on this PC (the Nox of this home)
german.ModeCore=Nox laeuft auf diesem PC (der Nox dieses Zuhauses)
english.ModeNode=Connect to a Nox in the home network (only this device)
german.ModeNode=Mit einem Nox im Heimnetz verbinden (nur dieses Geraet)
english.PairCaption=Connect to Nox
german.PairCaption=Mit Nox verbinden
english.PairDescription=On the Nox at home: Devices, "Add a PC or laptop". Paste the link shown there, or leave this empty and pair later.
german.PairDescription=Am Nox zu Hause: Geraete, "PC oder Laptop hinzufuegen". Den angezeigten Link hier einfuegen - oder leer lassen und spaeter koppeln.
english.PairPrompt=Pairing link (nox-node://pair?...):
german.PairPrompt=Kopplungs-Link (nox-node://pair?...):
english.PairInvalid=That is not a Nox pairing link. It starts with nox-node://pair? and contains only letters, digits and : / ? = & . - _ %
german.PairInvalid=Das ist kein Nox-Kopplungs-Link. Er beginnt mit nox-node://pair? und enthaelt nur Buchstaben, Ziffern und : / ? = & . - _ %
english.PairFailed=Connecting to Nox did not work (code %1).%n%nPair later in a terminal:%n"%2" pair "<link>"
german.PairFailed=Die Verbindung mit Nox hat nicht geklappt (Code %1).%n%nSpaeter in einem Terminal koppeln:%n"%2" pair "<Link>"
english.NodeTaskStatus=Starting Nox with Windows (for this user only)...
german.NodeTaskStatus=Nox mit Windows starten (nur fuer diesen Benutzer)...
english.NodeStartNow=Start the Nox node now
english.NodeNoAdminInstall=Node mode is installed for one user only, never for all users. Run Setup again without administrator mode.
german.NodeNoAdminInstall=Der Node-Modus wird nur fuer einen Benutzer installiert, nie fuer alle. Setup bitte ohne Administratormodus erneut starten.
english.NodeNotElevated=Node mode never runs with administrator rights. Start Setup normally, not with "Run as administrator".
german.NodeNotElevated=Der Node-Modus laeuft nie mit Administratorrechten. Setup bitte normal starten, nicht mit "Als Administrator ausfuehren".
german.NodeStartNow=Nox-Knoten jetzt starten

[Code]
{ No vault path is ever written or read by the installer itself - first-start config (vault path,
  name, language, optional Twitch/mic-camera/AI backend) is handled by nox's own first-start flow,
  not by this script, so the installer stays honest about what it actually configures. }

#include "..\build\sandbox-rules\sandbox_rules.iss"

const
  SandboxStatusKey = 'SOFTWARE\Nox\SandboxRules';

{ ---- core or node --------------------------------------------------------------------------
  A node is this PC as the hands of a Nox elsewhere in the home (docs/NODES.md): the same program
  files without the dashboards, started at logon by a per-user task "Nox Node" (least privilege,
  never a service, never elevated), its notifications under its own AppUserModelID. Silent
  installs choose with /MODE=node (default /MODE=core) and may pass /PAIR=<link>. }

var
  ModePage: TInputOptionWizardPage;
  PairPage: TInputQueryWizardPage;

function ModeParam(): String;
begin
  Result := Lowercase(ExpandConstant('{param:MODE|core}'));
end;

function IsNodeMode(): Boolean;
begin
  if ModePage <> nil then
    Result := ModePage.SelectedValueIndex = 1
  else
    Result := ModeParam() = 'node';
end;

function IsCoreMode(): Boolean;
begin
  Result := not IsNodeMode();
end;

{ The link reaches the node as one quoted argument: only nox-node://pair? links made of characters
  a link holds are passed on - no quotes, spaces or anything a command line treats specially.
  The node checks the link itself again (nox.node.protocol.PairLink). }
function ValidPairLink(const Link: String): Boolean;
var
  I: Integer;
  C: String;
begin
  Result := (Pos('nox-node://pair?', Link) = 1) and (Length(Link) <= 400);
  if not Result then
    Exit;
  for I := 1 to Length(Link) do
  begin
    C := Copy(Link, I, 1);
    if not (((C >= 'a') and (C <= 'z')) or ((C >= 'A') and (C <= 'Z')) or
        ((C >= '0') and (C <= '9')) or (Pos(C, ':/?=&.-_%') > 0)) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function PairLinkValue(): String;
begin
  Result := '';
  if PairPage <> nil then
    Result := Trim(PairPage.Values[0]);
  if Result = '' then
    Result := Trim(ExpandConstant('{param:PAIR|}'));
end;

procedure InitializeWizard();
begin
  ModePage := CreateInputOptionPage(wpLicense, CustomMessage('ModeCaption'),
    CustomMessage('ModeDescription'), '', True, False);
  ModePage.Add(CustomMessage('ModeCore'));
  ModePage.Add(CustomMessage('ModeNode'));
  if ModeParam() = 'node' then
    ModePage.SelectedValueIndex := 1
  else
    ModePage.SelectedValueIndex := 0;
  PairPage := CreateInputQueryPage(ModePage.ID, CustomMessage('PairCaption'),
    CustomMessage('PairDescription'), '');
  PairPage.Add(CustomMessage('PairPrompt'), False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PairPage <> nil) and (PageID = PairPage.ID) and not IsNodeMode();
end;

{ Node mode never runs elevated: user code (the node, its pairing, its logon task) must not run
  with administrator rights from the user-writable install folder. Refused for an all-users install, and
  when Setup was elevated on purpose while UAC is on (without UAC there is no boundary to cross). }
function NodeModeRefusal(): String;
var
  Lua: Cardinal;
begin
  Result := '';
  if IsAdminInstallMode() then
    Result := CustomMessage('NodeNoAdminInstall')
  else if IsAdmin() and RegQueryDWordValue(HKLM64,
      'SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System', 'EnableLUA', Lua) and
      (Lua <> 0) then
    Result := CustomMessage('NodeNotElevated');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Refusal: String;
begin
  Result := True;
  if (ModePage <> nil) and (CurPageID = ModePage.ID) and IsNodeMode() then
  begin
    Refusal := NodeModeRefusal();
    if Refusal <> '' then
    begin
      MsgBox(Refusal, mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
  if (PairPage <> nil) and (CurPageID = PairPage.ID) and (Trim(PairPage.Values[0]) <> '') and
      not ValidPairLink(Trim(PairPage.Values[0])) then
  begin
    MsgBox(CustomMessage('PairInvalid'), mbError, MB_OK);
    Result := False;
  end;
end;

{ A running node keeps its files open: end it first (the user's own task, as that user). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ErrorCode: Integer;
begin
  Result := '';
  if IsNodeMode() then
  begin
    Result := NodeModeRefusal();
    if Result <> '' then
      Exit;
  end;
  ExecAsOriginalUser(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "Nox Node"', '', SW_HIDE,
    ewWaitUntilTerminated, ErrorCode);
end;

{ Pair right away when a link was given: the node's own `nox node pair`, as this user. }
procedure PairNode();
var
  Link: String;
  Code: Integer;
begin
  Link := PairLinkValue();
  if Link = '' then
    Exit;
  if not ValidPairLink(Link) then
  begin
    Log('Pairing link refused: unexpected characters');
    Exit;
  end;
  { As the original user: never elevated, whoever started Setup. }
  if ExecAsOriginalUser(ExpandConstant('{app}\runtime\python.exe'),
      '-I "' + ExpandConstant('{app}\app\start_node.pyw') + '" --cli node pair "' + Link + '"',
      ExpandConstant('{app}\app'), SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
    Log('Node paired')
  else
  begin
    Log('Node pairing failed: ' + IntToStr(Code));
    { No line may start with "[" here: ISCC would read it as a section tag. }
    SuppressibleMsgBox(FmtMessage(CustomMessage('PairFailed'), [IntToStr(Code),
      ExpandConstant('{app}\nox-node.cmd')]), mbInformation, MB_OK, IDOK);
  end;
end;

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

{ Runs the helper through the bootstrap: elevated ("runas", one UAC prompt) or not, reading the
  helper from First (and, if that does not match the pinned hash, from Second, when given). The
  paths reach the bootstrap only hex-encoded (SandboxPathArg); a path with a character that is not
  allowed (quotes, typographic quotes, dollar, backtick, semicolon, ampersand, control characters)
  is refused before anything starts. Returns False when it could not be started at all (ErrorCode
  says why). }
function RunSandboxHelper(const Action, First, Second: String; Elevated: Boolean; var ErrorCode: Integer): Boolean;
var
  Redirect: Boolean;
  Paths, Params: String;
begin
  Result := False;
  ErrorCode := 123; { ERROR_INVALID_NAME }
  Paths := SandboxPathArg(First);
  if Paths = '' then
    Exit;
  if Second <> '' then
  begin
    if SandboxPathArg(Second) = '' then
      Exit;
    Paths := Paths + ',' + SandboxPathArg(Second);
  end;
  Params := SandboxRulesParams(Action, Paths);
  if Params = '' then
    Exit;
  Redirect := EnableFsRedirection(False);
  try
    if Elevated then
      Result := ShellExec('runas', SandboxPowerShell(), Params,
        ExpandConstant('{sys}'), SW_HIDE, ewWaitUntilTerminated, ErrorCode)
    else
      Result := Exec(SandboxPowerShell(), Params,
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
  if not RunSandboxHelper('install', SandboxAppCopy(), '', True, ErrorCode) then
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

const
  RulesAbsent = 0;    { the helper, not elevated, found no provider of Nox's }
  RulesRecorded = 1;  { the helper's protected folder or status key exists }
  RulesFound = 2;     { no record, but the helper found Nox's provider (an older build, by hand) }
  RulesUnknown = 3;   { no record, and the helper could not say: counts as "maybe" }

{ Whether Nox's rules may be in place - independent of the install task. }
function SandboxRulesState(): Integer;
var
  ErrorCode: Integer;
begin
  if FileExists(SandboxProtectedCopy()) or RegKeyExists(HKLM64, SandboxStatusKey) then
    Result := RulesRecorded
  else if not RunSandboxHelper('status', SandboxAppCopy(), '', False, ErrorCode) then
    Result := RulesUnknown
  else if ErrorCode = 1 then
    Result := RulesAbsent
  else if ErrorCode = 0 then
    Result := RulesFound
  else
    Result := RulesUnknown;
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
  if (CurStep = ssPostInstall) and IsNodeMode() then
    PairNode();
end;

{ Uninstall removes the rules whenever they may be there - independent of the task, so rules an
  older build or "nox sandbox firewall install" added go too. It runs the protected copy when its
  SHA-256 matches, else the (equally checked) copy in the install folder; never anything unchecked.
  Without a record of this setup's own rule step, a UAC prompt would come out of nowhere: the user
  is told why first and may skip it (silent uninstalls remove them). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ErrorCode, State: Integer;
  Why: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  State := SandboxRulesState();
  if State = RulesAbsent then
    Exit;
  if State <> RulesRecorded then
  begin
    if State = RulesFound then
      Why := CustomMessage('SandboxRulesUninstallFound')
    else
      Why := CustomMessage('SandboxRulesUninstallUnknown');
    if SuppressibleMsgBox(FmtMessage(CustomMessage('SandboxRulesUninstallAsk'), [Why]),
        mbConfirmation, MB_YESNO, IDYES) <> IDYES then
    begin
      Log('Sandbox network rules: removal skipped by the user');
      Exit;
    end;
  end;
  if RunSandboxHelper('uninstall', SandboxProtectedCopy(), SandboxAppCopy(), True,
      ErrorCode) then
    Log('Sandbox network rules: removal ran')
  else
    Log('Sandbox network rules not removed: ' + SysErrorMessage(ErrorCode));
end;
