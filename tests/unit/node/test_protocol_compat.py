"""Protocol compatibility: messages of the next protocol version with new fields are
accepted, a node of another protocol hears `compatible: false`, and unknown fields in a
hello do not prevent a connection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nox.node.protocol import NodeHello, Ping

# ---- protocol extensibility ----------------------------------------------------------------------


def test_messages_of_the_next_protocol_with_new_fields_are_accepted() -> None:
    hello = NodeHello.model_validate(
        {
            "protocolVersion": "2025-06-18",
            "node_protocol": 1,
            "node_version": "0.9.0",
            "os": "linux",
            "arch": "x86_64",
            "manifest_hash": "0" * 64,
            "features": ["voice", "something_new"],
            "a_field_of_protocol_2": {"x": 1},
        }
    )
    assert hello.features == ["voice", "something_new"]
    pong = Ping.model_validate(
        {"seq": 1, "ts": 1.0, "session": {"gen": 1, "active": False, "n": 2}, "z": 1}
    )  # noqa: E501
    assert pong.session is not None and pong.session.gen == 1
    with pytest.raises(ValueError):
        Ping.model_validate({"seq": "one", "ts": 1.0})  # known fields stay strict


async def test_a_node_of_another_protocol_hears_compatible_false(
    listener: Any, pairing: Any, tls: Any, tmp_path: Path
) -> None:
    from tests.unit.node.support import client_context, hello, open_node, pair_node, recv_until

    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    newer = hello(2)
    newer["params"]["os"] = "plan9"  # not something this release knows
    newer["params"]["capabilities_v2"] = ["x"]
    await ws.send(json.dumps(newer))
    answer = await recv_until(ws, lambda m: m.get("id") == 1)
    assert answer["result"]["compatible"] is False
    assert answer["result"]["node_protocol"] == [1, 1]
    assert answer["result"]["node_id"] == node.node_id


async def test_a_hello_of_this_protocol_with_unknown_fields_connects(
    listener: Any, pairing: Any, tls: Any, tmp_path: Path
) -> None:
    from tests.unit.node.support import client_context, hello, open_node, pair_node, recv_until

    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    message = hello(1)
    message["params"]["later_field"] = True
    await ws.send(json.dumps(message))
    answer = await recv_until(ws, lambda m: m.get("id") == 1)
    assert answer["result"]["compatible"] is True
    await ws.send(
        json.dumps(
            {"jsonrpc": "2.0", "method": "nox/pong", "params": {"seq": 1, "ts": 1.0, "new": 1}}
        )
    )  # noqa: E501
    await asyncio.sleep(0.1)
    assert listener.sessions[node.node_id].violations == 0
