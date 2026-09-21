"""`plugin.status`: the dashboard can ask why a plugin is not running.

A plugin that the active profile refuses fails validation during boot, and says so in a
`plugin.failed` event - minutes before any UI connects. Without this request the stream panel can
only show an empty list; with it, it can name the plugin and repeat the profile's own reason.
"""

from __future__ import annotations

from typing import Any, cast

from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION
from nox.ipc.handlers.core import UI_ROLES, CoreHandlers
from nox.ipc.protocol import Envelope, Kind, PluginStatusList, Source

REFUSED = (
    "plugin 'obs' declares egress '127.0.0.1:4455' which the active profile 'companion' does not "
    "allow"
)


class _Status:
    def __init__(self, plugin_id: str, state: str, reason: str = "") -> None:
        self.plugin_id = plugin_id
        self.state = _State(state)
        self.reason = reason


class _State:
    def __init__(self, value: str) -> None:
        self.value = value


class _Plugins:
    def status(self) -> list[Any]:
        return [
            _Status("twitch", "running"),
            _Status("obs", "failed", REFUSED),
        ]


class _CoreWithPlugins:
    plugins = _Plugins()


class _CoreWithoutPlugins:
    plugins = None


def _registry(core: object) -> RequestRegistry:
    registry = RequestRegistry()
    handlers = CoreHandlers(cast("Any", core))
    registry.register("plugin.status", EmptyPayload, handlers.plugin_status, roles=UI_ROLES)
    return registry


def _request(role: str) -> Envelope:
    return Envelope(
        kind=Kind.REQUEST,
        name="plugin.status",
        src=Source(role=role, id=f"{role}:1"),
        payload={},
    )


async def test_reports_every_plugin_with_its_state_and_reason() -> None:
    registry = _registry(_CoreWithPlugins())
    env = _request("dashboard")
    reply = await registry.dispatch(
        RequestContext(client_id=env.src.id, role=env.src.role, request=env), env
    )
    assert reply.kind is Kind.RESPONSE, reply.payload
    # The response validates against the contract the TypeScript types are generated from.
    payload = PluginStatusList.model_validate(reply.payload)
    assert [p.id for p in payload.plugins] == ["twitch", "obs"]
    assert payload.plugins[0].state == "running" and payload.plugins[0].reason == ""
    assert payload.plugins[1].state == "failed"
    assert "does not allow" in payload.plugins[1].reason  # the UI shows this verbatim


async def test_answers_with_an_empty_list_before_the_manager_exists() -> None:
    """Asked during boot the answer is "none yet", never a crash and never a fake entry."""
    registry = _registry(_CoreWithoutPlugins())
    env = _request("shell")
    reply = await registry.dispatch(
        RequestContext(client_id=env.src.id, role=env.src.role, request=env), env
    )
    assert reply.kind is Kind.RESPONSE and reply.payload == {"plugins": []}


async def test_is_not_reachable_from_a_plugin_or_the_pet() -> None:
    registry = _registry(_CoreWithPlugins())
    for role in ("plugin", "pet", "worker"):
        env = _request(role)
        reply = await registry.dispatch(
            RequestContext(client_id=env.src.id, role=env.src.role, request=env), env
        )
        assert reply.kind is Kind.ERROR and reply.payload["code"] == ERR_PERMISSION, role
