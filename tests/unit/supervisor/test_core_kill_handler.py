"""The core's side of `sup.kill`: `mode=restart` is a clean shutdown, every other mode is the kill
switch.

The product owner's log (2026-09-15) shows what the old handler did: the watchdog asked for a
graceful restart while the core was still booting, the core engaged the kill switch, and Nox sat in
safe mode with the voice worker terminated - while the supervisor, waiting for a process that never
exited, restarted nothing. Deliberately does not boot a `NoxCore` (far too heavy for two handlers);
it wires only what they touch, like `tests/unit/core/test_greeting_policy.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from nox.app import NoxCore
from nox.core.config import NoxConfig


class _FakeKillSwitch:
    def __init__(self) -> None:
        self.engaged: list[tuple[str, str]] = []

    async def engage(self, origin: str, reason: str) -> None:
        self.engaged.append((origin, reason))


def make_core() -> tuple[NoxCore, _FakeKillSwitch]:
    core = NoxCore(NoxConfig(), voice=False, extensions=False)
    killswitch = _FakeKillSwitch()
    core.security = SimpleNamespace(killswitch=killswitch)  # type: ignore[assignment]
    return core, killswitch


async def test_restart_request_shuts_down_cleanly_without_the_kill_switch() -> None:
    core, killswitch = make_core()
    core._on_supervisor_restart("heartbeats missed")
    assert core.shutdown_requested.is_set()  # _run() -> stop() -> exit 0 -> supervisor respawns
    assert killswitch.engaged == []


async def test_kill_still_engages_the_kill_switch() -> None:
    core, killswitch = make_core()
    await core._on_supervisor_kill("kill switch by tray", "tray")
    assert killswitch.engaged == [("supervisor", "kill switch by tray")]
    assert not core.shutdown_requested.is_set()


async def test_kill_before_security_exists_shuts_down_instead_of_crashing() -> None:
    """The client now runs from the first boot step, before `SecurityContext` is built."""
    core = NoxCore(NoxConfig(), voice=False, extensions=False)
    await core._on_supervisor_kill("kill switch", "hotkey")
    assert core.shutdown_requested.is_set()
