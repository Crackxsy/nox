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
print `admin_command` - the protected copy, for an administrator prompt.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from nox.sandbox.containers import SHELL_CONTAINER, WORKLOAD_SLOTS
from nox.sandbox.wfp import (
    BLOCK_WEIGHT,
    FORWARDER_PORTS,
    KEY_NAMESPACE,
    LAYERS,
    PERMIT_WEIGHT,
    PROVIDER_KEY,
    RULE_IDS,
    SUBLAYER_KEY,
    TCP,
)

__all__ = [
    "HELPER_DIR",
    "HELPER_FILE",
    "MAX_COMMAND_LINE",
    "POWERSHELL",
    "PROTECTED_ACL",
    "STATUS_KEY",
    "admin_command",
    "bootstrap",
    "command_params",
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
#: The only PowerShell the installer ever starts elevated: the system's own, by full path.
POWERSHELL = "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
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
        "PORT_HI": str(FORWARDER_PORTS.stop - 1),
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


#: The bootstrap the installer runs with the system's PowerShell: read the helper into memory,
#: check its SHA-256 against the pinned one, then run exactly the bytes it checked (passing them on,
#: so `install` stores those very bytes). A file that changes after the check changes nothing.
#: Single quotes only - the whole bootstrap sits inside one pair of double quotes on the command
#: line - and `{path}` is a single-quoted PowerShell string (any `'` in it doubled).
_BOOTSTRAP = (
    "$ErrorActionPreference='Stop';"
    "$b=$null;"
    "foreach($f in @({paths})){{"
    "try{{$c=[IO.File]::ReadAllBytes($f);"
    "if([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($c))"
    ".Replace('-','') -eq '{sha}'){{$b=$c;break}}}}catch{{}}}};"
    "if($null -eq $b){{[Console]::Error.WriteLine('no helper matches its pinned SHA-256');exit 3}};"
    "& ([ScriptBlock]::Create([Text.Encoding]::ASCII.GetString($b))) '{action}' $b"
)
#: The fixed switches in front of the bootstrap.
_SWITCHES = "-NoProfile -NonInteractive -WindowStyle Hidden -Command "


def ps_quote(text: str) -> str:
    """`text` as a single-quoted PowerShell string literal."""
    return "'" + text.replace("'", "''") + "'"


def bootstrap(action: str, paths: list[str]) -> str:
    """The PowerShell the installer runs: the first of `paths` whose bytes match the pinned
    SHA-256 is run with `action`. Install passes the copy in `{app}`; uninstall passes the
    protected copy first and the one in `{app}` second (both checked, in memory)."""
    if action not in ("install", "uninstall", "status"):
        raise ValueError(f"no bootstrap for {action!r}")
    for path in paths:
        if '"' in path or not path:
            raise ValueError(f"not a usable path: {path!r}")
    return _BOOTSTRAP.format(
        paths=",".join(ps_quote(p) for p in paths), sha=helper_sha256(), action=action
    )


def command_params(action: str, paths: list[str]) -> str:
    """The parameters for the system's `powershell.exe`: fixed switches and the bootstrap."""
    params = f'{_SWITCHES}"{bootstrap(action, paths)}"'
    if len(POWERSHELL) + 3 + len(params) >= MAX_COMMAND_LINE:
        raise RuntimeError("wfp_helper: the command line is too long")
    return params


def _pascal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def inno_include() -> str:
    """The `[Code]` include `nox.iss` pulls in: the pinned hash and the command builder.

    `SandboxRulesParams(Action, Paths)` builds the same text as `command_params` - `Paths` is
    the already-quoted, comma-separated list (`SandboxPsQuote` quotes one path)."""
    head, tail = _BOOTSTRAP.split("{paths}")
    before_sha, after_sha = tail.split("{sha}")
    before_action, after_action = after_sha.split("{action}")
    piece = {
        "head": head.replace("{{", "{").replace("}}", "}"),
        "mid": before_sha.replace("{{", "{").replace("}}", "}"),
        "mid2": before_action.replace("{{", "{").replace("}}", "}"),
        "tail": after_action.replace("{{", "{").replace("}}", "}"),
    }
    lines = [
        "{ Generated by nox.sandbox.wfp_helper (installer/build.py) - do not edit. }",
        "",
        "function SandboxHelperSha256(): String;",
        "begin",
        f"  Result := '{helper_sha256()}';",
        "end;",
        "",
        "function SandboxPsQuote(const S: String): String;",
        "var",
        "  T: String;",
        "begin",
        "  T := S;",
        "  StringChangeEx(T, '''', '''''', True);",
        "  Result := '''' + T + '''';",
        "end;",
        "",
        "function SandboxRulesParams(const Action, Paths: String): String;",
        "begin",
        f"  Result := {_pascal(_SWITCHES)} + '\"' + {_pascal(piece['head'])} + Paths",
        f"    + {_pascal(piece['mid'])} + SandboxHelperSha256()",
        f"    + {_pascal(piece['mid2'])} + Action + {_pascal(piece['tail'])} + '\"';",
        "end;",
        "",
    ]
    return "\r\n".join(lines)


def admin_command(action: str) -> str:
    """What to run from an administrator prompt: the protected copy, by the system's PowerShell."""
    return (
        f"{POWERSHELL} -NoProfile -ExecutionPolicy Bypass -File "
        f'"%ProgramFiles%\\{HELPER_DIR}\\{HELPER_FILE}" {action}'
    )


def write_build_files(out_dir: Path) -> tuple[Path, Path]:
    """Write the helper script and the Inno include into `out_dir` (what `build.py` calls)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    script = out_dir / HELPER_FILE
    script.write_bytes(helper_bytes())
    include = out_dir / "sandbox_rules.iss"
    include.write_bytes(inno_include().encode("ascii"))
    return script, include
