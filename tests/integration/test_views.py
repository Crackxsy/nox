"""Nox draws something, and the dashboard can read it.

The point of the test is the round trip: the tool validates and stores, the event says what appeared
without carrying it, and the request hands the whole thing to the page. Each of those three is a
place where a shape could quietly change and nobody would notice until a chart looked wrong.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from nox.core.events import E
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import Envelope, Kind, Source
from tests._ports import free_port_base

BARS = {
    "kind": "bars",
    "title": "Platz auf den Laufwerken",
    "note": "C: wird knapp",
    "bars": [{"label": "C:", "value": 41.5}, {"label": "D:", "value": 812.0}],
    "unit": "GB",
}


def _config(tmp_path: Path) -> Any:
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "balanced"},
        "security": {"profile": "companion"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": []},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    user_config = tmp_path / "user.yaml"
    user_config.write_text(yaml.safe_dump({}), encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))
    instance = NoxCore(_config(tmp_path), voice=False)
    await asyncio.wait_for(instance.start(), timeout=60)
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


def _ctx(role: str = "dashboard") -> RequestContext:
    return RequestContext(
        client_id=f"{role}:1",
        role=role,
        request=Envelope(kind=Kind.REQUEST, name="views.list", src=Source(role=role, id="1")),
    )


async def _request(core: NoxCore, request: str, /, **arguments: Any) -> dict[str, Any]:
    registration = core.registry.get(request)
    assert registration is not None, f"{request} is not registered"
    return await registration.handler(_ctx(), registration.payload_model(**arguments))


async def _as_model(core: NoxCore, tool: str, /, **arguments: Any) -> Any:
    return await core.tool_executor.call(
        agent="companion", name=tool, arguments=arguments, mode="companion"
    )


async def test_the_tool_and_the_requests_are_registered(core: NoxCore) -> None:
    assert "view.show" in core.tool_registry.names()
    assert core.registry.get("views.list") is not None
    assert core.registry.get("views.clear") is not None


async def test_drawing_something_and_reading_it_back(core: NoxCore) -> None:
    result = await _as_model(core, "view.show", **BARS)
    assert result.ok, result.error
    assert result.data["ok"], result.data

    payload = await _request(core, "views.list")

    assert len(payload["views"]) == 1
    body = payload["views"][0]["body"]
    assert body["kind"] == "bars" and body["unit"] == "GB"
    assert [bar["label"] for bar in body["bars"]] == ["C:", "D:"]
    assert isinstance(payload["views"][0]["created_at"], str)


async def test_the_event_says_what_appeared_and_not_what_is_in_it(core: NoxCore) -> None:
    """An event payload is a notification. A hundred table rows do not belong in one."""
    seen: list[dict[str, Any]] = []
    unsubscribe = core.bus.subscribe(E.VIEW_SHOWN, lambda ev: seen.append(dict(ev.payload)))
    try:
        await _as_model(core, "view.show", **BARS)
        await asyncio.sleep(0.05)
    finally:
        unsubscribe()

    assert len(seen) == 1
    assert seen[0]["kind"] == "bars" and seen[0]["title"] == BARS["title"]
    assert "bars" not in seen[0]


async def test_a_view_the_model_got_wrong_comes_back_as_a_sentence(core: NoxCore) -> None:
    broken = {"kind": "table", "title": "Kaputt", "columns": ["a", "b"], "rows": [["nur eins"]]}

    result = await _as_model(core, "view.show", **broken)

    assert result.ok, "the tool answers; it does not raise"
    assert result.data["ok"] is False and "row 0" in result.data["error"]
    payload = await _request(core, "views.list")
    assert payload["views"] == [], "nothing broken reaches the board"


async def test_clearing_the_board(core: NoxCore) -> None:
    await _as_model(core, "view.show", **BARS)

    cleared = await _request(core, "views.clear")

    assert cleared["removed"] == 1
    assert (await _request(core, "views.list"))["views"] == []


async def test_drawing_is_allowed_without_a_dialog(core: NoxCore) -> None:
    """Putting a chart on a page changes nothing on the machine; asking first would be noise."""
    result = await _as_model(core, "view.show", **BARS)

    assert result.decision == "allow"
