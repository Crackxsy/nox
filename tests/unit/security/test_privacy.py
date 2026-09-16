"""Privacy service: modes, confirmation for FULL, capture flags, zone matching, events."""

from __future__ import annotations

import pytest

from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.security.privacy import BUILTIN_ZONES, PrivacyService, ZoneSpec
from tests.unit.fakes import FakeBus


async def test_switch_to_private_and_offline_always_allowed(
    privacy: PrivacyService, bus: FakeBus
) -> None:
    change = await privacy.set_mode(PrivacyMode.PRIVATE, by="hotkey")
    assert change.applied and privacy.mode is PrivacyMode.PRIVATE
    change = await privacy.set_mode(PrivacyMode.OFFLINE, by="telegram")
    assert change.applied and privacy.mode is PrivacyMode.OFFLINE
    modes = [e.payload for e in bus.published if e.name == E.PRIVACY_MODE_CHANGED]
    assert modes[0]["previous"] == "balanced" and modes[0]["current"] == "private"
    assert modes[1]["current"] == "offline" and modes[1]["by"] == "telegram"
    assert E.PRIVACY_CAPTURE_CHANGED in bus.names()


async def test_switch_to_full_requires_confirmation(privacy: PrivacyService) -> None:
    change = await privacy.set_mode(PrivacyMode.FULL, by="user")
    assert (
        not change.applied and change.requires_confirmation and privacy.mode is PrivacyMode.BALANCED
    )
    change = await privacy.set_mode(PrivacyMode.FULL, by="user", confirmed=True)
    assert change.applied and privacy.mode is PrivacyMode.FULL
    same = await privacy.set_mode(PrivacyMode.FULL, by="user", confirmed=True)
    assert not same.applied and not same.requires_confirmation


async def test_mode_permission_matrix(privacy: PrivacyService) -> None:
    await privacy.set_mode(PrivacyMode.FULL, confirmed=True)
    assert (
        privacy.allows_cloud()
        and privacy.allows_screenshot_to_cloud()
        and privacy.allows_memory_write()
    )
    await privacy.set_mode(PrivacyMode.BALANCED)
    assert privacy.allows_cloud() and not privacy.allows_screenshot_to_cloud()
    await privacy.set_mode(PrivacyMode.PRIVATE)
    assert not privacy.allows_cloud() and not privacy.allows_memory_write()
    assert privacy.allows_capture("microphone")  # local capture stays possible
    await privacy.set_mode(PrivacyMode.OFFLINE)
    assert not privacy.allows_cloud() and privacy.allows_memory_write()


def test_capture_flags_default_from_config(privacy: PrivacyService) -> None:
    assert privacy.allows_capture("microphone") and privacy.allows_capture("screen")
    assert not privacy.allows_capture("camera")


async def test_set_capture_emits_capture_changed(privacy: PrivacyService, bus: FakeBus) -> None:
    current = await privacy.set_capture(microphone=False, camera=True)
    assert current.microphone is False and current.camera is True and current.cloud is True
    assert bus.published[-1].name == E.PRIVACY_CAPTURE_CHANGED
    assert bus.published[-1].payload["microphone"] is False
    assert privacy.state.microphone is False and privacy.state.camera is True


@pytest.mark.parametrize(
    "title,process,zone",
    [
        ("Sparkasse Online-Banking - Firefox", "firefox.exe", "banking"),
        ("KeePassXC", "KeePassXC.exe", "password_manager"),
        ("", "bitwarden.exe", "password_manager"),
        ("Posteingang - Outlook", "OUTLOOK.EXE", "email"),
        ("WhatsApp", "WhatsApp.exe", "private_chats"),
        ("Steuererklärung 2025.pdf", "AcroRd32.exe", "personal_documents"),
        ("#general - Discord", "Discord.exe", "discord"),
        ("Visual Studio Code", "Code.exe", None),
        ("Rocket League", "RocketLeague.exe", None),
    ],
)
def test_builtin_zone_matching(
    privacy: PrivacyService, title: str, process: str, zone: str | None
) -> None:
    assert privacy.match_zone(title, process) == zone
    assert privacy.zone_active(title, process) is (zone is not None)


def test_path_zone_matching(privacy: PrivacyService) -> None:
    assert privacy.path_zone(r"E:\Dokumente\Privat\vertrag.pdf") == "personal_documents"
    assert privacy.path_zone("D:/Projects/app/README.md") is None
    assert privacy.path_zone("") is None


def test_custom_zone_and_builtin_extension() -> None:
    svc = PrivacyService(
        zones=[
            {"id": "work_vpn", "window_titles": ["*ACME*"], "processes": ["citrix*"]},
            {"id": "discord", "processes": ["vesktop*"]},
            ZoneSpec(id="x", paths=["*/secret/*"]),
        ]
    )
    assert svc.match_zone("ACME Jira", "") == "work_vpn"
    assert svc.match_zone("", "Vesktop.exe") == "discord"
    assert svc.match_zone("#general - Discord", "") == "discord"  # builtin patterns kept
    assert svc.path_zone("C:/x/secret/y") == "x"
    with pytest.raises(ValueError):
        PrivacyService(zones=["not-a-builtin"])
    assert set(BUILTIN_ZONES) >= {
        "banking",
        "password_manager",
        "email",
        "private_chats",
        "personal_documents",
        "discord",
    }


async def test_zone_active_blocks_capture_and_memory_but_never_leaks_title(
    privacy: PrivacyService,
    bus: FakeBus,
) -> None:
    from nox.security.audit import SqliteAuditLog

    zone = await privacy.observe_foreground("PayPal - Zahlung an Max", "chrome.exe")
    assert zone == "banking" and privacy.active_zone == "banking"
    assert not privacy.allows_capture("screen") and not privacy.allows_memory_write()
    assert not privacy.allows_screenshot_to_cloud()
    assert privacy.snapshot().zone_active
    assert (
        bus.published[-1].name == E.PRIVACY_CAPTURE_CHANGED
        and bus.published[-1].payload["screen"] is False
    )
    audit = privacy._audit
    assert isinstance(audit, SqliteAuditLog)
    rows = audit.entries()
    assert all("PayPal" not in e.target and "Max" not in e.target for e in rows)
    assert await privacy.observe_foreground("Notepad", "notepad.exe") is None
    assert privacy.allows_capture("screen")


async def test_safe_mode_and_panic_cut_capture_and_cloud(
    privacy: PrivacyService,
    safe_mode_flag: dict[str, bool],
) -> None:
    safe_mode_flag["on"] = True
    assert not privacy.allows_capture("microphone") and not privacy.allows_cloud()
    assert privacy.snapshot().safe_mode
    safe_mode_flag["on"] = False
    await privacy.set_panic(True, by="hotkey")
    assert privacy.panic and privacy.state.panic and not privacy.allows_capture("screen")
    assert not privacy.allows_cloud() and privacy.snapshot().panic


def test_from_config_reads_defaults_yaml() -> None:
    import yaml

    from .conftest import DEFAULTS_YAML

    cfg = yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8"))
    svc = PrivacyService.from_config(cfg["privacy"])
    assert svc.mode is PrivacyMode.BALANCED
    assert len(svc.zones) == 6
    assert svc.allows_capture("screen") and not svc.allows_capture("camera")
