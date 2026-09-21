"""A fake Home Assistant WebSocket server for the `home` plugin's tests.

Speaks just enough of the documented protocol to exercise the real client against: the
`auth_required` -> `auth` -> `auth_ok`/`auth_invalid` handshake with a real token check,
`id`-correlated `result` frames from a scriptable table, `subscribe_events` acknowledgement, and
`event` broadcast to every authenticated client.

A real `websockets.serve` on an OS-granted port, not an in-process connector double: the plugin's
`HomeAssistantClient` opens a genuine TCP connection to it, so the handshake, the framing and the
reconnect loop are all exercised for real. Shared by the unit tests and
`tests/integration/test_home_plugin.py`.

There is no real Home Assistant anywhere in this repository's test suite, and there never will be:
every value below is a fixture.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

HA_VERSION = "2026.9.0"
VALID_TOKEN = "test-long-lived-token"  # noqa: S105 - a test fixture value, never a real credential


def default_states() -> list[dict[str, Any]]:
    """A small, deliberately awkward house: two rooms, one of every controllable domain, plus the
    three things Nox must never expose (a lock, an alarm panel and a garage door)."""
    return [
        _state("light.wz_decke", "on", {"friendly_name": "Deckenlampe", "brightness": 200}),
        _state("light.wz_steh", "off", {"friendly_name": "Stehlampe"}),
        _state("light.kueche", "on", {"friendly_name": "Küchenlicht"}),
        _state("switch.kaffee", "off", {"friendly_name": "Kaffeemaschine"}),
        _state("media_player.wz", "playing", {"friendly_name": "Fernseher", "volume_level": 0.4}),
        _state("climate.bad", "heat", {"friendly_name": "Heizung Bad", "current_temperature": 19}),
        _state("cover.wz_rollo", "open", {"friendly_name": "Rollladen", "device_class": "blind"}),
        _state("scene.kino", "unknown", {"friendly_name": "Kinoabend"}),
        _state("script.gute_nacht", "off", {"friendly_name": "Gute Nacht"}),
        _state("automation.morgens", "on", {"friendly_name": "Morgenroutine"}),
        _state(
            "sensor.wz_temp",
            "21.5",
            {"friendly_name": "Temperatur", "device_class": "temperature"},
        ),
        # Never exposed, whatever any layer above does:
        _state("lock.haustuer", "locked", {"friendly_name": "Haustür"}),
        _state("alarm_control_panel.haus", "disarmed", {"friendly_name": "Alarmanlage"}),
        _state("valve.wasser", "closed", {"friendly_name": "Hauptventil"}),
        _state("cover.garage", "closed", {"friendly_name": "Garagentor", "device_class": "garage"}),
        _state("person.someone", "home", {"friendly_name": "Jemand"}),
    ]


def _state(entity_id: str, state: str, attributes: dict[str, Any]) -> dict[str, Any]:
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def default_registries() -> dict[str, list[dict[str, Any]]]:
    """Area/device/entity registries that put the two rooms on the entities above."""
    return {
        "config/area_registry/list": [
            {"area_id": "wz", "name": "Wohnzimmer"},
            {"area_id": "kueche", "name": "Küche"},
            {"area_id": "bad", "name": "Bad"},
        ],
        "config/device_registry/list": [{"id": "dev1", "name": "Hub", "area_id": "wz"}],
        "config/entity_registry/list": [
            {"entity_id": "light.wz_decke", "area_id": "wz"},
            {"entity_id": "light.wz_steh", "device_id": "dev1"},
            {"entity_id": "light.kueche", "area_id": "kueche"},
            {"entity_id": "switch.kaffee", "area_id": "kueche"},
            {"entity_id": "media_player.wz", "area_id": "wz"},
            {"entity_id": "climate.bad", "area_id": "bad"},
            {"entity_id": "cover.wz_rollo", "area_id": "wz"},
            {"entity_id": "scene.kino", "area_id": "wz"},
            {"entity_id": "sensor.wz_temp", "area_id": "wz"},
        ],
    }


class FakeHomeAssistant:
    """One fake instance. `token=None` accepts any token; anything else must match exactly."""

    def __init__(self, *, token: str | None = VALID_TOKEN, admin: bool = True) -> None:
        self.token = token
        self.admin = admin
        self.port = 0
        self.auth_count = 0
        self.service_calls: list[dict[str, Any]] = []
        self.subscriptions: list[str] = []
        self.responses: dict[str, Any] = {
            "get_states": lambda _d: default_states(),
            "call_service": lambda _d: {"context": {"id": "ctx"}},
        }
        if admin:
            for name, rows in default_registries().items():
                self.responses[name] = rows
        self._server: Any = None
        self._clients: set[ServerConnection] = set()

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/websocket"

    def set_response(self, command: str, value: Callable[[dict[str, Any]], Any] | Any) -> None:
        self.responses[command] = value

    async def disconnect_all(self) -> None:
        for ws in list(self._clients):
            await ws.close()

    async def broadcast_state_changed(
        self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
    ) -> None:
        message = json.dumps(
            {
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": entity_id,
                        "new_state": _state(entity_id, state, attributes or {}),
                    },
                },
            }
        )
        for ws in list(self._clients):
            try:
                await ws.send(message)
            except websockets.ConnectionClosed:  # pragma: no cover - a racing disconnect
                pass

    # -- protocol ------------------------------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "auth_required", "ha_version": HA_VERSION}))
        try:
            message = json.loads(await ws.recv())
        except websockets.ConnectionClosed:  # pragma: no cover - a racing disconnect
            return
        provided = message.get("access_token")
        if self.token is not None and provided != self.token:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            await ws.close()
            return
        self.auth_count += 1
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": HA_VERSION}))
        self._clients.add(ws)
        try:
            async for raw in ws:
                await self._command(ws, json.loads(raw))
        except websockets.ConnectionClosed:  # pragma: no cover - the client went away
            pass
        finally:
            self._clients.discard(ws)

    async def _command(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        command_id = frame.get("id")
        command_type = str(frame.get("type", ""))
        if command_type == "subscribe_events":
            self.subscriptions.append(str(frame.get("event_type", "")))
            await ws.send(
                json.dumps({"id": command_id, "type": "result", "success": True, "result": None})
            )
            return
        if command_type == "call_service":
            self.service_calls.append(dict(frame))
        scripted = self.responses.get(command_type)
        if scripted is None:
            await ws.send(
                json.dumps(
                    {
                        "id": command_id,
                        "type": "result",
                        "success": False,
                        "error": {"code": "unknown_command", "message": command_type},
                    }
                )
            )
            return
        if isinstance(scripted, Exception):
            await ws.send(
                json.dumps(
                    {
                        "id": command_id,
                        "type": "result",
                        "success": False,
                        "error": {"code": "failed", "message": str(scripted)},
                    }
                )
            )
            return
        result = scripted(frame) if callable(scripted) else scripted
        await ws.send(
            json.dumps({"id": command_id, "type": "result", "success": True, "result": result})
        )


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll `predicate` instead of sleeping a magic number; raises when it never becomes true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was never reached")
