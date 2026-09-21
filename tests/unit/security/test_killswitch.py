"""Kill switch and panic: flag first, event first, hook timeout, never raises, PIN-gated resume."""

from __future__ import annotations

import asyncio
import time

from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.killswitch import KillSwitchService, PanicModeService
from nox.security.model import Decision, Risk
from nox.security.permissions import DefaultPermissionEngine
from nox.security.privacy import PrivacyService
from tests.unit.fakes import FakeBus

from .conftest import req


async def test_engage_sets_flag_and_emits_event_before_hooks_run(
    bus: FakeBus,
    audit: SqliteAuditLog,
    privacy: PrivacyService,
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit, privacy=privacy, hook_timeout_s=0.1)
    seen: dict[str, object] = {}

    async def voice_stop() -> None:
        seen["engaged_during_hook"] = ks.is_engaged()
        seen["events_before_hook"] = bus.names()

    ks.register_stop_hook("voice", voice_stop)
    report = await ks.engage("hotkey", "test")
    assert report.engaged and report.hooks == {"voice": "ok"} and not report.already_engaged
    assert seen["engaged_during_hook"] is True
    # The kill event goes out first, so everything listening stops at once; the audit entry is
    # written immediately after it and before any hook runs, so a hook that fails cannot cost us
    # the record that the kill happened.
    assert seen["events_before_hook"] == [E.SECURITY_KILL_SWITCH, E.SECURITY_AUDIT]
    assert bus.published[0].payload == {"by": "hotkey", "reason": "test"}
    rows = [e for e in audit.entries() if e.action == "kill_switch.engage"]
    assert rows and rows[0].actor == "hotkey"


async def test_kill_switch_during_a_hung_stop_hook_times_out_and_still_completes(
    bus: FakeBus,
    audit: SqliteAuditLog,
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit, hook_timeout_s=0.1)
    started = asyncio.Event()

    async def hung_tts() -> None:
        started.set()
        await asyncio.sleep(30)

    async def broken_plugin() -> None:
        raise RuntimeError("boom")

    async def fine_worker() -> None:
        await asyncio.sleep(0)

    ks.register_stop_hook("tts", hung_tts)
    ks.register_stop_hook("plugin", broken_plugin)
    ks.register_stop_hook("worker", fine_worker)
    t0 = time.perf_counter()
    report = await ks.engage("tray")
    assert time.perf_counter() - t0 < 1.5
    assert started.is_set()
    assert report.hooks == {"tts": "timeout", "plugin": "error:RuntimeError", "worker": "ok"}
    assert ks.is_engaged()
    assert audit.details(audit.entries()[-1].seq)["hook.tts"] == "timeout"


async def test_engage_never_raises_even_when_bus_and_audit_fail() -> None:
    class ExplodingBus(FakeBus):
        async def publish(self, event: object) -> None:  # type: ignore[override]
            raise RuntimeError("bus down")

    class ExplodingAudit:
        def append(self, **kwargs: object) -> int:
            raise RuntimeError("db down")

        def verify_chain(self) -> bool:
            return True

    ks = KillSwitchService(bus=ExplodingBus(), audit=ExplodingAudit())  # type: ignore[arg-type]
    report = await ks.engage("ui")
    assert report.engaged and ks.is_engaged()
    report = await ks.panic(by="ui")
    assert report.already_engaged


async def test_engaged_kill_switch_makes_engine_deny_side_effects(
    engine: DefaultPermissionEngine,
    safe_mode_flag: dict[str, bool],
    bus: FakeBus,
    audit: SqliteAuditLog,
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit)
    safe_mode_flag["on"] = False
    assert engine.check(req("memory", "write", Risk.LOW)).decision is Decision.ALLOW
    await ks.trigger(by="voice", reason="Nox, Notaus")
    safe_mode_flag["on"] = ks.is_engaged()
    assert engine.check(req("memory", "write", Risk.LOW)).rule_id == "safe_mode"


async def test_user_kill_resumes_without_a_pin_and_is_audited(
    bus: FakeBus, audit: SqliteAuditLog, privacy: PrivacyService
) -> None:
    """OP-6 D: a user-initiated kill (tray/hotkey/UI/voice) needs an explicit action, no PIN."""
    ks = KillSwitchService(bus=bus, audit=audit, privacy=privacy)
    report = await ks.engage("tray")
    assert report.security_path is False and ks.security_path is False
    assert await ks.resume(pin_ok=False, by="shell")
    assert not ks.is_engaged() and ks.engaged_at is None
    entry = next(e for e in audit.entries() if e.action == "kill_switch.resume")
    assert entry.result == "ok" and entry.actor == "shell"
    assert audit.details(entry.seq) == {"security_path": "false", "origin": "tray"}
    assert await ks.resume(pin_ok=False)  # nothing engaged -> trivially ok


async def test_security_path_kill_requires_the_pin_to_resume(
    bus: FakeBus, audit: SqliteAuditLog, privacy: PrivacyService
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit, privacy=privacy)
    report = await ks.engage("tamper", "hard prohibition touched")
    assert report.security_path is True and ks.security_path is True
    assert not await ks.resume(pin_ok=False, by="dashboard")
    assert ks.is_engaged()
    denied = [e for e in audit.entries() if e.action == "kill_switch.resume"]
    assert [e.result for e in denied] == ["denied"]
    assert audit.details(denied[0].seq)["security_path"] == "true"
    assert audit.details(denied[0].seq)["origin"] == "tamper"
    assert await ks.resume(pin_ok=True, by="dashboard")
    assert not ks.is_engaged() and ks.security_path is False


async def test_panic_is_a_security_path_kill_even_after_a_user_kill(
    bus: FakeBus, audit: SqliteAuditLog, privacy: PrivacyService
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit, privacy=privacy)
    await ks.engage("ui", "user stop")
    assert ks.security_path is False
    report = await ks.panic(by="hotkey")
    assert report.security_path is True and ks.security_path is True
    assert not await ks.resume(pin_ok=False, by="shell")
    await ks.engage("ui", "user stop")  # a later user kill must not clear the flag
    assert ks.security_path is True
    assert not await ks.resume(pin_ok=False, by="shell")
    assert await ks.resume(pin_ok=True, by="shell")


async def test_engage_records_the_security_path_flag_in_the_audit(
    bus: FakeBus, audit: SqliteAuditLog
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit)
    await ks.engage("audit", "chain broken")
    entry = next(e for e in audit.entries() if e.action == "kill_switch.engage")
    assert audit.details(entry.seq)["security_path"] == "true"


async def test_panic_goes_offline_and_hides_pet(
    bus: FakeBus,
    audit: SqliteAuditLog,
    privacy: PrivacyService,
) -> None:
    ks = KillSwitchService(bus=bus, audit=audit, privacy=privacy)
    panic = PanicModeService(ks)
    await panic.engage(by="hotkey")
    assert ks.is_engaged() and privacy.mode is PrivacyMode.OFFLINE and privacy.panic
    assert not privacy.allows_capture("microphone") and not privacy.allows_cloud()
    names = bus.names()
    assert names[0] == E.SECURITY_KILL_SWITCH and E.SECURITY_PANIC in names
    panic_event = next(e for e in bus.published if e.name == E.SECURITY_PANIC)
    assert panic_event.payload["hide_pet"] is True
    await panic.release(by="user", pin_ok=True)
    assert not ks.is_engaged() and not privacy.panic
    assert (
        privacy.mode is PrivacyMode.OFFLINE
    )  # staying offline is the user's choice (FULL needs confirm)


def test_unregister_hook() -> None:
    ks = KillSwitchService()

    async def h() -> None:
        return None

    unregister = ks.register_stop_hook("h", h)
    unregister()
    assert ks._hooks == {}
