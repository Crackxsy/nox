"""`nox.watch.install`: a finding reaches `ProactiveService.notify` with `watch:<id>`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from nox.core.config import NoxConfig
from nox.data.db import Database
from nox.ipc.dispatch import RequestRegistry
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec
from nox.watch.install import install


class Empty(BaseModel):
    pass


class Proactive:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def notify(self, kind: str, priority: str, text: str, *, context_key: str) -> None:
        self.calls.append((kind, priority, text, context_key))


class Executor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        spec = self.registry.get(kwargs["name"])
        assert spec is not None
        data = await spec.handler(kwargs["arguments"])
        return SimpleNamespace(ok=True, data=data, error=None)


async def test_a_finding_is_reported_through_the_proactive_service(tmp_path: Path) -> None:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    registry = ToolRegistry()

    async def fetch(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "content": "RB 40 +12 Min"}

    registry.register(
        ToolSpec(name="web.fetch", description="", input_model=Empty, risk=Risk.LOW, handler=fetch)
    )
    proactive = Proactive()
    config = NoxConfig()
    config.watch = config.watch.model_copy(update={"poll_s": 3600})
    audit_rows: list[dict[str, Any]] = []
    core = SimpleNamespace(
        config=config,
        db=db,
        tool_registry=registry,
        tool_executor=Executor(registry),
        registry=RequestRegistry(),
        extensions={"proactive": SimpleNamespace(service=proactive)},
        state=None,
        security=SimpleNamespace(audit=SimpleNamespace(append=lambda **r: audit_rows.append(r))),
    )
    runtime = install(core)
    assert runtime is not None
    try:
        create = registry.get("watch.create")
        assert create is not None
        made = await create.handler(
            {
                "title": "Zug RB 40 verspaetet",
                "tool": "web.fetch",
                "path": "content",
                "near": "RB 40",
                "op": "contains",
                "value": "Min",
                "value_from": "user",
            }
        )
        assert made["ok"]
        assert await runtime.scheduler.tick() == [made["id"]]
        assert proactive.calls == [
            ("urgent", "reminder", "Zug RB 40 verspaetet (+12 Min)", f"watch:{made['id']}")
        ]
        assert core.tool_executor.calls[0]["agent"] == "watcher"
        assert core.tool_executor.calls[0]["origin"] == "watch"
    finally:
        await runtime.stop()
        db.close()
