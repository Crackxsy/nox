"""Privacy modes, capture flags and privacy zones.

A zone is a local sensor result, not a policy: `observe_foreground(window_title, process_name)`
matches the foreground window against title and process patterns, and `path_zone()` matches paths.
When the foreground window cannot be observed at all (a Wayland session, a missing permission),
`observe_foreground_unobservable()` enters the reserved `unobservable` zone, because "cannot see a
banking window" is not "no banking window". What that zone closes is `privacy.unobservable_policy`:
`strict` closes every gate a real zone closes; `screen_only` (the shipped default) keeps the screen,
the camera, screenshots to the cloud and clipboard reads closed but leaves the microphone and memory
writes open - the risk of an unseen window is what the screen shows, not what the user says. A real
zone (banking, a password manager, ...) always closes everything. `privacy.zones_enabled: false`
switches window zones off altogether; path zones for vault notes stay in force either way. The
window title that caused a match is never logged and never audited - only the zone id, and only as
a boolean on the bus.

Every gate question goes through `_zone_closes(gate)`, so the capture flags on the bus, the
permission engine's snapshot (`zone_active` plus `zone_screen_only`) and the memory gate can never
disagree about what the unobservable zone allows.

Tightening privacy is always allowed. Relaxing it is not: switching to FULL needs an explicit
confirmation, and when a PIN is configured the IPC layer asks for it first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nox.core.events import (
    CaptureChanged,
    E,
    EventBus,
    PrivacyModeChanged,
    PrivacyZoneChanged,
)
from nox.core.globbing import value_matches
from nox.core.state import PrivacyMode, PrivacyState
from nox.security._events import publish
from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.model import AuditLog
from nox.security.permissions import SCREEN_ONLY_OPEN_GATES, PrivacySnapshot, ZoneGate

if TYPE_CHECKING:  # avoids a runtime cycle: the config package reads the security hard list
    from nox.core.config import PrivacyConfig

log = get_logger(__name__)

CaptureKind = Literal["microphone", "camera", "screen"]
CAPTURE_KINDS: tuple[CaptureKind, ...] = ("microphone", "camera", "screen")
#: `privacy.unobservable_policy`: what the fail-closed zone closes (module docstring).
UnobservablePolicy = Literal["screen_only", "strict"]
Clock = Callable[[], datetime]

#: The zone in force while the foreground window cannot be read. Reserved: a configured zone with
#: this id is rejected, so the fail-closed state can never be confused with a user-defined one.
UNOBSERVABLE_ZONE = "unobservable"


class ZoneSpec(BaseModel):
    """Patterns (case-insensitive globs) that put an app/window/path into a privacy zone."""

    model_config = ConfigDict(frozen=True)
    id: str
    window_titles: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


#: The built-in zones. Title patterns are matched against the raw title *and* against its words
#: (`title_words`: lowercase, split on anything that is not a letter, digit or underscore, padded
#: with a space at each end), so `"* bank *"` matches the word "Bank" in "Meine Bank - Übersicht"
#: but not "Datenbank", and `"* depot *"` matches "Depot" but not "depot_tools". Brand names that
#: are never an ordinary word ("sparkasse", "keepass") stay substring patterns. A pattern without
#: spaces behaves exactly as before, so a user's own `*bank*` still means what it says.
BUILTIN_ZONES: dict[str, ZoneSpec] = {
    "banking": ZoneSpec(
        id="banking",
        window_titles=[
            "* bank *",
            "* banking *",
            "*onlinebanking*",
            "*internetbanking*",
            "*sparkasse*",
            "*volksbank*",
            "*raiffeisen*",
            "*commerzbank*",
            "*postbank*",
            "*consorsbank*",
            "*targobank*",
            "*hypovereinsbank*",
            "* dkb *",
            "*comdirect*",
            "*ing-diba*",
            "* n26 *",
            "*paypal*",
            "*trade republic*",
            "* finanzen *",
            "* depot *",
            "*wertpapierdepot*",
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
            "* passwort *",
            "* passwörter *",
            "*passwortmanager*",
            "* password *",
            "* passwords *",
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
            "* gmx *",
            "*web.de*",
            "*e-mail*",
        ],
        processes=["outlook*", "thunderbird*", "olk*", "hxoutlook*"],
    ),
    "private_chats": ZoneSpec(
        id="private_chats",
        window_titles=[
            "*whatsapp*",
            # The Signal and Messenger apps title their window with the bare name; "signal" or
            # "messenger" inside a longer title is usually code ("signal.py") or prose.
            "signal",
            "signal (*",
            "* telegram *",
            "*threema*",
            "messenger",
            "messenger |*",
            "*| messenger",
            "*facebook messenger*",
            "*imessage*",
        ],
        processes=["whatsapp*", "signal*", "telegram*", "threema*"],
    ),
    "personal_documents": ZoneSpec(
        id="personal_documents",
        window_titles=[
            "* steuer *",
            "*steuererklärung*",
            "*steuerbescheid*",
            "*einkommensteuer*",
            "*lohnsteuer*",
            "* elster *",
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
            "*/steuer/*",
            "*/steuern/*",
            "*/steuererklärung*",
            "*/versicherung*",
            "*/gesundheit/*",
            "*/health/*",
        ],
    ),
    "discord": ZoneSpec(id="discord", window_titles=["*discord*"], processes=["discord*"]),
}

_NOT_A_WORD = re.compile(r"[^\w]+", re.UNICODE)


def title_words(title: str) -> str:
    """`" meine bank übersicht "`: the title as lowercase words, padded, for word patterns."""
    return f" {_NOT_A_WORD.sub(' ', title.lower()).strip()} "


def title_matches(title: str, pattern: str) -> bool:
    """A title pattern matches the raw title, or - for word patterns - the title's words."""
    return value_matches(title, pattern) or value_matches(title_words(title), pattern)


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
    if zone_id == UNOBSERVABLE_ZONE:
        raise ValueError(f"privacy zone id {UNOBSERVABLE_ZONE!r} is reserved")
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
        zones_enabled: bool = True,
        unobservable_policy: UnobservablePolicy = "strict",
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
        self._zones_enabled = zones_enabled
        self._unobservable_policy: UnobservablePolicy = unobservable_policy

    @classmethod
    def from_config(cls, privacy_config: PrivacyConfig, **kwargs: Any) -> PrivacyService:
        """Build from the typed `privacy` section of `NoxConfig`."""
        return cls(
            mode=PrivacyMode(privacy_config.mode),
            capture=privacy_config.capture.model_dump(),
            zones=list(privacy_config.zones),
            zones_enabled=privacy_config.zones_enabled,
            unobservable_policy=privacy_config.unobservable_policy,
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
    def zones_enabled(self) -> bool:
        """False only when `privacy.zones_enabled: false` switched window zones off."""
        return self._zones_enabled

    @property
    def unobservable_policy(self) -> UnobservablePolicy:
        return self._unobservable_policy

    @property
    def zone_screen_only(self) -> bool:
        """The zone in force is the unobservable one, and it closes only screen-side gates."""
        return self._active_zone == UNOBSERVABLE_ZONE and self._unobservable_policy == "screen_only"

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
            zone_screen_only=self.zone_screen_only,
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
            and not self._zone_closes(kind)
        )

    def allows_memory_write(self) -> bool:
        return (
            self._mode is not PrivacyMode.PRIVATE
            and not self._zone_closes("memory")
            and not self._safe_mode()
        )

    def allows_screenshot_to_cloud(self) -> bool:
        return (
            self._mode is PrivacyMode.FULL
            and self.allows_cloud()
            and not self._zone_closes("screen")
        )

    def _zone_closes(self, gate: ZoneGate) -> bool:
        """Whether the zone in force closes `gate`. The one place that answers it."""
        if self._active_zone is None:
            return False
        if self.zone_screen_only:
            return gate not in SCREEN_ONLY_OPEN_GATES
        return True

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
            if title and any(title_matches(title, p) for p in zone.window_titles):
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
        """Update the active zone from the foreground window; emits capture_changed on change.

        With window zones switched off (`privacy.zones_enabled: false`) nothing is entered.
        """
        if not self._zones_enabled:
            return None
        return await self._enter_zone(self.match_zone(window_title, process_name))

    async def observe_foreground_unobservable(self, process_name: str = "") -> str | None:
        """The foreground window's title could not be read: fail closed.

        A process pattern that matches still names its own zone (it is the more specific answer);
        otherwise the reserved `UNOBSERVABLE_ZONE` applies. Either way a zone is active - unless
        window zones are switched off, when this returns None and nothing changes.
        """
        if not self._zones_enabled:
            return None
        zone = self.match_zone("", process_name) or UNOBSERVABLE_ZONE
        await self._enter_zone(zone)
        return zone

    async def _enter_zone(self, zone: str | None) -> str | None:
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
        await publish(
            self._bus,
            E.PRIVACY_ZONE_CHANGED,
            PrivacyZoneChanged(
                active=zone is not None, zone=zone, screen_only=self.zone_screen_only
            ),
        )
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
