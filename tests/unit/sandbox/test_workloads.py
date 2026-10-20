"""One AppContainer per workload class (re-review N6): names, and what `Sandbox` hands the backend.

Runs off Windows too: the backend here is a fake; the real isolation is proven in
`test_windows_run.test_a_run_cannot_read_the_work_folder_of_another_workload`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nox.sandbox import Sandbox
from nox.sandbox.containers import (
    SHELL_CONTAINER,
    WORKLOAD_SLOTS,
    ContainerSlots,
    container_name,
)
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
)
from nox.sandbox.netguard import NetGuard


def test_each_workload_class_has_its_own_container_name(tmp_path) -> None:  # noqa: ANN001
    slots = ContainerSlots(tmp_path / "slots.json")
    assert container_name("shell", slots) == "Nox.Sandbox"  # what every earlier build used
    mcp = container_name("mcp:github", slots)
    assert mcp == "Nox.Sandbox.w000" and len(mcp) <= 64
    assert "github" not in mcp  # a pool slot, never the id itself
    workloads = (
        "shell",
        "mcp:a",
        "mcp:b",
        "skill:a",
        "skilltest:a",
        "mcp:a b",
        "mcp:a.b",
        "mcp.a:b",
        "mcp:a:b",
        "mcp:a_b",
        "mcp:" + "x" * 200,
        "mcp:" + "x" * 199,
    )
    names = {container_name(w, slots) for w in workloads}
    assert len(names) == len(workloads)  # never shared, also not by sanitising or truncation
    # Recorded: the same workload gets the same container after a restart.
    again = ContainerSlots(tmp_path / "slots.json")
    assert {container_name(w, again) for w in workloads} == names


def test_a_chosen_name_cannot_take_over_another_container(tmp_path) -> None:  # noqa: ANN001
    # The slot is assigned first come, first served: a workload's own name never picks it, and no
    # workload but shell's ever gets `Nox.Sandbox`.
    slots = ContainerSlots(tmp_path / "slots.json")
    assert container_name("mcp:first", slots) == "Nox.Sandbox.w000"
    for workload in ("mcp:w000", "Nox.Sandbox.w000", "mcp:Sandbox", "Nox.Sandbox", ":", "x"):
        name = container_name(workload, slots)
        assert name not in (SHELL_CONTAINER, "Nox.Sandbox.w000")


def test_a_full_pool_and_a_damaged_table_fail_closed(tmp_path) -> None:  # noqa: ANN001
    import pytest

    small = ContainerSlots(tmp_path / "small.json", size=2)
    assert small.slot_for("mcp:a") == 0 and small.slot_for("mcp:b") == 1
    with pytest.raises(SandboxUnavailable, match="taken"):
        small.slot_for("mcp:c")
    assert small.slot_for("mcp:a") == 0  # an owner keeps its slot

    damaged = tmp_path / "damaged.json"
    damaged.write_text("{not json", encoding="utf-8")
    with pytest.raises(SandboxUnavailable, match="damaged"):
        ContainerSlots(damaged).slot_for("mcp:a")
    shared = tmp_path / "shared.json"
    shared.write_text('{"slots": {"mcp:a": 3, "mcp:b": 3}}', encoding="utf-8")
    with pytest.raises(SandboxUnavailable, match="inconsistent"):
        ContainerSlots(shared).slot_for("mcp:c")
    outside = tmp_path / "outside.json"
    outside.write_text(f'{{"slots": {{"mcp:a": {WORKLOAD_SLOTS}}}}}', encoding="utf-8")
    with pytest.raises(SandboxUnavailable, match="inconsistent"):
        ContainerSlots(outside).slot_for("mcp:c")
    with pytest.raises(ValueError, match="shell"):
        ContainerSlots(tmp_path / "x.json").slot_for("shell")


class _Backend:
    mode = SandboxMode.WINDOWS_APPCONTAINER
    proxy_transport = "pipe"
    isolates_workloads = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sids: list[str] = []

    def guarantees(self) -> Guarantees:
        return Guarantees(
            backend="fake",
            confines_writes=True,
            confines_reads=True,
            hides_secrets=True,
            confines_network=True,
        )

    def container_sid_for(self, workload: str) -> str:
        sid = f"S-1-15-2-{abs(hash(workload)) % 1000}"
        self.sids.append(workload)
        return sid

    async def run(self, argv: list[str], **kw: Any) -> Result:
        self.calls.append(kw)
        return Result(exit_code=0, stdout="", stderr="")


async def test_the_workload_reaches_the_backend_and_the_proxy_pipe(monkeypatch) -> None:  # noqa: ANN001
    from nox.sandbox import proxy as proxy_module

    opened: list[str] = []

    async def start_pipe(self: Any, container_sid: str) -> str:
        opened.append(container_sid)
        return r"\.\pipe\nox-test"

    async def stop(self: Any) -> None:
        return None

    monkeypatch.setattr(proxy_module.EgressProxy, "start_pipe", start_pipe)
    monkeypatch.setattr(proxy_module.EgressProxy, "stop", stop)
    backend = _Backend()
    sandbox = Sandbox(backend, guard=NetGuard())  # type: ignore[arg-type]
    await sandbox.run(["x"], limits=Limits(), workload="mcp:github")
    await sandbox.run(
        ["x"], limits=Limits(), egress=EgressDecl(hosts=("api.github.com",)), workload="mcp:github"
    )
    await sandbox.run(["x"], limits=Limits())
    assert [call["workload"] for call in backend.calls] == ["mcp:github", "mcp:github", "shell"]
    # The pipe only the run's own container may open.
    assert backend.sids == ["mcp:github"] and len(opened) == 1


async def test_a_backend_without_workload_classes_is_called_as_before() -> None:
    class _Linux(_Backend):
        isolates_workloads = False

        async def run(  # type: ignore[override]
            self, argv: list[str], *, extra_env: Mapping[str, str] | None = None, **kw: Any
        ) -> Result:
            assert "workload" not in kw
            return await super().run(argv, **kw)

    backend = _Linux()
    await Sandbox(backend, guard=NetGuard()).run(["x"], workload="skill:a")  # type: ignore[arg-type]


def test_the_windows_forwarder_waits_instead_of_polling() -> None:
    # Re-review LOW: the forwarder woke up every 10 ms for the whole life of a long-running MCP
    # server. It now blocks on the pending accept and the relays (the real relay is exercised by
    # test_windows_run.test_appcontainer_declared_egress_reaches_only_the_declared_host).
    from nox.sandbox.windows import FORWARDER_PS

    assert "Start-Sleep" not in FORWARDER_PS
    assert "Pending()" not in FORWARDER_PS
    assert "WaitAny" in FORWARDER_PS and "AcceptTcpClientAsync" in FORWARDER_PS


def test_the_windows_forwarder_never_gets_the_childs_environment(tmp_path) -> None:  # noqa: ANN001
    # Re-review HIGH: only the confined child gets the caller's extra variables and the proxy
    # credentials; the host-started egress forwarder runs with the fixed base environment.
    import os
    import types

    from nox.sandbox.windows import WindowsSandbox, _base_env

    seen: list[dict[str, str]] = []
    containers: list[str] = []
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"NOXFWD 47142\n")
    os.close(write_fd)

    def spawn(argv: list[str], **kw: Any) -> Any:
        seen.append(kw["env"])
        containers.append(kw["app_container"])
        return types.SimpleNamespace(stdout_fd=read_fd, stderr_fd=None, close=lambda: None)

    backend = WindowsSandbox.__new__(WindowsSandbox)
    backend._w = types.SimpleNamespace(spawn=spawn)  # type: ignore[attr-defined]
    _, port = backend._start_forwarder(tmp_path, r"\.\pipe\nox-abc", Limits(), "S-1-15-2-1-2")
    assert port == 47142  # a port of the range the network rules leave open
    assert seen == [_base_env(tmp_path)]
    assert not any("PROXY" in key.upper() for key in seen[0])
    # ... and in the run's own container, the only one the proxy pipe admits (N6).
    assert containers == ["S-1-15-2-1-2"]


def test_a_work_folder_names_its_user_system_and_one_container_only() -> None:
    # The dual access check of an AppContainer child: without the user the container cannot use
    # its own folder (the hosted runner's failure); with another container SID the isolation
    # between workloads would be gone.
    from pathlib import Path

    from nox.sandbox.windows import _work_folder_dacl

    calls = _work_folder_dacl(Path("w"), "S-1-15-2-9", "S-1-5-21-1-2-3-1001")
    grant, strip = calls
    assert grant[1:4] == ["w", "/inheritance:r", "/grant:r"]
    assert grant[4:] == [
        "*S-1-5-21-1-2-3-1001:(OI)(CI)F",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-15-2-9:(OI)(CI)F",
    ]
    assert strip[2] == "/remove:g" and "*S-1-15-2-1" in strip  # never ALL APPLICATION PACKAGES


def test_the_loopback_probe_reads_only_a_refused_connection_as_blocked() -> None:
    # The probe used `New-Object` inside a catch-all: where the container cannot load a cmdlet,
    # the failure itself printed "blocked" and the sandbox claimed loopback isolation (fail open).
    from nox.sandbox.model import Result
    from nox.sandbox.windows import _PROBE_PS, probe_verdict

    assert "New-Object" not in _PROBE_PS
    assert "catch [System.Net.Sockets.SocketException]" in _PROBE_PS
    error = Result(exit_code=0, stdout="NOXPROBE ERROR\n", stderr="")
    assert probe_verdict(connected=False, result=error) is None
