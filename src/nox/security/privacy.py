"""Privacy modes, capture flags and privacy zones.

A zone is a local sensor result, not a policy: `observe_foreground(window_title, process_name)`
matches the foreground window against title and process patterns, and `path_zone()` matches paths.
The window title that caused a match is never logged and never audited - only the zone id, and
only as a boolean on the bus.

Tightening privacy is always allowed. Relaxing it is not: switching to FULL needs an explicit
confirmation, and when a PIN is configured the IPC layer asks for it first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nox.core.events import CaptureChanged, E, EventBus, PrivacyModeChanged
from nox.core.globbing import value_matches
from nox.core.state import PrivacyMode, PrivacyState
from nox.security._events import publish
from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.model import AuditLog
from nox.security.permissions import PrivacySnapshot

if TYPE_CHECKING:  # avoids a runtime cycle: the config package reads the security hard list
    from nox.core.config import PrivacyConfig

log = get_logger(__name__)

CaptureKind = Literal["microphone", "camera", "screen"]
CAPTURE_KINDS: tuple[CaptureKind, ...] = ("microphone", "camera", "screen")
Clock = Callable[[], datetime]


class ZoneSpec(BaseModel):
    """Patterns (case-insensitive globs) that put an app/window/path into a privacy zone."""

    model_config = ConfigDict(frozen=True)
    id: str
    window_titles: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


BUILTIN_ZONES: dict[str, ZoneSpec] = {
    "banking": ZoneSpec(
        id="banking",
        window_titles=[
            "*bank*",
            "*sparkasse*",
            "*volksbank*",
            "*paypal*",
            "*n26*",
            "*ing-diba*",
            "*comdirect*",
            "*trade republic*",
            "*online-banking*",
            "*onlinebanking*",
            "*finanzen*",
            "*depot*",
        ],
    ),
    "password_manager": ZoneSpec(
        id="password_manager",
        window_titles=[
            "*keepass*",
            "*bitwarden*",
            "*1password*",
            "*lastpass*",
            "*dashlane*",
            "*passwort*",
            "*password*",
        ],
        processes=["keepass*", "bitwarden*", "1password*", "lastpass*", "dashlane*"],
    ),
    "email": ZoneSpec(
        id="email",
        window_titles=[
            "*outlook*",
            "*thunderbird*",
            "*gmail*",
            "*posteingang*",
            "*proton mail*",
            "*protonmail*",
            "*gmx*",
            "*web.de*",
            "*e-mail*",
        ],
        processes=["outlook*", "thunderbird*", "olk*", "hxoutlook*"],
    ),
    "private_chats": ZoneSpec(
        id="private_chats",
        window_titles=[
            "*whatsapp*",
            "*signal*",
            "*telegram*",
            "*threema*",
            "*messenger*",
            "*imessage*",
        ],
        processes=["whatsapp*", "signal*", "telegram*", "threema*"],
    ),
    "personal_documents": ZoneSpec(
        id="personal_documents",
        window_titles=[
            "*steuer*",
            "*versicherung*",
            "*lohnabrechnung*",
            "*gehaltsabrechnung*",
            "*arztbrief*",
            "*befund*",
        ],
        paths=[
            "*/dokumente/privat/*",
            "*/documents/private/*",
            "*/persönlich/*",
            "*/personal/*",
            "*/steuer*",
            "*/versicherung*",
            "*/gesundheit/*",
            "*/health/*",
        ],
    ),
    "discord": ZoneSpec(id="discord", window_titles=["*discord*"], processes=["discord*"]),
}


class PrivacyModeChange(BaseModel):
    model_config = ConfigDict(frozen=True)
    previous: PrivacyMode
    current: PrivacyMode
    applied: bool
    requires_confirmation: bool = False
    by: str = "user"


def _build_zone(item: str | Mapping[str, Any] | ZoneSpec) -> ZoneSpec:
    if isinstance(item, ZoneSpec):
        return item
    if isinstance(item, str):
        spec = BUILTIN_ZONES.get(item.strip().lower())
        if spec is None:
            raise ValueError(f"unknown built-in privacy zone {item!r}; use a mapping with patterns")
        return spec
    data = dict(item)
    zone_id = str(data.get("id", "")).strip().lower()
    base = BUILTIN_ZONES.get(zone_id)
    if base is not None:
        merged = base.model_dump()
        for key in ("window_titles", "processes", "paths"):
            merged[key] = list(merged[key]) + [str(p) for p in data.get(key, [])]
        return ZoneSpec.model_validate(merged)
    return ZoneSpec.model_validate(data)


class PrivacyService:
    """Owns the live PrivacyState; implements `PrivacyStateProvider` for the permission engine."""

    def __init__(
        self,
        *,
        mode: PrivacyMode = PrivacyMode.BALANCED,
        capture: Mapping[str, bool] | None = None,
        zones: Sequence[str | Mapping[str, Any] | ZoneSpec] = (),
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        safe_mode: Callable[[], bool] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._bus = bus
        self._audit = SafeAuditLog(audit, tool="privacy")
        self._safe_mode: Callable[[], bool] = safe_mode or (lambda: False)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._mode = mode
        flags = {"microphone": True, "camera": False, "screen": True}
        flags.update({k: bool(v) for k, v in (capture or {}).items() if k in flags})
        self._capture: dict[str, bool] = flags
        self._zones: tuple[ZoneSpec, ...] = tuple(_build_zone(z) for z in zones)
        self._active_zone: str | None = None
        self._panic = False

    @classmethod
    def from_config(cls, privacy_config: PrivacyConfig, **kwargs: Any) -> PrivacyService:
        """Build from the typed `privacy` section of `NoxConfig`."""
        return cls(
            mode=PrivacyMode(privacy_config.mode),
            capture=privacy_config.capture.model_dump(),
            zones=list(privacy_config.zones),
            **kwargs,
        )

    # ---- read side -------------------------------------------------------------------------------

    def set_safe_mode_source(self, is_engaged: Callable[[], bool]) -> None:
        """Tell the privacy service how to see the kill switch.

        Called once, right after the kill switch is built. The two services genuinely need each
        other, and naming the dependency here is clearer than handing the privacy service a
        mutable cell that is filled in later.
        """
        self._safe_mode = is_engaged

    @property
    def mode(self) -> PrivacyMode:
        return self._mode

    @property
    def zones(self) -> tuple[ZoneSpec, ...]:
        return self._zones

    @property
    def active_zone(self) -> str | None:
        return self._active_zone

    @property
    def panic(self) -> bool:
        return self._panic

    @property
    def capture_flags(self) -> dict[str, bool]:
        return dict(self._capture)

    @property
    def state(self) -> PrivacyState:
        return PrivacyState(
            mode=self._mode,
            microphone=self.allows_capture("microphone"),
            camera=self.allows_capture("camera"),
            screen=self.allows_capture("screen"),
            panic=self._panic,
        )

    def snapshot(self) -> PrivacySnapshot:
        return PrivacySnapshot(
            mode=self._mode,
            zone_active=self._active_zone is not None,
            safe_mode=self._safe_mode(),
            panic=self._panic,
        )

    def allows_cloud(self) -> bool:
        return (
            self._mode in (PrivacyMode.FULL, PrivacyMode.BALANCED)
            and not self._panic
            and not self._safe_mode()
        )

    def allows_capture(self, kind: CaptureKind) -> bool:
        return (
            self._capture.get(kind, False)
            and not self._panic
            and not self._safe_mode()
            and self._active_zone is None
        )

    def allows_memory_write(self) -> bool:
        return (
            self._mode is not PrivacyMode.PRIVATE
            and self._active_zone is None
            and not self._safe_mode()
        )

    def allows_screenshot_to_cloud(self) -> bool:
        return self._mode is PrivacyMode.FULL and self.allows_cloud() and self._active_zone is None

    def effective_capture(self) -> CaptureChanged:
        return CaptureChanged(
            microphone=self.allows_capture("microphone"),
            camera=self.allows_capture("camera"),
            screen=self.allows_capture("screen"),
            cloud=self.allows_cloud(),
        )

    # ---- zones -----------------------------------------------------------------------------------

    def match_zone(self, window_title: str = "", process_name: str = "") -> str | None:
        title = window_title.strip()
        process = process_name.strip()
        for zone in self._zones:
            if title and any(value_matches(title, p) for p in zone.window_titles):
                return zone.id
            if process and any(value_matches(process, p) for p in zone.processes):
                return zone.id
        return None

    def path_zone(self, path: str) -> str | None:
        text = path.replace("\\", "/").strip()
        if not text:
            return None
        for zone in self._zones:
            if any(value_matches(text, p) for p in zone.paths):
                return zone.id
        return None

    def zone_active(self, window_title: str = "", process_name: str = "") -> bool:
        return self.match_zone(window_title, process_name) is not None

    async def observe_foreground(self, window_title: str, process_name: str = "") -> str | None:
        """Update the active zone from the foreground window; emits capture_changed on change."""
        zone = self.match_zone(window_title, process_name)
        if zone == self._active_zone:
            return zone
        previous = self._active_zone
        self._active_zone = zone
        self._audit.append(
            actor="sensor",
            action="zone.enter" if zone else "zone.leave",
            target=zone or previous or "",
            decision="allow",
            result="ok",
        )
        # Formalizes the event `PetService._on_zone` already subscribes to by this literal name
        # Only the boolean and the zone id ever go on the bus, never the
        # window title/process that triggered the match. Published before CAPTURE_CHANGED so
        # existing callers that assert "the last published event is capture_changed" still hold.
        await publish(self._bus, E.PRIVACY_ZONE_CHANGED, {"active": zone is not None, "zone": zone})
        await publish(self._bus, E.PRIVACY_CAPTURE_CHANGED, self.effective_capture())
        log.info("privacy.zone_changed", zone=zone)
        return zone

    # ---- write side ------------------------------------------------------------------------------

    async def set_mode(
        self, mode: PrivacyMode, *, by: str = "user", confirmed: bool = False
    ) -> PrivacyModeChange:
        mode = PrivacyMode(mode)
        previous = self._mode
        if mode is PrivacyMode.FULL and previous is not PrivacyMode.FULL and not confirmed:
            self._audit.append(
                actor=by,
                action="mode.set",
                target=mode.value,
                decision="confirm",
                result="pending",
            )
            return PrivacyModeChange(
                previous=previous,
                current=previous,
                applied=False,
                requires_confirmation=True,
                by=by,
            )
        if mode is previous:
            return PrivacyModeChange(previous=previous, current=previous, applied=False, by=by)
        self._mode = mode
        self._audit.append(
            actor=by,
            action="mode.set",
            target=mode.value,
            decision="allow",
            result="ok",
            details={"previous": previous.value},
        )
        await publish(
            self._bus,
            E.PRIVACY_MODE_CHANGED,
            PrivacyModeChanged(previous=previous.value, current=mode.value, by=by),
        )
        await publish(self._bus, E.PRIVACY_CAPTURE_CHANGED, self.effective_capture())
        log.info("privacy.mode_changed", previous=previous.value, current=mode.value, by=by)
        return PrivacyModeChange(previous=previous, current=mode, applied=True, by=by)

    async def set_capture(
        self,
        *,
        microphone: bool | None = None,
        camera: bool | None = None,
        screen: bool | None = None,
        by: str = "user",
    ) -> CaptureChanged:
        changes = {"microphone": microphone, "camera": camera, "screen": screen}
        for kind, value in changes.items():
            if value is not None:
                self._capture[kind] = bool(value)
        self._audit.append(
            actor=by,
            action="capture.set",
            target="",
            decision="allow",
            result="ok",
            details={k: str(v) for k, v in changes.items() if v is not None},
        )
        current = self.effective_capture()
        await publish(self._bus, E.PRIVACY_CAPTURE_CHANGED, current)
        return current

    async def set_panic(self, active: bool, *, by: str = "user") -> None:
        if active == self._panic:
            return
        self._panic = active
        self._audit.append(
            actor=by,
            action="panic.set",
            target=str(active).lower(),
            decision="allow",
            result="ok",
        )
        await publish(self._bus, E.PRIVACY_CAPTURE_CHANGED, self.effective_capture())
