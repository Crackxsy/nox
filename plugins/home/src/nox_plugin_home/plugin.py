"""The `home` plugin: eleven tools over one authenticated Home Assistant session.

Three properties are worth reading the code for:

* **The boundary is enforced here, not trusted from above.** Every write tool resolves its targets
  against the current inventory, which `nox_plugin_home.inventory` has already filtered through
  `nox.home.boundary`. An entity that is not in that inventory is refused with a logged reason -
  so a lock, an alarm panel, a valve or a garage-door cover is unreachable even if some caller
  hands the tool a perfectly well-formed entity id for one.
* **Privacy modes cut the connection.** `private` and `offline` mean "nothing leaves this machine
  except allow-listed local services", and smart-home control is not an exception. The connection
  loop refuses to dial in those modes and health says exactly that, rather than the plugin
  quietly working because Home Assistant happens to live on loopback.
* **Nothing is faked.** Without a reachable instance every tool returns a structured
  `{"ok": false, "reason": ...}` and health is `unavailable` with the reason a user can act on.

Health: AVAILABLE once authenticated with Home Assistant; LIMITED when authenticated but the area
registry could not be read (the token is not an admin token, so rooms are unknown); UNAVAILABLE
when there is no token, the privacy mode blocks the connection, or Home Assistant is not reachable.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from nox.core.events import HealthStatus
from nox.core.state import PrivacyMode
from nox.home.boundary import ForbiddenEntityError, domain_of, forbidden_reason, require_allowed
from nox.plugins.api import PluginApi
from nox.security.model import Risk

from .inventory import EntityRow, Inventory, build_inventory, filter_attributes
from .models import (
    ClimateInput,
    CoverInput,
    EmptyInput,
    LightInput,
    ListInput,
    MediaInput,
    SceneInput,
    ScriptInput,
    StateInput,
    SwitchInput,
    cover_service,
    light_service,
    media_service,
)
from .settings import resolve_settings, websocket_url
from .ws_client import HomeAssistantClient, HomeCommandError

ACCESS_TOKEN_SECRET = "nox/home/access_token"  # noqa: S105 - a secret *name*, not a value

#: How long one inventory snapshot is reused. Long enough that a burst of tool calls costs one
#: `get_states`, short enough that a light someone flipped by hand is not stale on screen.
INVENTORY_TTL_S = 5.0

#: Privacy modes in which this plugin does not connect at all.
BLOCKED_PRIVACY_MODES = (PrivacyMode.PRIVATE, PrivacyMode.OFFLINE)

#: The area, device and entity registries, or three `None`s when the token may not read them.
RegistryLists = tuple[
    list[dict[str, Any]] | None, list[dict[str, Any]] | None, list[dict[str, Any]] | None
]


class PrivacyBlockedError(PermissionError):
    """The active privacy mode forbids reaching Home Assistant."""


class HomePlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self.settings = resolve_settings(api)
        self._inventory = Inventory(areas_available=False)
        self._inventory_at = 0.0
        self._event_allowance = self.settings.max_state_events_per_s
        self._event_allowance_at = time.monotonic()
        url = websocket_url(self.settings)
        self.client = HomeAssistantClient(
            url,
            token_provider=self._token,
            on_event=self._on_ha_event,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            subscribe_event_types=tuple(
                str(name) for name in api.config.get("subscribe_event_types", ["state_changed"])
            ),
            min_backoff_s=self.settings.min_backoff_s,
            max_backoff_s=self.settings.max_backoff_s,
            request_timeout_s=self.settings.request_timeout_s,
            authorize=lambda: self._authorize(url),
        )

    # -- guards --------------------------------------------------------------------------------

    def _authorize(self, url: str) -> None:
        """Privacy gate first, then the manifest-scoped egress guard.

        The egress guard alone would let a loopback Home Assistant through in `private`/`offline`,
        because the profile has to allow-list that endpoint for the manifest to validate at all.
        Smart-home control is egress, so the privacy modes that cut the network cut it too - and
        that decision belongs here, in code, where it is testable.
        """
        if self.api.privacy.mode in BLOCKED_PRIVACY_MODES:
            raise PrivacyBlockedError(
                f"privacy mode {self.api.privacy.mode.value} blocks the Home Assistant connection"
            )
        parts = urlsplit(url)
        self.api.egress.authorize(
            parts.hostname or "", parts.port or self.settings.port, scheme=parts.scheme or "ws"
        )

    async def _token(self) -> str | None:
        return await self.api.secrets.get(ACCESS_TOKEN_SECRET)

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        self.api.events.on("security.panic", self._on_panic)
        self.client.start()

    async def stop(self) -> None:
        await self.client.stop()

    async def _on_panic(self, _name: str, _payload: dict[str, Any]) -> None:
        """Panic mode forces privacy to OFFLINE; drop the session rather than wait for a timeout.

        Never actuates anything on the way out: turning lights off "for safety" is a side effect
        nobody asked for, and panic never changes the state of the house.
        """
        await self.client.stop()
        self._inventory = Inventory(areas_available=False)
        self.api.log.info("home.panic_disconnected")

    # -- health --------------------------------------------------------------------------------

    async def health(self) -> tuple[HealthStatus, str]:
        if self.api.privacy.mode in BLOCKED_PRIVACY_MODES:
            return (
                HealthStatus.UNAVAILABLE,
                f"privacy mode {self.api.privacy.mode.value}: not connecting to Home Assistant",
            )
        if not self.client.authenticated:
            if await self._token() is None:
                return HealthStatus.UNAVAILABLE, "no access token stored: " + ACCESS_TOKEN_SECRET
            return (
                HealthStatus.UNAVAILABLE,
                self.client.last_error or "not connected to Home Assistant",
            )
        if not self._inventory.areas_available:
            return HealthStatus.LIMITED, "connected, but the area registry is not readable"
        return HealthStatus.AVAILABLE, f"connected (Home Assistant {self.client.ha_version})"

    # -- Home Assistant events -------------------------------------------------------------------

    async def _on_connected(self) -> None:
        self._inventory_at = 0.0
        await self.api.events.emit("home.connected", {"ha_version": self.client.ha_version})

    async def _on_disconnected(self, reason: str) -> None:
        await self.api.events.emit(
            "home.disconnected", {"reason": reason, "backoff_s": self.client.backoff_s}
        )

    def _may_forward_event(self) -> bool:
        """Token bucket over `home.state_changed`; a busy house must not flood the hub."""
        now = time.monotonic()
        rate = self.settings.max_state_events_per_s
        refilled = self._event_allowance + (now - self._event_allowance_at) * rate
        self._event_allowance = min(rate, refilled)
        self._event_allowance_at = now
        if self._event_allowance < 1.0:
            return False
        self._event_allowance -= 1.0
        return True

    async def _on_ha_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type != "state_changed":
            return
        entity_id = str(data.get("entity_id", ""))
        new_state = data.get("new_state") or {}
        attributes = new_state.get("attributes") or {}
        if not entity_id or forbidden_reason(entity_id, attributes) is not None:
            return
        row = self._inventory.by_id(entity_id)
        if row is None:  # an entity outside what `home.list` exposes is not forwarded either
            return
        if not self._may_forward_event():
            return
        await self.api.events.emit(
            "home.state_changed",
            {
                "entity_id": entity_id,
                "state": str(new_state.get("state", "")),
                "attributes": filter_attributes(attributes),
                "area": row.area,
            },
        )

    # -- inventory -----------------------------------------------------------------------------

    async def _refresh_inventory(self, *, force: bool = False) -> Inventory:
        """The current entity/area view, refreshed at most every `INVENTORY_TTL_S`."""
        now = time.monotonic()
        if not force and self._inventory.entities and now - self._inventory_at < INVENTORY_TTL_S:
            return self._inventory
        states = await self.client.get_states()
        areas, devices, entities = await self._registries()
        self._inventory = build_inventory(states, areas=areas, devices=devices, entities=entities)
        self._inventory_at = now
        return self._inventory

    async def _registries(self) -> RegistryLists:
        """The three registries, or `(None, None, None)` when the token may not read them.

        A non-admin long-lived token is a perfectly normal setup; it costs the room names, and the
        plugin says so through `areas_available` and a LIMITED health status rather than pretending
        every device is in no room.
        """
        try:
            areas = await self.client.registry("area")
            devices = await self.client.registry("device")
            entities = await self.client.registry("entity")
        except (HomeCommandError, ConnectionError, TimeoutError) as exc:
            self.api.log.info("home.registry_unavailable", error=str(exc))
            return None, None, None
        return areas, devices, entities

    def _resolve(self, entity_id: str, domain: str) -> EntityRow:
        """One target, past the boundary, the domain check and the configured room list."""
        row = self._inventory.by_id(entity_id)
        if row is None:
            reason = self._inventory.hidden_reason(entity_id) or forbidden_reason(entity_id)
            if reason is not None:
                raise ForbiddenEntityError(entity_id, reason)
            raise ValueError(f"{entity_id!r} is not an entity Nox can see in Home Assistant")
        require_allowed(entity_id, expected_domain=domain, attributes=row.attributes)
        allowed = [area.strip().lower() for area in self.settings.areas_allowed if area.strip()]
        if allowed and row.area.strip().lower() not in allowed:
            raise ValueError(
                f"{entity_id!r} is in {row.area or 'no room'}, which is not in home.areas_allowed"
            )
        return row

    # -- tool plumbing ---------------------------------------------------------------------------

    def _not_connected(self) -> dict[str, Any]:
        if self.api.privacy.mode in BLOCKED_PRIVACY_MODES:
            return {
                "ok": False,
                "connected": False,
                "reason": f"privacy mode {self.api.privacy.mode.value} blocks the connection",
            }
        return {
            "ok": False,
            "connected": False,
            "reason": self.client.last_error or "not connected to Home Assistant",
        }

    async def _act(
        self, domain: str, service: str, entity_ids: Sequence[str], data: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve, refuse or call. Every expected failure comes back structured, never raised."""
        if not self.client.authenticated:
            return self._not_connected()
        try:
            await self._refresh_inventory()
        except (HomeCommandError, ConnectionError, TimeoutError) as exc:
            return {"ok": False, "connected": True, "reason": str(exc)}
        resolved: list[str] = []
        for entity_id in entity_ids:
            try:
                resolved.append(self._resolve(entity_id, domain).entity_id)
            except ForbiddenEntityError as exc:
                self.api.log.warning("home.entity_refused", entity=exc.entity_id, reason=exc.reason)
                return {"ok": False, "connected": True, "reason": exc.reason, "refused": True}
            except ValueError as exc:
                return {"ok": False, "connected": True, "reason": str(exc)}
        try:
            await self.client.call_service(domain, service, entity_ids=resolved, data=data)
        except (HomeCommandError, ConnectionError, TimeoutError) as exc:
            return {"ok": False, "connected": True, "reason": str(exc)}
        # The state a service call produces arrives as a `state_changed` event; forcing a refresh
        # here would report the *old* state as often as the new one.
        self._inventory_at = 0.0
        return {"ok": True, "connected": True, "entity_ids": resolved, "reason": ""}

    # -- tools -------------------------------------------------------------------------------------

    async def status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`home.status.read`: honest connection state, and what to do when there is none."""
        if self.api.privacy.mode in BLOCKED_PRIVACY_MODES:
            return {
                "connected": False,
                "reason": f"privacy mode {self.api.privacy.mode.value} blocks the connection",
                "host": self.settings.host,
                "port": self.settings.port,
                "ha_version": "",
                "areas_available": False,
                "token_present": await self._token() is not None,
            }
        not_connected = self.client.last_error or "not connected"
        return {
            "connected": self.client.authenticated,
            "reason": "" if self.client.authenticated else not_connected,
            "host": self.settings.host,
            "port": self.settings.port,
            "ha_version": self.client.ha_version,
            "areas_available": self._inventory.areas_available,
            "token_present": await self._token() is not None,
        }

    async def list_entities(self, data: ListInput) -> dict[str, Any]:
        """`home.list`: areas and entities, already filtered by the boundary."""
        if not self.client.authenticated:
            return {**self._not_connected(), "entities": [], "areas": [], "areas_available": False}
        try:
            inventory = await self._refresh_inventory()
        except (HomeCommandError, ConnectionError, TimeoutError) as exc:
            return {
                "ok": False,
                "connected": True,
                "reason": str(exc),
                "entities": [],
                "areas": [],
                "areas_available": False,
            }
        rows = [
            row
            for row in inventory.entities
            if (not data.area or row.area.lower() == data.area.strip().lower())
            and (not data.domain or domain_of(row.entity_id) == data.domain.strip().lower())
        ]
        return {
            "ok": True,
            "connected": True,
            "reason": "",
            "entities": [row.as_dict() for row in rows],
            "areas": list(inventory.areas),
            "areas_available": inventory.areas_available,
        }

    async def read_state(self, data: StateInput) -> dict[str, Any]:
        """`home.state`: one entity, or an honest reason why not."""
        if not self.client.authenticated:
            return {**self._not_connected(), "entity": None}
        try:
            inventory = await self._refresh_inventory()
        except (HomeCommandError, ConnectionError, TimeoutError) as exc:
            return {"ok": False, "connected": True, "reason": str(exc), "entity": None}
        reason = inventory.hidden_reason(data.entity_id) or forbidden_reason(data.entity_id)
        if reason is not None:
            self.api.log.warning("home.entity_refused", entity=data.entity_id, reason=reason)
            return {
                "ok": False,
                "connected": True,
                "reason": reason,
                "refused": True,
                "entity": None,
            }
        row = inventory.by_id(data.entity_id)
        if row is None:
            return {
                "ok": False,
                "connected": True,
                "reason": f"{data.entity_id} is not an entity Nox can see",
                "entity": None,
            }
        return {"ok": True, "connected": True, "reason": "", "entity": row.as_dict()}

    async def light(self, data: LightInput) -> dict[str, Any]:
        """`home.light`: on/off, brightness, colour temperature."""
        service, payload = light_service(data)
        return await self._act("light", service, data.entity_ids, payload)

    async def switch(self, data: SwitchInput) -> dict[str, Any]:
        """`home.switch`: on/off."""
        service = "turn_on" if data.on else "turn_off"
        return await self._act("switch", service, data.entity_ids, {})

    async def scene(self, data: SceneInput) -> dict[str, Any]:
        """`home.scene`: activate one of the user's own scenes."""
        return await self._act("scene", "turn_on", [data.entity_id], {})

    async def media(self, data: MediaInput) -> dict[str, Any]:
        """`home.media`: play, pause, skip, volume."""
        service, payload = media_service(data)
        return await self._act("media_player", service, data.entity_ids, payload)

    async def climate(self, data: ClimateInput) -> dict[str, Any]:
        """`home.climate`: target temperature. Mode switching is deliberately not exposed."""
        return await self._act(
            "climate", "set_temperature", data.entity_ids, {"temperature": data.temperature_c}
        )

    async def cover(self, data: CoverInput) -> dict[str, Any]:
        """`home.cover`: blinds and curtains only - the boundary refuses garage doors and gates."""
        service, payload = cover_service(data)
        return await self._act("cover", service, data.entity_ids, payload)

    async def script(self, data: ScriptInput) -> dict[str, Any]:
        """`home.script`: high risk, always confirmed. A script can do anything its author wrote."""
        return await self._act("script", "turn_on", [data.entity_id], {})

    async def automation_trigger(self, data: ScriptInput) -> dict[str, Any]:
        """`home.automation.trigger`: high risk, always confirmed. Conditions are NOT skipped."""
        return await self._act("automation", "trigger", [data.entity_id], {"skip_condition": False})


def create(api: PluginApi) -> HomePlugin:
    plugin = HomePlugin(api)
    api.tools.register(
        "home.status.read",
        EmptyInput,
        plugin.status_read,
        Risk.READ,
        description="Whether Nox is connected to Home Assistant, and why not when it is not.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "home.list",
        ListInput,
        plugin.list_entities,
        Risk.READ,
        description="List the areas and entities Nox can see, with their current state.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "home.state",
        StateInput,
        plugin.read_state,
        Risk.READ,
        description="Read the current state of one entity.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "home.light",
        LightInput,
        plugin.light,
        Risk.MEDIUM,
        description="Switch lights on or off, set brightness or colour temperature.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.switch",
        SwitchInput,
        plugin.switch,
        Risk.MEDIUM,
        description="Switch a socket or switch entity on or off.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.scene",
        SceneInput,
        plugin.scene,
        Risk.MEDIUM,
        description="Activate a scene defined in Home Assistant.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.media",
        MediaInput,
        plugin.media,
        Risk.MEDIUM,
        description="Control a media player: play, pause, skip, volume.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.climate",
        ClimateInput,
        plugin.climate,
        Risk.MEDIUM,
        description="Set the target temperature of a thermostat.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.cover",
        CoverInput,
        plugin.cover,
        Risk.MEDIUM,
        description="Open, close or position blinds and curtains. Never garage doors or gates.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.script",
        ScriptInput,
        plugin.script,
        Risk.HIGH,
        description="Run a Home Assistant script. Always asks first.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "home.automation.trigger",
        ScriptInput,
        plugin.automation_trigger,
        Risk.HIGH,
        description="Trigger a Home Assistant automation. Always asks first.",
        side_effects=True,
        local=True,
    )
    return plugin
