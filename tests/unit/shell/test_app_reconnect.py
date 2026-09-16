"""The shell follows a core that starts later or is restarted by the supervisor (fresh session
token) instead of staying offline for the rest of the session (2026-09-15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.shell.test_app import make_app, make_runtime


def test_connects_when_the_core_comes_up_later(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path, with_core=False), factory)
    app.start()
    qapp.processEvents()
    assert created == [] and app.model.connected is False
    assert app._reconnect_timer.isActive()

    make_runtime(tmp_path)  # core booted: session.token + ipc.json appear
    app._retry_bridge()
    qapp.processEvents()
    assert len(created) == 1 and created[0].token == "session-token-1234567890"
    assert app.bridge is created[0]
    assert not app._reconnect_timer.isActive()
    app._ping()
    qapp.processEvents()
    assert app.model.connected is True
    app.quit()


def test_bridge_start_failure_is_retried_not_fatal(qapp: Any, tmp_path: Path) -> None:
    attempts: list[str] = []

    def factory(url: str, token: str, role: str, client_id: str) -> Any:
        attempts.append(token)
        raise TimeoutError("timed out during opening handshake")

    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    assert attempts == ["session-token-1234567890"]
    assert app.bridge is None and app._reconnect_timer.isActive()
    app._retry_bridge()
    assert len(attempts) == 2
    app.quit()


def test_repeated_ping_failures_drop_the_bridge_and_reconnect_with_the_new_token(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    app.start()
    qapp.processEvents()
    assert app.model.connected
    old = created[0]
    old.fail_calls = True
    app._ping()
    qapp.processEvents()
    assert app.bridge is old  # one miss is tolerated
    app._ping()
    qapp.processEvents()
    assert app.bridge is None and old.stopped
    assert app.model.connected is False and app._reconnect_timer.isActive()

    # The supervisor restarted the core: a new session token is on disk.
    (tmp_path / "session.token").write_text("session-token-2-abcdefghij", encoding="utf-8")
    app._retry_bridge()
    qapp.processEvents()
    assert len(created) == 2 and created[1].token == "session-token-2-abcdefghij"
    assert app.bridge is created[1]
    app._ping()
    qapp.processEvents()
    assert app.model.connected is True
    app.quit()


def test_pet_page_is_reloaded_when_the_session_token_changed(
    qapp: Any, tmp_path: Path, fake_bridge_factory: Any
) -> None:
    """Shell and core start together: the page can be loaded with the previous run's token
    (`ipc_auth_denied role=pet`); the first successful bridge connect reloads it."""
    from types import SimpleNamespace

    factory, created = fake_bridge_factory
    app = make_app(make_runtime(tmp_path), factory)
    loads: list[str] = []
    app.pet = SimpleNamespace(load=loads.append, show_offline_page=lambda: loads.append("offline"))
    app._load_pet_page()  # what start() does first, with the token read at construction
    assert len(loads) == 1 and "session-token-1234567890" in loads[0]

    # the core (re)started in the meantime and wrote a new token
    (tmp_path / "session.token").write_text("session-token-2-abcdefghij", encoding="utf-8")
    app._connect_bridge()
    assert len(loads) == 2 and "session-token-2-abcdefghij" in loads[1]
    app.bridge = None
    app._connect_bridge()
    assert len(loads) == 2
    app._ping_timer.stop()
    app._reconnect_timer.stop()
