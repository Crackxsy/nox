"""Every way into the core protects it on Nox-OS first (`nox.noxos.protect`): `run_core` itself,
so `python -m nox.app` or any other caller is covered, not only `nox core`."""

from __future__ import annotations

from typing import Any

import pytest

import nox.entrypoints as entrypoints


async def test_run_core_protects_before_it_builds_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def build_config(*_args: Any) -> Any:
        order.append("config")
        raise entrypoints.ConfigError("stop here")

    monkeypatch.setattr(entrypoints, "protect_on_nox_os", lambda: order.append("protect"))
    monkeypatch.setattr(entrypoints, "build_config", build_config)
    assert await entrypoints.run_core(voice=False) == 2
    assert order == ["protect", "config"]


async def test_run_dev_protects_too(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    def build_config(*_args: Any) -> Any:
        order.append("config")
        raise RuntimeError("stop here")

    monkeypatch.setattr(entrypoints, "protect_on_nox_os", lambda: order.append("protect"))
    monkeypatch.setattr(entrypoints, "build_config", build_config)
    with pytest.raises(RuntimeError):
        await entrypoints.run_dev(voice=False, shell=False)
    assert order == ["protect", "config"]


def test_python_m_nox_app_protects_before_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def fake_run(coro: Any) -> int:
        order.append("loop")
        coro.close()
        return 0

    monkeypatch.setattr(entrypoints, "protect_on_nox_os", lambda: order.append("protect"))
    monkeypatch.setattr(entrypoints.asyncio, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["python -m nox.app", "--no-voice"])
    with pytest.raises(SystemExit):
        entrypoints.main()
    assert order == ["protect", "loop"]
