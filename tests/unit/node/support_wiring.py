"""A core with its real parts - configuration, database, security, bus, registries, orchestrator,
tokens - for the tests of `nox.node.hub.install`: `install(core)` runs against it as the core runs
it, with no network (the node listener never starts, so nothing binds a port)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nox.app import DEFAULTS_PATH, PROFILES_DIR
from nox.core.config.loader import load_config
from nox.data.db import Database
from nox.ipc.dispatch import RequestRegistry
from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.ipc.tokens import TokenStore
from nox.lan.tls import ensure_tls
from nox.node.hub.install import NodeRuntime, install
from nox.node.hub.listener import NodeListener
from nox.security.service import SecurityContext
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import FakeBus, make_orchestrator
from tests.unit.node.support import ADDRESSES, NAMES

#: The line the orchestrator's own facts give, so a test sees what is added after it.
BASE_FACTS = "Es ist zwölf Uhr."


class Recorder:
    """What happened, in order, across every spy of one test."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def replace(self, owner: Any, name: str, label: str) -> None:
        """Make `owner.name` an async function that records `label` and does nothing else."""
        setattr(owner, name, self.spy(label))

    def spy(self, label: str, original: Callable[..., Awaitable[Any]] | None = None) -> Any:
        """An async function that records `label` and then runs `original`, if there is one."""

        async def spied(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(label)
            if original is not None:
                return await original(*args, **kwargs)
            return None

        return spied


class RecordingClient:
    """The core's end of a node connection: records every request and notification in order."""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, method: str, params: dict[str, Any], timeout_s: float = 5
    ) -> dict[str, Any]:
        self._recorder.calls.append(f"request {method}")
        self.requests.append((method, params))
        return {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self._recorder.calls.append(f"notify {method}")
        self.sent.append((method, params))


def recording_session(device_id: str, recorder: Recorder) -> Any:
    """A ready listener session of `device_id` that records what the core sends it."""
    ready = asyncio.Event()
    ready.set()
    return SimpleNamespace(
        device_id=device_id, client=RecordingClient(recorder), ready=ready, computer=None
    )


class WiredCore(SimpleNamespace):
    """What `install(core)` is given, built from the real parts."""

    config: Any
    db: Database
    bus: FakeBus
    security: SecurityContext
    registry: RequestRegistry
    tool_registry: ToolRegistry
    orchestrator: Any
    tokens: TokenStore
    hub: Any
    lan_runtime: Any
    disconnected_credentials: list[list[str]]


def build_core(tmp_path: Path, db: Database, *, network: bool = True) -> WiredCore:
    """A core with network access on (or off) whose extension `node` is not installed yet."""
    paths = {
        name: str(tmp_path / name.removesuffix("_dir"))
        for name in (
            "data_dir",
            "vault_dir",
            "index_dir",
            "database_dir",
            "cache_dir",
            "backups_dir",
            "runtime_dir",
            "logs_dir",
        )
    }
    config = load_config(
        DEFAULTS_PATH,
        None,
        None,
        {
            "paths": paths,
            "ipc": {"lan": {"enabled": network, "host": "127.0.0.1", "port": 0, "node_port": 0}},
            "logging": {"level": "WARNING"},
        },
    )
    # The authority a node's certificate is issued by; network access builds it in the real core.
    ensure_tls(Path(paths["data_dir"]) / "tls", names=NAMES, addresses=ADDRESSES)
    orchestrator, bus, _router, _speaker, _turns = make_orchestrator()
    orchestrator.facts = lambda: BASE_FACTS
    security = SecurityContext.build(
        config,
        conn=db.connection,
        connection_lock=db.lock,
        profiles_dir=PROFILES_DIR,
        bus=bus,  # type: ignore[arg-type]
    )
    disconnected: list[list[str]] = []

    async def disconnect_credential(ids: Any, reason: str = "") -> int:
        disconnected.append(sorted(ids))
        return len(disconnected[-1])

    lan = SimpleNamespace(
        tls=None,
        running=False,
        port=0,
        node_pair=None,
        devices=SimpleNamespace(get=lambda _id: None),
    )
    return WiredCore(
        config=config,
        db=db,
        bus=bus,
        security=security,
        registry=RequestRegistry(),
        tool_registry=ToolRegistry(),
        orchestrator=orchestrator,
        tokens=TokenStore(overlay=OverlayTokenStore(tmp_path / "overlay_tokens.json")),
        hub=SimpleNamespace(disconnect_credential=disconnect_credential),
        lan_runtime=lan,
        disconnected_credentials=disconnected,
    )


@asynccontextmanager
async def installed(
    tmp_path: Path, db: Database, *, network: bool = True
) -> AsyncIterator[tuple[WiredCore, NodeRuntime]]:
    """A `WiredCore` with the extension `node` installed, stopped again afterwards."""
    core = build_core(tmp_path, db, network=network)
    runtime = install(core)
    try:
        yield core, runtime
    finally:
        await runtime.stop()
        core.security.close()


# ---- the places where a test reaches into the wiring -------------------------------------------
# Kept in one spot: these are the only lines that name how the core's side is hooked up.


async def fire_connected(listener: NodeListener, session: Any) -> None:
    """What the listener does once a node is authenticated and ready."""
    for hook in listener.on_connected:
        await hook(session)


async def fire_disconnected(listener: NodeListener, session: Any) -> None:
    """What the listener does after a node's connection ended."""
    for hook in listener.on_disconnected:
        await hook(session)


def capability_probe(core: WiredCore, tool: str) -> Any:
    """The answer the capability report gets for `tool` from the extensions that probe tools."""
    return core.tool_registry.availability(tool)


def name_of_satellite(core: WiredCore, device_id: str) -> str:
    """How the permission engine names the voice satellite `device_id` in a question."""
    return core.security.engine._satellite_of(f"voice:node:{device_id}#c0ffee")  # noqa: SLF001


async def _no_credentials(_ids: Any, reason: str = "") -> int:
    return 0


def fake_security(**parts: Any) -> Any:
    """The core's security services as fakes that allow capture, are not engaged and not in panic;
    `parts` replace the ones a test is about."""
    services: dict[str, Any] = {
        "audit": SimpleNamespace(append=lambda **_kw: 0),
        "killswitch": SimpleNamespace(is_engaged=lambda: False),
        "privacy": SimpleNamespace(panic=False, allows_capture=lambda _kind: True),
        "engine": SimpleNamespace(
            add_satellite_label=lambda _name, _label: None,
            mark_tainted=lambda _conversation, _content=None: None,
            is_tainted=lambda _conversation: False,
        ),
    }
    services.update(parts)
    return SimpleNamespace(**services)


def fake_services(**parts: Any) -> Any:
    """A `CoreServices` of fakes, for the tests of one piece of the wiring: every part the hub
    reads is there and does nothing, and `parts` replace the ones a test is about."""
    from nox.tools.registry import ToolRegistry as Tools

    config = SimpleNamespace(
        devices=SimpleNamespace(defaults={}),
        desktop=SimpleNamespace(computer_use=SimpleNamespace(stop_hotkey="ctrl+alt+shift+k")),
        paths=SimpleNamespace(runtime_dir="."),
        sensors=SimpleNamespace(game=SimpleNamespace(process_names=[])),
    )
    services: dict[str, Any] = {
        "config": config,
        "db": None,
        "bus": FakeBus(),
        "security": fake_security(),
        "registry": RequestRegistry(),
        "tool_registry": Tools(),
        "orchestrator": SimpleNamespace(add_fact_source=lambda _name, _source: None),
        "tokens": SimpleNamespace(overlay=None, add_node_overlay_check=lambda _n, _check: None),
        "hub": SimpleNamespace(disconnect_credential=_no_credentials),
        "lan": None,
    }
    services.update(parts)
    return SimpleNamespace(**services)
