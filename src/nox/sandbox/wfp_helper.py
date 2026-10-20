"""The protected helper that adds and removes the sandbox network rules (`nox.sandbox.wfp`).

Nox installs per user, into a folder the user's account can write. Running Nox's own Python
elevated from there would hand administrator rights to anything that can change a file in it (a
`.pth` file, the standard library zip, a DLL next to `python.exe`). So the one elevated step uses
none of it. Instead:

* `helper_script()` renders a **self-contained** Windows PowerShell 5.1 script: .NET only, no
  cmdlet (so no module autoload from a user-writable module path), no file of Nox's, no input but
  its action. It derives every rule itself, exactly as `nox.sandbox.wfp` does (container names, the
  AppContainer SIDs, the filter keys - a test runs it and compares), and talks to the Base
  Filtering Engine through P/Invoke methods emitted in memory (no compiler, no temporary files).
* The installer starts `%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe` - the
  system's own, by full path, with `-NoProfile` - through the `runas` verb, with a short plain-text
  bootstrap on its command line (`bootstrap`). The bootstrap reads the script into memory, checks
  its SHA-256 against the value pinned into the installer at build time, and runs exactly the bytes
  it checked. The copy it reads may sit in the user-writable install folder: a file changed before
  the check is refused, one changed after it is never read again. (No `-EncodedCommand`: an
  installer starting an encoded PowerShell is what antivirus behaviour blockers look for.)
* `install` first stores those verified bytes in `%ProgramFiles%\\Nox Sandbox Rules\\` with an
  explicit, protected ACL (`PROTECTED_ACL`: Administrators and SYSTEM full control, Users read and
  execute, owner Administrators) and checks the ACL it set. The uninstaller runs *that* copy when
  its SHA-256 still matches the pinned one, else the (equally checked) copy in the install folder.
* The result goes to `HKLM\\SOFTWARE\\Nox\\SandboxRules` (`STATUS_KEY`): only administrators can
  write there, the installer can read it, and no file in the user's temp folder is ever written
  by an elevated process.

`build.py` writes the script and an Inno Setup include with the pinned hash and the command
builder (`inno_include`); `nox.iss` includes it. `nox sandbox firewall install|remove` refuse and
print `admin_command` - the protected copy, for an administrator prompt, by absolute paths that
the Windows API resolves (never `%SystemRoot%`/`%ProgramFiles%`: a process running as the same
user can redirect those through `HKCU\\Environment`, and an administrator pasting the line would
then start that process's program).
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from functools import cache
from pathlib import Path

from nox.sandbox.containers import SHELL_CONTAINER, WORKLOAD_SLOTS
from nox.sandbox.wfp import (
    BLOCK_WEIGHT,
    FORWARDER_PORTS,
    KEY_NAMESPACE,
    LAYERS,
    PERMIT_WEIGHT,
    PORTS_PER_CONTAINER,
    PROVIDER_KEY,
    RULE_IDS,
    SUBLAYER_KEY,
    TCP,
)

__all__ = [
    "HELPER_DIR",
    "HELPER_FILE",
    "MAX_COMMAND_LINE",
    "POWERSHELL_IN_SYSTEM32",
    "PROTECTED_ACL",
    "STATUS_KEY",
    "admin_command",
    "bootstrap",
    "command_params",
    "path_arg",
    "path_is_safe",
    "program_files_dir",
    "system_dir",
    "helper_bytes",
    "helper_script",
    "helper_sha256",
    "inno_include",
]

_TEMPLATE = Path(__file__).with_name("wfp_helper.ps1.template")

#: The folder under `%ProgramFiles%` that holds the protected copy, and the copy's name.
HELPER_DIR = "Nox Sandbox Rules"
HELPER_FILE = "nox-sandbox-rules.ps1"
#: Where the helper reports (under HKEY_LOCAL_MACHINE): admin-write, user-read.
STATUS_KEY = "SOFTWARE\\Nox\\SandboxRules"
#: The only PowerShell the installer ever starts elevated: the system's own, by full path
#: (`{sys}` in Inno, `system_dir()` here).
POWERSHELL_IN_SYSTEM32 = "WindowsPowerShell\\v1.0\\powershell.exe"
#: `FOLDERID_ProgramFiles` (KnownFolders.h).
_FOLDERID_PROGRAM_FILES = uuid.UUID("905e63b6-c1bf-494e-b29c-65b732d3d21a")
#: Windows' defaults - used only where the API is not there (tests on other systems).
_DEFAULT_SYSTEM_DIR = "C:\\Windows\\System32"
_DEFAULT_PROGRAM_FILES = "C:\\Program Files"
#: The explicit ACL of the protected folder (inherited by the copy), as (SID, rights): nothing
#: but administrators and SYSTEM may change it; the folder's inheritance from Program Files is cut.
PROTECTED_ACL: tuple[tuple[str, str], ...] = (
    ("S-1-5-32-544", "FullControl"),  # BUILTIN\Administrators
    ("S-1-5-18", "FullControl"),  # NT AUTHORITY\SYSTEM
    ("S-1-5-32-545", "ReadAndExecute"),  # BUILTIN\Users
)
#: Who may own or write the folder and the copy - checked after the ACL is set.
WRITERS: tuple[str, ...] = ("S-1-5-32-544", "S-1-5-18")
#: CreateProcess's limit on a command line, in characters.
MAX_COMMAND_LINE = 32_767


def _rule_lines() -> str:
    lines = []
    for rule_id, layer, action in RULE_IDS:
        conds = {
            "permit-forwarder-connect-v4": "proto,raddr,rport",
            "permit-forwarder-accept-v4": "proto,laddr,lport",
        }.get(rule_id, "")
        lines.append(f"  '{rule_id}|{layer}|{action}|{conds}'")
    return ",\n".join(lines)


@cache
def helper_script() -> str:
    """The helper's PowerShell source - ASCII, deterministic, built from Nox's own constants."""
    values = {
        "PROVIDER": str(PROVIDER_KEY),
        "SUBLAYER": str(SUBLAYER_KEY),
        "NAMESPACE": KEY_NAMESPACE.hex,
        "PORT_LO": str(FORWARDER_PORTS.start),
        "PER": str(PORTS_PER_CONTAINER),
        "SHELL": SHELL_CONTAINER,
        "SLOTS": str(WORKLOAD_SLOTS),
        "LAYERS": "\n".join(f"  '{name}' = [Guid]'{key}'" for name, key in LAYERS.items()),
        "TCP": str(TCP),
        "RULES": _rule_lines(),
        "PERMIT_W": str(PERMIT_WEIGHT),
        "BLOCK_W": str(BLOCK_WEIGHT),
        "ACL": ", ".join(f"'{sid}={rights}'" for sid, rights in PROTECTED_ACL),
        "WRITERS": ", ".join(f"'{sid}'" for sid in WRITERS),
        "DIR": HELPER_DIR,
        "FILE": HELPER_FILE,
        "REGKEY": STATUS_KEY,
        "REGPARENT": STATUS_KEY.rsplit("\\", 1)[0],
    }
    text = _TEMPLATE.read_text(encoding="ascii")
    for name, value in values.items():
        text = text.replace(f"@@{name}@@", value)
    if "@@" in text:
        raise RuntimeError("wfp_helper: a placeholder of the template has no value")
    # One line ending everywhere: the bytes - and so the pinned hash - never depend on checkout.
    return text.replace("\r\n", "\n")


def helper_bytes() -> bytes:
    """The script as the installer pins and stores it: ASCII, LF line endings, no BOM."""
    return helper_script().encode("ascii")


def helper_sha256() -> str:
    """The SHA-256 the installer pins (upper-case hex, as .NET's `BitConverter` prints it)."""
    return hashlib.sha256(helper_bytes()).hexdigest().upper()


#: The fixed switches in front of the bootstrap.
_SWITCHES = "-NoProfile -NonInteractive -WindowStyle Hidden -Command "
#: The bootstrap the installer runs with the system's PowerShell: read the helper into memory,
#: check its SHA-256 against the pinned one, then run exactly the bytes it checked (passing them on,
#: so `install` stores those very bytes). A file that changes after the check changes nothing.
#: No value is ever embedded as PowerShell source: each path arrives as the hex digits of its
#: UTF-16 code units (`path_arg`) and is decoded by the script, so no character of a path - an
#: apostrophe, a typographic quote PowerShell also reads as one, `$`, a backtick - can end a string
#: literal in this elevated process. Single quotes only around the hex; the whole bootstrap sits
#: inside one pair of double quotes on the command line.
#:
#: A 32-bit Setup can end up starting the 32-bit PowerShell (WOW64 redirects `System32` for it, and
#: the elevation path does not honour Setup's own redirection switch). The WFP structures and the
#: status key are 64-bit, so such a PowerShell first restarts this very command line in the 64-bit
#: one, through the `Sysnative` alias of the Windows folder (from the API, not `%windir%`) - it is
#: already elevated, so no second prompt - and hands back its exit code.
_RELAUNCH_64 = (
    "if([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess){{"
    "$a=[Environment]::GetCommandLineArgs();"
    "$s=[Diagnostics.ProcessStartInfo]::new([IO.Path]::Combine("
    "[Environment]::GetFolderPath('Windows'),'Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe'),"
    "'" + _SWITCHES + "'+[char]34+$a[$a.Length-1]+[char]34);"
    "$s.UseShellExecute=$false;$q=[Diagnostics.Process]::Start($s);$q.WaitForExit();"
    "exit $q.ExitCode}};"
)
_BOOTSTRAP = (
    "$ErrorActionPreference='Stop';" + _RELAUNCH_64 + "$b=$null;"
    "foreach($x in @({paths})){{"
    "$f='';for($i=0;$i -lt $x.Length;$i+=4){{$f+=[char][Convert]::ToInt32($x.Substring($i,4),16)}};"
    "try{{$c=[IO.File]::ReadAllBytes($f);"
    "if([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($c))"
    ".Replace('-','') -eq '{sha}'){{$b=$c;break}}}}catch{{}}}};"
    "if($null -eq $b){{[Console]::Error.WriteLine('no helper matches its pinned SHA-256');exit 3}};"
    "& ([ScriptBlock]::Create([Text.Encoding]::ASCII.GetString($b))) '{action}' $b"
)
#: The actions a bootstrap may name - fixed words, never data.
ACTIONS = ("install", "uninstall", "status")
#: Characters a helper path may never contain (defence in depth - the hex encoding already keeps
#: every character out of the source): ASCII quotes, `$`, backtick, `;`, `&`, and the typographic
#: quotes U+2018-U+201F, which PowerShell treats as single and double quotes. Control characters
#: are refused too. Letters outside ASCII (a user name like "Jürgen") stay allowed.
UNSAFE_PATH_CHARS = "'\"`$;&" + "".join(chr(c) for c in range(0x2018, 0x2020))


def path_is_safe(path: str) -> bool:
    """Whether `path` may be handed to the elevated bootstrap at all."""
    return bool(path) and not any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch in UNSAFE_PATH_CHARS for ch in path
    )


def path_arg(path: str) -> str:
    """`path` for the bootstrap: the hex digits of its UTF-16 code units, single-quoted."""
    if not path_is_safe(path):
        raise ValueError(f"not a usable helper path: {path!r}")
    units = path.encode("utf-16-le")
    return (
        "'"
        + "".join(
            f"{int.from_bytes(units[i : i + 2], 'little'):04X}" for i in range(0, len(units), 2)
        )
        + "'"
    )


def bootstrap(action: str, paths: list[str]) -> str:
    """The PowerShell the installer runs: the first of `paths` whose bytes match the pinned
    SHA-256 is run with `action`. Install passes the copy in the install folder; uninstall the
    protected copy first and the install folder's second (both checked, in memory)."""
    if action not in ACTIONS:
        raise ValueError(f"no bootstrap for {action!r}")
    if not paths:
        raise ValueError("no helper path")
    return _BOOTSTRAP.format(
        paths=",".join(path_arg(p) for p in paths), sha=helper_sha256(), action=action
    )


def command_params(action: str, paths: list[str]) -> str:
    """The parameters for the system's `powershell.exe`: fixed switches and the bootstrap."""
    params = f'{_SWITCHES}"{bootstrap(action, paths)}"'
    if 260 + 3 + len(params) >= MAX_COMMAND_LINE:  # MAX_PATH for the quoted powershell.exe
        raise RuntimeError("wfp_helper: the command line is too long")
    return params


def _pascal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def inno_include() -> str:
    """The `[Code]` include `nox.iss` pulls in: the pinned hash and the command builder.

    `SandboxPathArg` is `path_arg` in Pascal (empty for an unsafe path - the caller refuses), and
    `SandboxRulesParams(Action, Paths)` builds the same text as `command_params` from the
    comma-separated `SandboxPathArg` values (empty for an action that is not one of `ACTIONS`)."""
    head, tail = _BOOTSTRAP.split("{paths}")
    before_sha, after_sha = tail.split("{sha}")
    before_action, after_action = after_sha.split("{action}")

    def unbrace(text: str) -> str:
        return text.replace("{{", "{").replace("}}", "}")

    unsafe = " or ".join(f"(C = {ord(ch)})" for ch in UNSAFE_PATH_CHARS)
    actions = " and ".join(f"(Action <> '{a}')" for a in ACTIONS)
    lines = [
        "{ Generated by nox.sandbox.wfp_helper (installer/build.py) - do not edit. }",
        "",
        "function SandboxHelperSha256(): String;",
        "begin",
        f"  Result := '{helper_sha256()}';",
        "end;",
        "",
        "function SandboxPathSafe(const S: String): Boolean;",
        "var",
        "  I, C: Integer;",
        "begin",
        "  Result := Length(S) > 0;",
        "  for I := 1 to Length(S) do",
        "  begin",
        "    C := Ord(S[I]);",
        f"    if (C < 32) or (C = 127) or {unsafe} then",
        "    begin",
        "      Result := False;",
        "      Exit;",
        "    end;",
        "  end;",
        "end;",
        "",
        "function SandboxPathArg(const S: String): String;",
        "var",
        "  I: Integer;",
        "begin",
        "  Result := '';",
        "  if not SandboxPathSafe(S) then",
        "    Exit;",
        "  for I := 1 to Length(S) do",
        "    Result := Result + Format('%.4X', [Ord(S[I])]);",
        "  Result := '''' + Result + '''';",
        "end;",
        "",
        "function SandboxRulesParams(const Action, Paths: String): String;",
        "begin",
        "  Result := '';",
        f"  if {actions} then",
        "    Exit;",
        f"  Result := {_pascal(_SWITCHES)} + '\"' + {_pascal(unbrace(head))} + Paths",
        f"    + {_pascal(unbrace(before_sha))} + SandboxHelperSha256()",
        f"    + {_pascal(unbrace(before_action))} + Action",
        f"    + {_pascal(unbrace(after_action))} + '\"';",
        "end;",
        "",
    ]
    return "\r\n".join(lines)


def system_dir() -> str:
    """The system folder from `GetSystemDirectoryW` - not `%SystemRoot%`, which the user's own
    environment (`HKCU\\Environment`) can point elsewhere."""
    import ctypes  # noqa: PLC0415

    windll = getattr(ctypes, "windll", None)
    if sys.platform != "win32" or windll is None:
        return _DEFAULT_SYSTEM_DIR
    buf = ctypes.create_unicode_buffer(32_768)
    size = windll.kernel32.GetSystemDirectoryW(buf, len(buf))
    if not 0 < size < len(buf):
        raise OSError("GetSystemDirectoryW failed")
    return str(buf.value)


def program_files_dir() -> str:
    """`FOLDERID_ProgramFiles` from `SHGetKnownFolderPath` - not `%ProgramFiles%`."""
    import ctypes  # noqa: PLC0415

    windll = getattr(ctypes, "windll", None)
    if sys.platform != "win32" or windll is None:
        return _DEFAULT_PROGRAM_FILES
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(_FOLDERID_PROGRAM_FILES.bytes_le)
    out = ctypes.c_void_p()
    hr = windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        path = ctypes.wstring_at(out.value) if hr == 0 and out.value else ""
    finally:
        windll.ole32.CoTaskMemFree(out)
    if not path:
        raise OSError(f"SHGetKnownFolderPath failed (0x{hr & 0xFFFFFFFF:08X})")
    return path


def admin_command(action: str) -> str:
    """What to run from an administrator prompt: the protected copy, by the system's PowerShell -
    both by absolute paths the Windows API resolved, no environment variable in the line."""
    powershell = f"{system_dir()}\\{POWERSHELL_IN_SYSTEM32}"
    helper = f"{program_files_dir()}\\{HELPER_DIR}\\{HELPER_FILE}"
    return f'"{powershell}" -NoProfile -ExecutionPolicy Bypass -File "{helper}" {action}'


def write_build_files(out_dir: Path) -> tuple[Path, Path]:
    """Write the helper script and the Inno include into `out_dir` (what `build.py` calls)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    script = out_dir / HELPER_FILE
    script.write_bytes(helper_bytes())
    include = out_dir / "sandbox_rules.iss"
    include.write_bytes(inno_include().encode("ascii"))
    return script, include
