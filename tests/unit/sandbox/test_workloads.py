"""One AppContainer per workload class (re-review N6): names, and what `Sandbox` hands the backend.

Runs off Windows too: the backend here is a fake; the real isolation is proven in
`test_windows_run.test_a_run_cannot_read_the_work_folder_of_another_workload`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nox.sandbox import Sandbox
from nox.sandbox.model import EgressDecl, Guarantees, Limits, Result, SandboxMode
from nox.sandbox.netguard import NetGuard
from nox.sandbox.windows import container_name

KEY = b"k" * 32


def test_each_workload_class_has_its_own_container_name() -> None:
    assert container_name("shell", KEY) == "Nox.Sandbox"  # what every earlier build used
    mcp = container_name("mcp:github", KEY)
    assert mcp.startswith("Nox.Sandbox.mcp.") and len(mcp) <= 64
    assert "github" not in mcp  # always the keyed hash, never the id itself
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
    names = {container_name(w, KEY) for w in workloads}
    assert len(names) == len(workloads)  # never shared, also not by sanitising or truncation


def test_a_chosen_name_cannot_take_over_another_container() -> None:
    # Before, `mcp:<id>` was the name itself, so an id could be chosen to equal another
    # workload's (hashed) name. Now every name is a keyed hash: without the installation's key
    # nobody can aim at one, and no workload but shell's ever gets `Nox.Sandbox`.
    other_key = b"o" * 32
    assert container_name("mcp:github", KEY) != container_name("mcp:github", other_key)
    for workload in ("mcp:Sandbox", "Nox.Sandbox", "mcp:", ":", "x"):
        assert container_name(workload, KEY) != "Nox.Sandbox"


def test_the_container_key_is_created_once_per_installation(tmp_path) -> None:  # noqa: ANN001
    from nox.sandbox.windows import container_key

    first = container_key(tmp_path)
    assert len(first) == 32 and container_key(tmp_path) == first
    assert container_key(tmp_path / "other") != first


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
