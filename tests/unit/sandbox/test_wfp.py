"""The Windows sandbox network rules (`nox.sandbox.wfp`) and the probe that decides "strong".

Runs on every platform: the rules are pure data, the WFP structures are plain ctypes, and the
Windows API is replaced by a fake that records every call. Nothing here adds a real filter or
needs an administrator - what a real installation does is the manual check in docs/SECURITY.md.
"""

from __future__ import annotations

import ctypes
import ipaddress
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from nox.sandbox import wfp
from nox.sandbox.containers import (
    SHELL_CONTAINER,
    WORKLOAD_SLOTS,
    all_container_names,
    container_sid,
    slot_container_name,
)
from nox.sandbox.model import Result
from nox.sandbox.wfp import (
    BLOCKED_V4,
    BLOCKED_V6,
    FORWARDER_PORTS,
    Flags,
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


# ---- the rules -------------------------------------------------------------------------------


def test_each_container_gets_exactly_these_five_filters() -> None:
    permit, block_v4, loop_v4, block_v6, loop_v6 = rules_for(SHELL_CONTAINER)
    pkg = Package(SHELL_SID)

    assert (permit.layer, permit.action, permit.weight, permit.hard) == (
        "ALE_AUTH_CONNECT_V4",
        "PERMIT",
        15,
        False,
    )
    assert permit.conditions == (
        pkg,
        Protocol(6),
        Remote(ipaddress.IPv4Network("127.0.0.1/32")),
        PortRange(47_100, 47_227),
    )
    assert (block_v4.layer, block_v4.action, block_v4.weight, block_v4.hard) == (
        "ALE_AUTH_CONNECT_V4",
        "BLOCK",
        10,
        True,
    )
    assert block_v4.conditions == (pkg, *(Remote(n) for n in BLOCKED_V4))
    assert loop_v4.conditions == (pkg, Flags(0x1)) and loop_v4.hard and loop_v4.action == "BLOCK"
    assert loop_v4.weight < block_v4.weight < permit.weight
    assert block_v6.layer == loop_v6.layer == "ALE_AUTH_CONNECT_V6"
    assert block_v6.conditions == (pkg, *(Remote(n) for n in BLOCKED_V6))
    assert loop_v6.conditions == (pkg, Flags(0x1)) and loop_v6.hard
    # No IPv6 permit: the forwarder listens on IPv4 loopback only.
    assert not any(r.action == "PERMIT" for r in (block_v6, loop_v6))


def test_the_blocked_ranges_cover_loopback_the_lan_and_metadata() -> None:
    v4 = [
        "127.0.0.1",
        "127.5.5.5",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.178.1",
        "169.254.169.254",
        "100.100.100.100",
        "0.0.0.0",
        "224.0.0.251",
        "255.255.255.255",
    ]
    for addr in v4:
        assert any(ipaddress.IPv4Address(addr) in n for n in BLOCKED_V4), addr
    v6 = ["::1", "::", "fe80::1", "fd00::1", "ff02::fb", "::ffff:127.0.0.1", "64:ff9b::a00:1"]
    for addr in v6:
        assert any(ipaddress.IPv6Address(addr) in n for n in BLOCKED_V6), addr
    # The public internet is not touched by these rules (Windows' own isolation keeps a container
    # without capabilities off it; declared egress goes through the proxy, never a direct socket).
    for addr in ("1.1.1.1", "8.8.8.8", "140.82.112.3"):
        assert not any(ipaddress.IPv4Address(addr) in n for n in BLOCKED_V4), addr
    assert not any(ipaddress.IPv6Address("2606:4700::1111") in n for n in BLOCKED_V6)


def test_the_forwarder_range_is_below_the_dynamic_ports_and_open_only_to_tcp() -> None:
    assert FORWARDER_PORTS == range(47_100, 47_228) and len(FORWARDER_PORTS) == 128
    assert FORWARDER_PORTS.stop <= 49_152  # Windows' default dynamic range starts here


def test_every_container_of_the_pool_is_covered_with_fixed_keys() -> None:
    rules = all_rules()
    assert len(rules) == 5 * (WORKLOAD_SLOTS + 1) == 645
    assert len({r.key for r in rules}) == len(rules)
    sids = {c.sid for r in rules for c in r.conditions if isinstance(c, Package)}
    assert sids == {container_sid(n) for n in all_container_names()}
    # Keys are what uninstall deletes by: they must never change between builds.
    assert wfp.filter_key("Nox.Sandbox", "block-local-v4") == uuid.uuid5(
        uuid.UUID("ab693b13-a6ef-48c4-8c8c-b27790b328e1"), "Nox.Sandbox|block-local-v4"
    )
    assert all_rules() == rules  # deterministic


def test_show_lists_one_line_per_filter(capsys: pytest.CaptureFixture[str]) -> None:
    assert wfp.main(["show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 645
    assert lines[0].split()[1:4] == ["ALE_AUTH_CONNECT_V4", "PERMIT", "w=15"]
    assert f"package={SHELL_SID}" in lines[0] and "remote-port=47100-47227" in lines[0]
    assert "BLOCK (hard)" in lines[1] and "remote=192.168.0.0/16" in lines[1]


# ---- the installer's entry point -------------------------------------------------------------


def test_usage_errors_and_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert wfp.main([]) == 2
    assert wfp.main(["install", "extra"]) == 2
    assert wfp.main(["purge"]) == 2
    monkeypatch.setattr(sys, "platform", "linux")
    assert wfp.main(["install"]) == 3


def test_install_reports_through_the_status_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wfp, "apply", lambda: 645)
    status = tmp_path / "status.txt"
    assert wfp.main(["install", "--status-file", str(status)]) == 0
    assert status.read_text(encoding="utf-8").splitlines() == [
        "0",
        "sandbox network rules: installed 645 filter(s)",
    ]

    def refused() -> int:
        raise OSError(5, "FwpmEngineOpen0 failed with 0x5 (run it as administrator)")

    monkeypatch.setattr(wfp, "remove", refused)
    assert wfp.main(["remove", "--status-file", str(status)]) == 1
    first, message = status.read_text(encoding="utf-8").splitlines()
    assert first == "1" and "administrator" in message


# ---- the WFP structures and calls (fake engine) ----------------------------------------------


def test_the_structures_have_the_sdk_layout() -> None:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("layouts pinned for 64-bit Windows")
    from nox.sandbox import _wfp_win32 as w

    sizes = {
        w.GUID: 16,
        w.FWP_VALUE0: 16,
        w.FWP_CONDITION_VALUE0: 16,
        w.FWP_RANGE0: 32,
        w.FWP_V4_ADDR_AND_MASK: 8,
        w.FWP_V6_ADDR_AND_MASK: 17,
        w.FWPM_FILTER_CONDITION0: 40,
        w.FWPM_ACTION0: 20,
        w.FWPM_FILTER0: 200,
        w.FWPM_PROVIDER0: 64,
        w.FWPM_SUBLAYER0: 72,
        w.FWPM_FILTER_ENUM_TEMPLATE0: 72,
    }
    for struct, size in sizes.items():
        assert ctypes.sizeof(struct) == size, struct.__name__
    offsets = {"layerKey": 64, "weight": 96, "filterCondition": 120, "action": 128, "filterId": 176}
    for field, offset in offsets.items():
        assert getattr(w.FWPM_FILTER0, field).offset == offset, field
    assert w.FWPM_SUBLAYER0.weight.offset == 64
    # A GUID is laid out as Windows does (little-endian first three fields).
    key = uuid.UUID("c38d57d1-05a7-4c33-904f-7fbceee60e82")
    g = w.guid(key)
    assert (g.Data1, g.Data2, g.Data3) == (0xC38D57D1, 0x05A7, 0x4C33)
    assert w.to_uuid(g) == key


def test_a_rule_becomes_exactly_this_filter() -> None:
    from nox.sandbox import _wfp_win32 as w

    permit, block_v4, loop_v4, block_v6, _ = rules_for(SHELL_CONTAINER)

    built = w._Built(permit, w.guid(wfp.PROVIDER_KEY))  # noqa: SLF001
    f = built.filter
    assert w.to_uuid(f.filterKey) == permit.key
    assert w.to_uuid(f.layerKey) == w.LAYERS["ALE_AUTH_CONNECT_V4"]
    assert w.to_uuid(f.subLayerKey) == wfp.SUBLAYER_KEY
    assert w.to_uuid(f.providerKey.contents) == wfp.PROVIDER_KEY
    assert f.flags == w.FWPM_FILTER_FLAG_PERSISTENT  # a soft permit
    assert (f.weight.type, f.weight.uint8) == (w.FWP_UINT8, 15)
    assert f.action.type == w.FWP_ACTION_PERMIT
    assert f.numFilterConditions == 4
    pkg, proto, remote, ports = (f.filterCondition[i] for i in range(4))
    assert w.to_uuid(pkg.fieldKey) == w.CONDITION_PACKAGE_ID
    assert pkg.conditionValue.type == w.FWP_SID
    blob = sid_bytes(SHELL_SID)
    assert ctypes.string_at(pkg.conditionValue.pointer, len(blob)) == blob
    assert w.to_uuid(proto.fieldKey) == w.CONDITION_PROTOCOL
    assert (proto.conditionValue.type, proto.conditionValue.uint8) == (w.FWP_UINT8, 6)
    assert w.to_uuid(remote.fieldKey) == w.CONDITION_REMOTE_ADDRESS
    assert remote.conditionValue.type == w.FWP_V4_ADDR_MASK
    v4 = w.FWP_V4_ADDR_AND_MASK.from_address(remote.conditionValue.pointer)
    assert (v4.addr, v4.mask) == (0x7F000001, 0xFFFFFFFF)  # host byte order
    assert ports.matchType == w.FWP_MATCH_RANGE
    rng = w.FWP_RANGE0.from_address(ports.conditionValue.pointer)
    assert (rng.valueLow.type, rng.valueLow.uint16, rng.valueHigh.uint16) == (
        w.FWP_UINT16,
        47_100,
        47_227,
    )

    # Each `_Built` owns the memory its filter points at: keep it alive while reading.
    hard_built = w._Built(block_v4, w.guid(wfp.PROVIDER_KEY))  # noqa: SLF001
    hard = hard_built.filter
    assert hard.flags == w.FWPM_FILTER_FLAG_PERSISTENT | w.FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT
    assert hard.action.type == w.FWP_ACTION_BLOCK
    assert hard.numFilterConditions == 1 + len(BLOCKED_V4)
    ten = w.FWP_V4_ADDR_AND_MASK.from_address(hard.filterCondition[2].conditionValue.pointer)
    assert (ten.addr, ten.mask) == (0x0A000000, 0xFF000000)  # 10.0.0.0/8

    flags_built = w._Built(loop_v4, w.guid(wfp.PROVIDER_KEY))  # noqa: SLF001
    flags = flags_built.filter.filterCondition[1]
    assert w.to_uuid(flags.fieldKey) == w.CONDITION_FLAGS
    assert flags.matchType == w.FWP_MATCH_FLAGS_ANY_SET
    assert (flags.conditionValue.type, flags.conditionValue.uint32) == (w.FWP_UINT32, 1)

    v6_built = w._Built(block_v6, w.guid(wfp.PROVIDER_KEY))  # noqa: SLF001
    v6 = v6_built.filter
    assert w.to_uuid(v6.layerKey) == w.LAYERS["ALE_AUTH_CONNECT_V6"]
    loopback = w.FWP_V6_ADDR_AND_MASK.from_address(v6.filterCondition[1].conditionValue.pointer)
    assert v6.filterCondition[1].conditionValue.type == w.FWP_V6_ADDR_MASK
    assert bytes(loopback.addr) == bytes(16) and loopback.prefixLength == 127


class _FakeEngine:
    """Stands in for fwpuclnt.dll: records every call, answers like the real engine would."""

    def __init__(self, *, existing: set[uuid.UUID] | None = None, fail_add: int = 0) -> None:
        from nox.sandbox import _wfp_win32 as w

        self.w = w
        self.calls: list[str] = []
        self.filters: set[uuid.UUID] = set(existing or ())
        self.added: list[Any] = []
        self.fail_add = fail_add
        self.committed = False
        self.aborted = False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any) -> int:
            self.calls.append(name)
            return getattr(self, "_" + name, lambda *a: 0)(*args)

        return call

    def _FwpmFilterEnum0(self, engine: Any, handle: Any, n: int, entries: Any, count: Any) -> int:  # noqa: N802
        count._obj.value = 0  # nothing of an older build to find
        return 0

    def _FwpmFilterDeleteByKey0(self, engine: Any, key: Any) -> int:  # noqa: N802
        found = self.w.to_uuid(key._obj)
        if found in self.filters:
            self.filters.discard(found)
            return 0
        return self.w.FWP_E_FILTER_NOT_FOUND

    def _FwpmFilterAdd0(self, engine: Any, f: Any, sd: Any, out: Any) -> int:  # noqa: N802
        if self.fail_add and len(self.added) + 1 == self.fail_add:
            return 0x80320017  # FWP_E_INVALID_PARAMETER, say
        self.added.append(self.w.to_uuid(f._obj.filterKey))
        return 0

    def _FwpmProviderAdd0(self, *args: Any) -> int:  # noqa: N802
        return self.w.FWP_E_ALREADY_EXISTS  # an earlier install: not an error

    def _FwpmTransactionCommit0(self, engine: Any) -> int:  # noqa: N802
        self.committed = True
        return 0

    def _FwpmTransactionAbort0(self, engine: Any) -> int:  # noqa: N802
        self.aborted = True
        return 0


def test_install_replaces_every_filter_in_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.sandbox import _wfp_win32 as w

    rules = all_rules()
    fake = _FakeEngine(existing={rules[0].key})
    monkeypatch.setattr(w, "_lib", lambda: fake)
    assert w.install(rules) == 645
    assert fake.added == [r.key for r in rules]
    assert fake.calls[:4] == [
        "FwpmEngineOpen0",
        "FwpmTransactionBegin0",
        "FwpmProviderAdd0",
        "FwpmSubLayerAdd0",
    ]
    assert fake.calls.count("FwpmFilterCreateEnumHandle0") == 2  # both layers searched
    assert fake.calls.count("FwpmFilterDeleteByKey0") == 645
    assert fake.committed and not fake.aborted and fake.calls[-1] == "FwpmEngineClose0"


def test_a_failed_filter_rolls_everything_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.sandbox import _wfp_win32 as w

    fake = _FakeEngine(fail_add=3)
    monkeypatch.setattr(w, "_lib", lambda: fake)
    with pytest.raises(w.WfpError, match="0x80320017"):
        w.install(all_rules())
    assert fake.aborted and not fake.committed and fake.calls[-1] == "FwpmEngineClose0"


def test_without_administrator_the_message_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.sandbox import _wfp_win32 as w

    fake = _FakeEngine()
    fake._FwpmEngineOpen0 = lambda *a: 5  # type: ignore[attr-defined]  # noqa: SLF001
    monkeypatch.setattr(w, "_lib", lambda: fake)
    with pytest.raises(w.WfpError, match="administrator"):
        w.install(all_rules())
    assert "FwpmTransactionBegin0" not in fake.calls


def test_uninstall_removes_filters_sublayer_and_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.sandbox import _wfp_win32 as w

    rules = all_rules()
    fake = _FakeEngine(existing={r.key for r in rules[:10]})
    monkeypatch.setattr(w, "_lib", lambda: fake)
    assert w.uninstall(rules) == 10
    assert not fake.filters and not fake.added
    order = [c for c in fake.calls if c.endswith("DeleteByKey0") and "Filter" not in c]
    assert order == ["FwpmSubLayerDeleteByKey0", "FwpmProviderDeleteByKey0"]
    assert fake.committed


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

    conns = [conn(80), conn(47_099), conn(47_100), conn(47_227), conn(47_228), conn(47_150, "X")]
    monkeypatch.setattr(psutil, "net_connections", lambda kind: conns)
    assert windows.forwarder_range_listeners() == [47_100, 47_227]

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


def test_the_forwarder_listens_only_in_its_range() -> None:
    from nox.sandbox.windows import forwarder_port, forwarder_script

    script = forwarder_script("nox-abc")
    assert "'nox-abc'" in script and "$lo = 47100; $n = 47227 - $lo + 1" in script
    assert "{pipe}" not in script and "{lo}" not in script and "{hi}" not in script
    assert "Loopback, 0)" not in script  # never "any free port"
    assert forwarder_port("NOXFWD 47100") == 47_100
    assert forwarder_port("NOXFWD 47227") == 47_227
    for bad in ("NOXFWD 4242", "NOXFWD NONE", "NOXFWD", "", "NOXFWD 47100 x"):
        assert forwarder_port(bad) is None, bad


# ---- the installer ---------------------------------------------------------------------------


def _iss() -> str:
    return (REPO / "installer" / "nox.iss").read_text(encoding="utf-8")


def test_the_installer_asks_for_administrator_only_for_the_rules() -> None:
    script = _iss()
    assert script.isascii()  # Inno reads a file without BOM as ANSI
    assert "PrivilegesRequired=lowest" in script  # the install itself stays per-user
    task = next(line for line in script.splitlines() if line.startswith('Name: "sandboxrules"'))
    assert "Flags: unchecked" not in task  # ticked by default
    assert "ShellExec('runas', ExpandConstant('{app}\\runtime\\python.exe')," in script
    assert "'-m nox.sandbox.wfp install --status-file \"' + StatusFile + '\"'" in script
    assert "WizardIsTaskSelected('sandboxrules')" in script
    # Elevated `runas` appears for the rule steps only.
    lines = script.splitlines()
    runas = [line for line in lines if "ShellExec('runas'" in line or 'Verb: "runas"' in line]
    assert len(runas) == 2
    assert "nox.sandbox.wfp remove" in runas[0]  # [UninstallRun]
    assert "python.exe" in runas[1]  # [Code]: the install step, parameters checked above


def test_uninstall_removes_the_rules_before_the_runtime_is_gone() -> None:
    lines = _iss().split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0].splitlines()
    entries = [line for line in lines if line.startswith("Filename:")]
    remove = entries[0]
    assert 'Parameters: "-m nox.sandbox.wfp remove"' in remove
    assert 'Verb: "runas"' in remove and "shellexec" in remove and "waituntilterminated" in remove
    assert "Tasks: sandboxrules" in remove and "RunOnceId:" in remove
    assert "{app}\\runtime\\python.exe" in remove
