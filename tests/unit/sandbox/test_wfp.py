"""The Windows sandbox network rules (`nox.sandbox.wfp`), the protected helper that installs them
(`nox.sandbox.wfp_helper`), the installer around it, and the probe that decides "strong".

Runs on every platform: the rules are pure data and the helper is generated text. On Windows one
test runs the helper's `show` and compares it with Nox's own rules. Nothing here adds a real filter
or needs an administrator - what a real installation does is the manual check in docs/SECURITY.md.
"""

from __future__ import annotations

import ctypes
import hashlib
import ipaddress
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from nox.sandbox import wfp, wfp_helper
from nox.sandbox.containers import (
    SHELL_CONTAINER,
    WORKLOAD_SLOTS,
    all_container_names,
    container_sid,
    slot_container_name,
)
from nox.sandbox.model import Result
from nox.sandbox.wfp import (
    FORWARDER_PORTS,
    LocalAddress,
    LocalPortRange,
    Package,
    PortRange,
    Protocol,
    Remote,
    all_rules,
    rules_for,
    sid_bytes,
)

REPO = Path(__file__).resolve().parents[3]
#: What `DeriveAppContainerSidFromAppContainerName("Nox.Sandbox")` returns on Windows.
SHELL_SID = "S-1-15-2-1894388580-1009738779-1695386769-852024268-3913880356-3155751965-703058514"


# ---- containers and SIDs ---------------------------------------------------------------------


def test_container_sids_are_derived_exactly_like_windows_does() -> None:
    assert container_sid(SHELL_CONTAINER) == SHELL_SID
    assert container_sid("NOX.SANDBOX") == SHELL_SID  # Windows lower-cases the name first
    assert container_sid(slot_container_name(0)) != SHELL_SID


@pytest.mark.skipif(sys.platform != "win32", reason="compares with the Windows API")
def test_container_sids_match_the_windows_api() -> None:
    from ctypes import wintypes

    userenv = ctypes.WinDLL("userenv")  # type: ignore[attr-defined,unused-ignore]
    advapi = ctypes.WinDLL("advapi32")  # type: ignore[attr-defined,unused-ignore]
    for name in (SHELL_CONTAINER, slot_container_name(0), slot_container_name(WORKLOAD_SLOTS - 1)):
        sid = ctypes.c_void_p()
        # Derives the SID only; no profile is created.
        assert userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid)) == 0
        text = wintypes.LPWSTR()
        assert advapi.ConvertSidToStringSidW(sid, ctypes.byref(text))
        assert text.value == container_sid(name)
        assert ctypes.string_at(sid, len(sid_bytes(text.value))) == sid_bytes(text.value)


def test_the_pool_is_fixed_and_every_name_is_a_valid_appcontainer_name() -> None:
    names = all_container_names()
    assert names[0] == SHELL_CONTAINER == "Nox.Sandbox"
    assert names[1:3] == ("Nox.Sandbox.w000", "Nox.Sandbox.w001")
    assert names[-1] == "Nox.Sandbox.w127"
    assert len(names) == len(set(names)) == WORKLOAD_SLOTS + 1
    assert all(len(n) <= 64 for n in names)
    assert len({container_sid(n) for n in names}) == len(names)
    with pytest.raises(ValueError):
        slot_container_name(WORKLOAD_SLOTS)


def test_sid_bytes_is_the_binary_sid_windows_expects() -> None:
    assert sid_bytes("S-1-5-18") == bytes([1, 1, 0, 0, 0, 0, 0, 5, 18, 0, 0, 0])
    blob = sid_bytes(SHELL_SID)
    # Eight sub-authorities: 2 (the AppContainer kind) and the seven from the hash.
    assert blob[:8] == bytes([1, 8, 0, 0, 0, 0, 0, 15]) and len(blob) == 8 + 4 * 8
    assert int.from_bytes(blob[8:12], "little") == 2
    assert int.from_bytes(blob[-4:], "little") == 703058514
    for bad in ("", "S-1", "X-1-5-18", "S-1-5-99999999999", "S-1-5-" + "-".join(["1"] * 16)):
        with pytest.raises(ValueError):
            sid_bytes(bad)


# ---- the rules: an allowlist by construction ---------------------------------------------------


def test_each_container_gets_a_hard_block_of_everything_and_one_forwarder_exception() -> None:
    rules = rules_for(SHELL_CONTAINER)
    pkg = Package(SHELL_SID)
    by_id = {r.name.rsplit(": ", 1)[1]: r for r in rules}
    assert list(by_id) == [rule_id for rule_id, _, _ in wfp.RULE_IDS]

    connect = by_id["permit-forwarder-connect-v4"]
    assert (connect.layer, connect.action, connect.weight, connect.hard) == (
        "ALE_AUTH_CONNECT_V4",
        "PERMIT",
        15,
        False,
    )
    assert connect.conditions == (
        pkg,
        Protocol(6),
        Remote(ipaddress.IPv4Network("127.0.0.1/32")),
        PortRange(47_100, 47_103),  # shell's own block, nobody else's
    )
    accept = by_id["permit-forwarder-accept-v4"]
    assert accept.layer == "ALE_AUTH_RECV_ACCEPT_V4" and accept.action == "PERMIT"
    assert accept.conditions == (
        pkg,
        Protocol(6),
        LocalAddress(ipaddress.IPv4Network("127.0.0.1/32")),
        LocalPortRange(47_100, 47_103),
    )
    blocks = [r for r in rules if r.action == "BLOCK"]
    # Every layer Nox filters gets a block of everything: nothing but the package is matched, so
    # no address family, range or tunnel (IPv4-mapped, NAT64, 6to4, Teredo, the LAN, the internet)
    # is left out by a missing list entry.
    assert {r.layer for r in blocks} == set(wfp.LAYERS)
    for block in blocks:
        assert block.conditions == (pkg,)
        assert block.hard and block.weight < connect.weight
    # The only permits are the two forwarder rules, both IPv4 loopback in the forwarder range.
    assert [r for r in rules if r.action == "PERMIT"] == [connect, accept]


def test_the_forwarder_range_is_below_the_dynamic_ports_and_open_only_to_tcp() -> None:
    assert FORWARDER_PORTS == range(47_100, 47_616) and len(FORWARDER_PORTS) == 4 * 129
    assert FORWARDER_PORTS.stop <= 49_152  # Windows' default dynamic range starts here


def test_each_container_can_reach_only_its_own_forwarder_ports() -> None:
    # Workload A can never even connect to workload B's forwarder: the permits name A's block.
    blocks = [wfp.container_ports(n) for n in all_container_names()]
    assert blocks[0] == range(47_100, 47_104)
    assert blocks[-1] == range(47_612, 47_616)
    seen: set[int] = set()
    for block in blocks:
        assert len(block) == wfp.PORTS_PER_CONTAINER and not seen & set(block)
        seen |= set(block)
    assert seen == set(FORWARDER_PORTS)
    a, b = slot_container_name(0), slot_container_name(1)
    a_ports = {
        port
        for rule in rules_for(a)
        for cond in rule.conditions
        if isinstance(cond, (PortRange, LocalPortRange))
        for port in range(cond.low, cond.high + 1)
    }
    assert a_ports == set(wfp.container_ports(a))
    assert not a_ports & set(wfp.container_ports(b))
    assert wfp.ports_by_sid()[container_sid(b)] == wfp.container_ports(b)
    with pytest.raises(ValueError):
        wfp.container_ports("Someone.Else")


def test_every_container_of_the_pool_is_covered_with_fixed_keys() -> None:
    rules = all_rules()
    assert len(rules) == 6 * (WORKLOAD_SLOTS + 1) == 774
    assert len({r.key for r in rules}) == len(rules)
    sids = {c.sid for r in rules for c in r.conditions if isinstance(c, Package)}
    assert sids == {container_sid(n) for n in all_container_names()}
    # Keys are what uninstall deletes by: they must never change between builds.
    assert wfp.filter_key("Nox.Sandbox", "block-all-connect-v4") == uuid.uuid5(
        uuid.UUID("ab693b13-a6ef-48c4-8c8c-b27790b328e1"), "Nox.Sandbox|block-all-connect-v4"
    )
    assert all_rules() == rules  # deterministic


def test_show_lists_one_line_per_filter(capsys: pytest.CaptureFixture[str]) -> None:
    assert wfp.main(["show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 774
    assert lines[0].split()[1:4] == ["ALE_AUTH_CONNECT_V4", "PERMIT", "w=15"]
    assert f"package={SHELL_SID}" in lines[0] and "remote-port=47100-47103" in lines[0]
    assert "local-port=47100-47103" in lines[1]
    assert "remote-port=47104-47107" in lines[6]  # the next container's own block
    assert lines[2].endswith(f"BLOCK (hard) w=1 package={SHELL_SID}")


# ---- Nox's own Python never changes the rules ----------------------------------------------------


def test_usage_errors_and_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert wfp.main([]) == 2
    assert wfp.main(["install", "extra"]) == 2
    assert wfp.main(["install", "--status-file", "x"]) == 2
    assert wfp.main(["purge"]) == 2
    monkeypatch.setattr(sys, "platform", "linux")
    assert wfp.main(["install"]) == 3


@pytest.mark.parametrize("action", ["install", "remove"])
def test_install_refuses_when_elevated_from_the_user_writable_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wfp, "is_elevated", lambda: True)
    assert wfp.main([action]) == wfp.REFUSED
    err = capsys.readouterr().err
    assert "refusing to run as administrator" in err
    # It points at the protected helper, run by the system's PowerShell.
    assert "%ProgramFiles%\\Nox Sandbox Rules\\nox-sandbox-rules.ps1" in err
    assert err.rstrip().endswith(f" {action}")
    assert "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" in err


def test_install_without_administrator_names_the_helper_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wfp, "is_elevated", lambda: False)
    assert wfp.main(["install"]) == wfp.REFUSED
    err = capsys.readouterr().err
    assert "needs an administrator" in err and "Nox Sandbox Rules" in err


def test_the_ctypes_plumbing_is_gone() -> None:
    # The elevated code path lives in the helper script only; nothing in the package can add
    # filters from the user-writable runtime.
    assert not (REPO / "src" / "nox" / "sandbox" / "_wfp_win32.py").exists()
    assert not hasattr(wfp, "apply") and not hasattr(wfp, "remove")


# ---- the protected helper ------------------------------------------------------------------


def test_no_helper_function_shadows_a_script_variable() -> None:
    # PowerShell names are case-insensitive and scoped dynamically: a local `$n` inside a function
    # hid the P/Invoke type `$N` from that function (a live-engine run caught it once).
    import re

    script = wfp_helper.helper_script()
    lines = script.splitlines()
    top = {m.group(1).lower() for line in lines if (m := re.match(r"\$(\w+)\s*=", line))}
    top.add("fwp")  # assigned inside the main try block
    used_in_functions: set[str] = set()
    inside = False
    for line in lines:
        if line.startswith("function "):
            inside = True
            params = line.split("(", 1)[1].split(") {", 1)[0] if "(" in line else ""
            used_in_functions |= {n.lower() for n in re.findall(r"\$(\w+)", params)}
            body = line.split(") {", 1)[1] if ") {" in line else ""
            used_in_functions |= {n.lower() for n in re.findall(r"\$(\w+)\s*=(?!=)", body)}
        elif line.startswith("}"):
            inside = False
        elif inside:
            used_in_functions |= {m.lower() for m in re.findall(r"\$(\w+)\s*=(?!=)", line)} | {
                m.lower() for m in re.findall(r"foreach \(\$(\w+) in", line)
            }
    assert top & used_in_functions == set()


def test_the_helper_is_ascii_deterministic_and_self_contained() -> None:
    script = wfp_helper.helper_script()
    data = wfp_helper.helper_bytes()
    assert data.isascii() and b"\r\n" not in data and not data.startswith(b"\xef\xbb\xbf")
    assert "@@" not in script
    assert wfp_helper.helper_sha256() == hashlib.sha256(data).hexdigest().upper()
    lowered = script.lower()
    # .NET only: no cmdlet (module autoload could load a user-writable module), no compiler, no
    # file of Nox's, no encoded command, no temp file.
    for banned in (
        "add-type",
        "new-object",
        "import-module",
        "get-content",
        "set-content",
        "out-null",
        "write-host",
        "write-output",
        "foreach-object",
        "invoke-expression",
        "iex ",
        "$env:",
        "gettemppath",
        "-encodedcommand",
        "csharpcodeprovider",
    ):
        assert banned not in lowered, banned
    # Forwarder ports are above 32767: never squeezed through an [int16] cast (a live-engine run
    # caught that once).
    assert "[int16]$plo" not in script and "[int16]$phi" not in script
    assert "$M::WriteInt32($v, 8, $plo)" in script
    # fwpuclnt.dll by full system path only.
    assert "[Environment]::SystemDirectory, 'fwpuclnt.dll'" in script
    for key in (wfp.PROVIDER_KEY, wfp.SUBLAYER_KEY, *wfp.LAYERS.values()):
        assert str(key) in script


def test_the_protected_folder_acl_is_admin_only() -> None:
    script = wfp_helper.helper_script()
    assert wfp_helper.PROTECTED_ACL == (
        ("S-1-5-32-544", "FullControl"),
        ("S-1-5-18", "FullControl"),
        ("S-1-5-32-545", "ReadAndExecute"),
    )
    assert "$Acl = @('S-1-5-32-544=FullControl', 'S-1-5-18=FullControl', " in script
    assert "'S-1-5-32-545=ReadAndExecute')" in script
    assert "$Writers = @('S-1-5-32-544', 'S-1-5-18')" in script
    # Inheritance from Program Files is cut, the owner is Administrators, and the result is read
    # back and checked for anyone else who could write, own or re-ACL it.
    assert "$sec.SetAccessRuleProtection($true, $false)" in script
    assert "$sec.SetOwner((Sid 'S-1-5-32-544'))" in script
    assert "AssertAdminOnly ([IO.Directory]::GetAccessControl($Dir)) $Dir" in script
    assert "AssertAdminOnly ([IO.File]::GetAccessControl($File)) $File" in script
    assert "ReparsePoint" in script
    write = 2 | 4 | 16 | 64 | 256 | 65536 | 262144 | 524288 | 0x40000000 | 0x10000000
    assert f"$WriteMask = {write}" in script


def test_status_goes_to_hklm_never_a_user_folder() -> None:
    script = wfp_helper.helper_script()
    assert wfp_helper.STATUS_KEY == "SOFTWARE\\Nox\\SandboxRules"
    assert "[Microsoft.Win32.Registry]::LocalMachine.CreateSubKey($RegPath)" in script
    assert "--status-file" not in script and "WriteAllText" not in script
    # The only file it ever writes is the protected copy, under Program Files.
    assert script.count("WriteAllBytes") == 1
    assert "[IO.File]::WriteAllBytes($File, $Self)" in script
    assert "GetFolderPath('ProgramFiles'), 'Nox Sandbox Rules'" in script


def test_the_bootstrap_runs_only_bytes_that_match_the_pinned_hash() -> None:
    sha = wfp_helper.helper_sha256()
    path = "C:\\Users\\Jürgen\\Nox\\sandbox\\x.ps1"
    boot = wfp_helper.bootstrap("install", [path])
    assert f"-eq '{sha}'" in boot
    # The path is never source text: only the hex digits of its UTF-16 code units are.
    hexed = "".join(f"{ord(ch):04X}" for ch in path)
    assert f"@('{hexed}')" in boot and "Jürgen" not in boot and boot.isascii()
    assert "[Convert]::ToInt32($x.Substring($i,4),16)" in boot
    assert '"' not in boot  # the whole bootstrap sits in one pair of double quotes
    # The bytes it checked are the bytes it runs (and hands on for the protected copy).
    assert "[ScriptBlock]::Create([Text.Encoding]::ASCII.GetString($b))) 'install' $b" in boot
    assert "exit 3" in boot
    params = wfp_helper.command_params("uninstall", ["C:\\a.ps1", "C:\\b.ps1"])
    assert params.startswith('-NoProfile -NonInteractive -WindowStyle Hidden -Command "')
    assert "EncodedCommand" not in params and "Bypass" not in params
    assert f"@({wfp_helper.path_arg('C:\\a.ps1')},{wfp_helper.path_arg('C:\\b.ps1')})" in params
    with pytest.raises(ValueError):
        wfp_helper.bootstrap("show", ["C:\\a.ps1"])
    with pytest.raises(ValueError):
        wfp_helper.bootstrap("install", [])


@pytest.mark.parametrize(
    "bad",
    [
        "\u2019",  # right single quotation mark: PowerShell reads it as '
        "\u2018",
        "\u201a",
        "\u201b",
        "\u201c",  # PowerShell reads these as "
        "\u201d",
        "\u201e",
        "'",
        '"',
        "$",
        "`",
        ";",
        "&",
        "\n",
        "\x00",
    ],
)
def test_a_path_with_a_quote_or_metacharacter_never_reaches_the_elevated_bootstrap(
    bad: str,
) -> None:
    # e.g. a user name like O’Brien$(calc) - refused outright (defence in depth; the hex
    # encoding alone already keeps every character out of the source).
    path = f"C:\\Users\\O{bad}Brien\\AppData\\Local\\Programs\\Nox\\sandbox\\x.ps1"
    assert not wfp_helper.path_is_safe(path)
    with pytest.raises(ValueError):
        wfp_helper.path_arg(path)
    with pytest.raises(ValueError):
        wfp_helper.command_params("uninstall", ["C:\\ok.ps1", path])
    # The Pascal side refuses exactly the same characters.
    include = wfp_helper.inno_include()
    if ord(bad) >= 32 and ord(bad) != 127:
        assert f"(C = {ord(bad)})" in include


def test_the_pascal_path_check_matches_python() -> None:
    include = wfp_helper.inno_include()
    check = include.split("function SandboxPathSafe", 1)[1].split("end;\r\n\r\n", 1)[0]
    codes = {int(part.split(")")[0]) for part in check.split("(C = ")[1:]}
    assert codes == {ord(ch) for ch in wfp_helper.UNSAFE_PATH_CHARS} | {127}
    assert "(C < 32)" in check
    # The hex encoding: four upper-case hex digits per UTF-16 code unit, like `path_arg`.
    assert "Format('%.4X', [Ord(S[I])])" in include
    assert (
        wfp_helper.path_arg("C:\\Jürgen")
        == "'" + "".join(f"{ord(c):04X}" for c in "C:\\Jürgen") + "'"
    )
    # Only the three fixed actions build a command at all.
    assert "(Action <> 'install') and (Action <> 'uninstall') and (Action <> 'status')" in include


def test_the_inno_include_embeds_the_pinned_hash_and_the_same_bootstrap() -> None:
    include = wfp_helper.inno_include()
    assert include.isascii()
    assert f"Result := '{wfp_helper.helper_sha256()}';" in include
    assert "function SandboxRulesParams(const Action, Paths: String): String;" in include
    # Evaluate the Pascal concatenation by hand: it must give exactly `command_params`.
    body = include.split("function SandboxRulesParams", 1)[1].split("begin", 1)[1]
    expr = body.split("Exit;", 1)[1].split("Result :=", 1)[1].split(";\r\nend;", 1)[0]
    paths = wfp_helper.path_arg("C:\\x.ps1")
    parts = []
    for token in _pascal_tokens(expr):
        if token == "Paths":
            parts.append(paths)
        elif token == "SandboxHelperSha256()":
            parts.append(wfp_helper.helper_sha256())
        elif token == "Action":
            parts.append("install")
        else:
            parts.append(token)
    assert "".join(parts) == wfp_helper.command_params("install", ["C:\\x.ps1"])


def _pascal_tokens(expr: str) -> list[str]:
    """The operands of a Pascal `a + 'b' + c` expression, string literals unquoted."""
    out: list[str] = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            j, buf = i + 1, []
            while True:
                if expr[j] == "'" and j + 1 < len(expr) and expr[j + 1] == "'":
                    buf.append("'")
                    j += 2
                elif expr[j] == "'":
                    break
                else:
                    buf.append(expr[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        elif ch.isalpha():
            j = i
            while j < len(expr) and (expr[j].isalnum() or expr[j] in "()"):
                j += 1
            out.append(expr[i:j])
            i = j
        else:
            i += 1
    return out


@pytest.mark.skipif(sys.platform != "win32", reason="runs the helper in Windows PowerShell")
def test_the_helper_derives_exactly_the_rules_nox_shows(tmp_path: Path) -> None:
    script = tmp_path / "helper.ps1"
    script.write_bytes(wfp_helper.helper_bytes())
    result = subprocess.run(
        [
            str(
                Path(os.environ.get("SystemRoot", "C:\\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            ),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "show",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == wfp.describe(wfp.all_rules())


# ---- the probe stays the source of truth -----------------------------------------------------


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Any:
    from nox.sandbox import windows

    monkeypatch.setattr(windows, "_loopback_probe", {})
    return windows


@pytest.mark.parametrize(
    ("measured", "listeners", "reachable"),
    [
        (False, [], False),  # refused, nothing in the forwarder range: confined -> strong
        (False, [47_150], True),  # a host program in the open range: reachable
        (False, None, True),  # the listeners cannot be listed: fail closed
        (True, [], True),  # the rules are missing or did not take effect
        (None, [], True),  # inconclusive
    ],
)
def test_the_probe_reports_confined_only_when_it_observes_it(
    probe: Any,
    monkeypatch: pytest.MonkeyPatch,
    measured: bool | None,
    listeners: list[int] | None,
    reachable: bool,
) -> None:
    monkeypatch.setattr(probe, "_probe_loopback", lambda sid: measured)
    monkeypatch.setattr(probe, "forwarder_range_listeners", lambda: listeners)
    assert probe.host_loopback_reachable(SHELL_SID) is reachable


def test_a_connect_refused_by_the_rules_reads_as_blocked() -> None:
    from nox.sandbox.windows import probe_verdict

    # With the rules, the connect fails at once (access denied) - a SocketException, so the
    # probe prints its "blocked" marker; that and nothing else is "confined".
    refused = Result(exit_code=0, stdout="NOXPROBE BLOCKED\r\n", stderr="")
    assert probe_verdict(connected=False, result=refused) is False
    assert probe_verdict(connected=True, result=refused) is True


def test_the_listener_check_looks_only_at_the_forwarder_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    import psutil

    from nox.sandbox import windows

    def conn(port: int, status: str = psutil.CONN_LISTEN) -> Any:
        return types.SimpleNamespace(laddr=types.SimpleNamespace(port=port), status=status)

    conns = [conn(80), conn(47_099), conn(47_100), conn(47_615), conn(47_616), conn(47_150, "X")]
    monkeypatch.setattr(psutil, "net_connections", lambda kind: conns)
    assert windows.forwarder_range_listeners() == [47_100, 47_615]

    def denied(kind: str) -> Any:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", denied)
    assert windows.forwarder_range_listeners() is None


def test_the_probe_listener_never_sits_in_the_forwarder_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from nox.sandbox import windows

    ports = iter([47_100, 47_227, 51_000])
    closed: list[int] = []

    class _Sock:
        def __init__(self, *args: Any) -> None:
            self.port = next(ports)

        def bind(self, addr: Any) -> None:
            assert addr == ("127.0.0.1", 0)

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", self.port)

        def close(self) -> None:
            closed.append(self.port)

    monkeypatch.setattr(socket, "socket", _Sock)
    assert windows._probe_listener().getsockname()[1] == 51_000  # noqa: SLF001
    assert closed == [47_100, 47_227]


def test_the_forwarder_listens_only_in_its_containers_block() -> None:
    from nox.sandbox.windows import forwarder_port, forwarder_script

    ports = wfp.container_ports(SHELL_CONTAINER)
    script = forwarder_script("nox-abc", ports)
    assert "'nox-abc'" in script and "$lo = 47100; $n = 47103 - $lo + 1" in script
    assert "{pipe}" not in script and "{lo}" not in script and "{hi}" not in script
    assert "Loopback, 0)" not in script  # never "any free port"
    assert forwarder_port("NOXFWD 47100", ports) == 47_100
    assert forwarder_port("NOXFWD 47103", ports) == 47_103
    # Another container's port - even one of the global range - is not this forwarder's.
    for bad in ("NOXFWD 47104", "NOXFWD 4242", "NOXFWD NONE", "NOXFWD", "", "NOXFWD 47100 x"):
        assert forwarder_port(bad, ports) is None, bad
    with pytest.raises(ValueError):
        forwarder_script("nox-abc", range(4000, 4004))


OWN = (10, 1000.0)
PORTS = range(47_148, 47_152)


def _listener(port: int, pid: int | None, status: str = "LISTEN") -> Any:
    import types

    return types.SimpleNamespace(laddr=types.SimpleNamespace(port=port), status=status, pid=pid)


@pytest.mark.parametrize(
    ("conns", "ours", "problem"),
    [
        ([(47_150, 10)], set(), None),  # the forwarder holds its port, nothing else in its block
        ([(47_150, 10), (47_151, 20)], {(20, 2000.0)}, None),  # a sibling run's live forwarder
        ([(47_150, 10), (80, 99), (47_160, 99)], set(), None),  # outside its block: irrelevant
        ([(47_150, 10), (47_151, 99)], set(), "47151"),  # a foreign listener in the block
        ([(47_150, 10), (47_151, None)], set(), "47151"),  # owner unknown: foreign
        ([(47_150, 10), (47_151, 20)], {(20, 1.0)}, "47151"),  # pid reused by another process
        ([(47_150, 99)], set(), "47150"),  # someone else holds the forwarder's port
        ([], set(), "not held"),  # the forwarder's port is not listening at all
    ],
)
def test_every_run_checks_who_holds_its_forwarder_ports(
    monkeypatch: pytest.MonkeyPatch,
    conns: list[tuple[int, int | None]],
    ours: set[tuple[int, float]],
    problem: str | None,
) -> None:
    import psutil

    from nox.sandbox import windows

    monkeypatch.setattr(windows, "_forwarders", set(ours))
    created = {10: 1000.0, 20: 2000.0, 99: 9.0}
    monkeypatch.setattr(windows, "_identity", lambda pid: (pid, created[pid]) if pid else None)
    listeners = [_listener(port, pid, psutil.CONN_LISTEN) for port, pid in conns]
    monkeypatch.setattr(psutil, "net_connections", lambda kind: listeners)
    found = windows.forwarder_port_problem(47_150, OWN, PORTS)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


def test_unlistable_listeners_or_an_unknown_forwarder_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.sandbox import windows

    def denied(kind: str) -> Any:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", denied)
    assert "cannot be listed" in (windows.forwarder_port_problem(47_150, OWN, PORTS) or "")
    assert "cannot be identified" in (windows.forwarder_port_problem(47_150, None, PORTS) or "")


def test_a_listener_that_appears_during_a_run_ends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from nox.sandbox import windows

    answers = iter([None, None, "a listener that is not Nox's forwarder holds port(s) [47151]"])
    monkeypatch.setattr(windows, "forwarder_port_problem", lambda *a: next(answers, None))
    stopped = threading.Event()
    watch = windows._PortWatch(47_150, OWN, PORTS, stopped.set, interval_s=0.01)  # noqa: SLF001
    assert stopped.wait(5), "the run was not ended"
    assert watch.problem is not None and "47151" in watch.problem
    watch.close()

    calm = windows._PortWatch(  # noqa: SLF001
        47_150, OWN, PORTS, lambda: pytest.fail("stopped"), interval_s=0.01
    )
    monkeypatch.setattr(windows, "forwarder_port_problem", lambda *a: None)
    calm.close()
    assert calm.problem is None


def test_an_unverified_forwarder_refuses_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from nox.sandbox import windows
    from nox.sandbox.model import SandboxUnavailable

    closed: list[str] = []
    fwd = types.SimpleNamespace(pid=10, stdout_fd=1, close=lambda: closed.append("closed"))
    backend = object.__new__(windows.WindowsSandbox)
    backend._w = types.SimpleNamespace(spawn=lambda *a, **k: fwd)  # type: ignore[attr-defined]
    monkeypatch.setattr(windows, "_read_line", lambda fd, timeout: "NOXFWD 47101")
    monkeypatch.setattr(windows, "_close_fds", lambda s: None)
    monkeypatch.setattr(windows, "_identity", lambda pid: OWN)
    monkeypatch.setattr(windows, "forwarder_port_problem", lambda *a: "foreign 47102")
    limits = types.SimpleNamespace(memory_bytes=1 << 30)
    with pytest.raises(SandboxUnavailable, match="could not be verified"):
        backend._start_forwarder(  # noqa: SLF001
            Path("."), "\\\\.\\pipe\\nox-abc", limits, SHELL_SID
        )
    assert closed == ["closed"] and OWN not in windows._forwarders  # noqa: SLF001

    monkeypatch.setattr(windows, "forwarder_port_problem", lambda *a: None)
    got, port, own, ports = backend._start_forwarder(  # noqa: SLF001
        Path("."), "\\\\.\\pipe\\nox-abc", limits, SHELL_SID
    )
    assert (port, own, ports) == (47_101, OWN, wfp.container_ports(SHELL_CONTAINER))
    assert OWN in windows._forwarders  # noqa: SLF001
    windows._release_forwarder(got, own)  # noqa: SLF001
    assert OWN not in windows._forwarders  # noqa: SLF001
    # A container outside Nox's pool has no ports at all.
    with pytest.raises(SandboxUnavailable, match="no forwarder ports"):
        backend._start_forwarder(Path("."), "\\\\.\\pipe\\nox-abc", limits, "S-1-15-2-1-2")  # noqa: SLF001


async def test_a_run_cannot_use_another_runs_proxy() -> None:
    # Workload A reaching workload B's forwarder would land in B's proxy - which answers 407 to
    # anything but B's own per-run credentials (and the rules keep A off B's ports anyway).
    import asyncio
    import base64

    from nox.sandbox.netguard import NetGuard
    from nox.sandbox.proxy import EgressProxy

    run_a = EgressProxy.new_credentials()
    run_b = EgressProxy.new_credentials()
    assert run_a != run_b
    proxy_b = EgressProxy(allow=["example.com:443"], guard=NetGuard(), credentials=run_b)
    port = await proxy_b.start()
    try:
        for creds in (run_a, None):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            auth = b""
            if creds:
                token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode())
                auth = b"Proxy-Authorization: Basic " + token + b"\r\n"
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n" + auth)
            writer.write(b"\r\n")
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(), 10)
            assert reply.startswith(b"HTTP/1.1 407"), reply[:40]
            writer.close()
    finally:
        await proxy_b.stop()


# ---- the installer ---------------------------------------------------------------------------


def _iss() -> str:
    return (REPO / "installer" / "nox.iss").read_text(encoding="utf-8")


def test_the_installer_never_runs_its_own_python_elevated() -> None:
    script = _iss()
    assert script.isascii()  # Inno reads a file without BOM as ANSI
    assert "PrivilegesRequired=lowest" in script  # the install itself stays per-user
    task = next(line for line in script.splitlines() if line.startswith('Name: "sandboxrules"'))
    assert "Flags: unchecked" not in task  # ticked by default
    lines = script.splitlines()
    elevated = [ln for ln in lines if "'runas'" in ln or 'Verb: "runas"' in ln]
    # One elevated call site: the system's PowerShell with the hash-checking bootstrap.
    assert len(elevated) == 1
    assert "ShellExec('runas', SandboxPowerShell(), Params," in elevated[0]
    assert "Params := SandboxRulesParams(Action, Paths);" in script
    assert "ExpandConstant('{sys}\\WindowsPowerShell\\v1.0\\powershell.exe')" in script
    assert 'python.exe"; Parameters: "-m nox.sandbox.wfp' not in script
    assert "nox.sandbox.wfp install" not in script and "--status-file" not in script
    assert '#include "..\\build\\sandbox-rules\\sandbox_rules.iss"' in script
    # An older build's uninstall log could still name the elevated python step: never keep it.
    assert "UninstallLogMode=overwrite" in script
    # Status comes from HKLM, not a file in the user's temp folder.
    assert "RegQueryDWordValue(HKLM64, SandboxStatusKey, 'Sequence'" in script
    assert "{tmp}" not in script


def test_uninstall_removes_the_rules_whenever_they_may_exist() -> None:
    script = _iss()
    code = script.split("procedure CurUninstallStepChanged", 1)[1]
    assert "usUninstall" in code and "SandboxRulesMayExist()" in code
    # The protected copy first, then the copy in {app}: both only after their hash matched.
    assert "RunSandboxHelper('uninstall', SandboxProtectedCopy(), SandboxAppCopy(), True," in code
    run = script.split("function RunSandboxHelper", 1)[1].split("\nend;", 1)[0]
    # Paths reach the bootstrap only hex-encoded and checked; a refused path starts nothing.
    assert "Paths := SandboxPathArg(First);" in run and "if Paths = '' then" in run
    assert "SandboxPsQuote" not in script
    may = script.split("function SandboxRulesMayExist", 1)[1].split("end;", 1)[0]
    # Independent of the install task: the folder, the status key, or Nox's WFP provider.
    assert "FileExists(SandboxProtectedCopy())" in may
    assert "RegKeyExists(HKLM64, SandboxStatusKey)" in may
    assert "RunSandboxHelper('status'" in may
    assert "Tasks: sandboxrules" not in script.split("[UninstallRun]", 1)[1]


def test_the_installer_ships_the_licenses_and_the_source_offer() -> None:
    script = _iss()
    assert "LicenseFile={#BuildLegalDir}\\INSTALLER_LICENSE.txt" in script
    assert 'Source: "{#BuildLegalDir}\\*"; DestDir: "{app}"' in script
    assert (
        'Source: "{#BuildSandboxRulesDir}\\nox-sandbox-rules.ps1"; DestDir: "{app}\\sandbox"'
        in (script)
    )
