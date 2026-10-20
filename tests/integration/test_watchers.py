"""Watchers on a real headless core: a fake page changes -> exactly one report; the cooldown holds;
a restart in between changes nothing about either."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.security.model import Decision, Risk
from nox.tools.registry import ToolSpec
from tests._ports import free_port_base

pytestmark = pytest.mark.timeout(240)

PAGE = {"text": "07:12 RB 40 Nordstadt 5 puenktlich"}


class Empty(BaseModel):
    pass


def _config(tmp_path: Path, base: int):  # type: ignore[no-untyped-def]
    (tmp_path / "vault").mkdir(exist_ok=True)
    names = ("data", "vault", "index", "database", "cache", "backups", "runtime", "logs")
    overrides = {
        "paths": {f"{name}_dir": str(tmp_path / name) for name in names},
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": []},
        "watch": {"poll_s": 600},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def _boot(tmp_path: Path, base: int, reports: list[str]) -> Any:
    core = NoxCore(_config(tmp_path, base), voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=90)

    async def page(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "content": PAGE["text"]}

    core.tool_registry.register(
        ToolSpec(
            name="page.read",
            description="",
            input_model=Empty,
            risk=Risk.READ,
            handler=page,
            # A watcher only takes a tool that declares it changes nothing (unknown = may change).
            side_effects=False,
        )
    )
    service = core.extensions["proactive"].service
    original = service.notify

    async def notify(kind: str, priority: str, text: str, *, context_key: str = "") -> Any:
        reports.append(f"{context_key}|{text}")
        return await original(kind, priority, text, context_key=context_key)

    service.notify = notify
    return core


def _due_now(core: Any) -> None:
    core.db.execute("UPDATE watchers SET next_check_at = '2000-01-01T00:00:00+00:00'")


async def test_exactly_one_report_cooldown_and_restart(tmp_path: Path) -> None:
    reports: list[str] = []
    base = free_port_base()
    core = await _boot(tmp_path, base, reports)
    try:
        engine = core.security.engine
        create = asyncio.create_task(
            core.tool_executor.call(
                "companion",
                "watch.create",
                {
                    "title": "RB 40 hat Verspaetung",
                    "tool": "page.read",
                    "path": "content",
                    "near": "RB 40",
                    "op": "contains",
                    "value": "Min",
                    "value_from": "user",
                    "cooldown_min": 60,
                },
                mode="companion",
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not engine.pending():  # watch.create asks first
            assert loop.time() < deadline
            await asyncio.sleep(0.05)
        engine.reply(engine.pending()[0], Decision.ALLOW)
        made = await create
        assert made.ok and made.data["ok"], made
        watcher_id = made.data["id"]
        scheduler = core.extensions["watch"].scheduler

        assert await scheduler.tick() == []  # on time: nothing
        PAGE["text"] = "07:12 RB 40 Nordstadt 5 +12 Min"
        _due_now(core)
        assert await scheduler.tick() == [watcher_id]
        _due_now(core)
        assert await scheduler.tick() == []  # same delay: not news
        PAGE["text"] = "07:12 RB 40 Nordstadt 5 +20 Min"
        _due_now(core)
        assert await scheduler.tick() == []  # new value, inside the cooldown
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)

    core = await _boot(tmp_path, free_port_base(), reports)  # a restart
    try:
        scheduler = core.extensions["watch"].scheduler
        _due_now(core)
        assert await scheduler.tick() == []  # still inside the cooldown after the restart
        core.db.execute(
            "UPDATE watchers SET last_fired_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (watcher_id,),
        )
        _due_now(core)
        assert await scheduler.tick() == [watcher_id]  # cooldown over, value changed: news
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)
    assert [r.split("|", 1)[0] for r in reports] == [f"watch:{watcher_id}"] * 2
    assert "+12 Min" in reports[0] and "+20 Min" in reports[1]
