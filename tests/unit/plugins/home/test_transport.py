"""How the access token travels, and how much Home Assistant may send in one frame.

The long-lived token has administrator rights over the whole house, so it must not cross the
network unencrypted unless the user said so by hand. And a real install's `get_states` is one
frame of several MiB, which the `websockets` default of 1 MiB turned into a disconnect on every
inventory refresh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.security.egress import EgressDenied

from .conftest import FakeClient, make_api, write_home_settings
from .fake_ha_server import FakeHomeAssistant, default_states, wait_until

pytestmark = pytest.mark.timeout(30)

LAN_HOST = "192.168.1.20"


def _plugin(client: FakeClient, user_config: Path, **settings: Any) -> Any:
    from nox_plugin_home import create

    write_home_settings(user_config, host=LAN_HOST, port=8123, **settings)
    return create(make_api(client, port=8123))


def test_the_token_is_not_sent_unencrypted_to_another_machine(
    fake_client: FakeClient, home_config: Path
) -> None:
    from nox_plugin_home.plugin import InsecureTransportError

    plugin = _plugin(fake_client, home_config)

    with pytest.raises(InsecureTransportError, match="unencrypted"):
        plugin._authorize(plugin.client.url)


def test_tls_or_an_explicit_opt_in_passes_on_to_the_egress_guard(
    fake_client: FakeClient, home_config: Path
) -> None:
    """Past the transport check the manifest-scoped guard still decides (and denies here)."""
    with_tls = _plugin(fake_client, home_config, tls=True)
    assert with_tls.client.url.startswith("wss://")
    with pytest.raises(EgressDenied):
        with_tls._authorize(with_tls.client.url)

    opted_in = _plugin(fake_client, home_config, allow_insecure=True)
    with pytest.raises(EgressDenied):
        opted_in._authorize(opted_in.client.url)


def test_a_loopback_instance_needs_no_tls(fake_client: FakeClient, home_config: Path) -> None:
    from nox_plugin_home import create

    write_home_settings(home_config, host="127.0.0.1", port=8123)
    plugin = create(make_api(fake_client, port=8123))
    plugin._authorize(plugin.client.url)  # no exception: loopback, declared, BALANCED


async def test_an_inventory_larger_than_one_mebibyte_arrives_in_one_piece(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    from nox_plugin_home import create

    padding = "x" * 1500
    many = [
        {
            "entity_id": f"sensor.probe_{index}",
            "state": "1",
            "attributes": {"friendly_name": f"Probe {index}", "note": padding},
        }
        for index in range(1200)
    ]
    ha_server.set_response("get_states", lambda _d: [*default_states(), *many])
    write_home_settings(
        home_config, host="127.0.0.1", port=ha_server.port, min_backoff_s=0.05, max_backoff_s=0.2
    )
    api = make_api(fake_client, port=ha_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.authenticated)
        result = await api.tools.call("home.list", {"domain": "sensor"})
        assert result["ok"] is True, result
        assert len(result["entities"]) == 1201
        assert ha_server.auth_count == 1  # the big frame did not cost a reconnect
    finally:
        await plugin.stop()
