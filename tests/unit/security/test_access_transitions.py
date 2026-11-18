"""The two locks as a state machine: one ends while the other stays, and neither leaks into the
other's decision. The words are those of `test_access_table`."""

from __future__ import annotations

import asyncio

from nox.ipc.protocol import Kind
from tests.unit.node.support_sessions import PinGate
from tests.unit.security.access_world import PIN, World, build_world, outcome
from tests.unit.security.test_access_table import (
    REQUEST_TABLE,
    WORDS,
    request_words,
)


async def settled(world: World, client: str) -> str:
    return await request_words(world, client)


async def test_a_lock_ending_leaves_the_other_one_in_force() -> None:
    world = build_world()
    # Walk through every combination and back; after each step the row of that state holds.
    walk = [
        (False, "off", "idle"),
        (True, "off", "local lock"),
        (True, "remote_pc", "both locks"),
        (False, "remote_pc", "node session"),  # the local lock ended, the node's stays
        (True, "remote_pc", "both locks"),
        (True, "off", "local lock"),  # the node's ended, the local one stays
        (False, "off", "idle"),
        (False, "remote_pc", "node session"),
        (False, "off", "idle"),
    ]
    for lock, node, state in walk:
        world.local_lock(lock)
        world.node(node)
        for client in ("dashboard", "phone", "linked"):
            assert await settled(world, client) == REQUEST_TABLE[state][client], (state, client)


async def test_either_lock_alone_refuses_unmuting_and_neither_refuses_muting() -> None:
    """Unmuting could give Nox an ear again: it is not calm - under the local input lock and
    in a node session alike. Muting always is."""
    world = build_world()
    for lock, node in ((True, "off"), (False, "remote_pc"), (True, "remote_pc")):
        world.local_lock(lock)
        world.node(node)
        assert (await world.send("dashboard", "voice.mute", {"muted": True})).kind is Kind.RESPONSE
        unmute = await world.send("dashboard", "voice.mute", {"muted": False})
        assert unmute.kind is Kind.ERROR, (lock, node)
        for odd in ("false", 0, None):  # not a boolean `true`: not calm either
            refused = await world.send("dashboard", "voice.mute", {"muted": odd})
            assert refused.kind is Kind.ERROR, (lock, node, odd)
    world.local_lock(False)
    world.node("off")
    assert (await world.send("dashboard", "voice.mute", {"muted": False})).kind is Kind.RESPONSE


async def test_with_both_locks_the_local_one_speaks_first() -> None:
    world = build_world()
    world.local_lock(True)
    world.node("remote_pc")
    for client in ("dashboard", "phone", "linked"):
        reply = await world.send(client, "plans.approve", {"pin": PIN})
        assert outcome(reply) == "lock", client  # not the node's words, and no PIN is asked


async def test_requests_asked_at_once_do_not_share_an_answer() -> None:
    """A PIN given by one client never counts for another, whatever order the checks finish."""

    class SlowGate(PinGate):
        async def require(self, pin: str | None, *, action: str, by: str) -> None:
            await asyncio.sleep(0)  # yield between two clients' checks
            await super().require(pin, action=action, by=by)

    world = build_world(pin=SlowGate())
    world.node("remote_pc")
    asks = [
        ("phone", {}),
        ("dashboard", {"pin": PIN}),
        ("linked", {"pin": PIN}),
        ("shell", {"pin": "0000"}),
        ("phone", {"pin": PIN}),
        ("dashboard", {}),
    ]
    for order in (asks, list(reversed(asks))):
        replies = await asyncio.gather(*(world.send(c, "plans.approve", p) for c, p in order))
        words = [WORDS[outcome(r)] for r in replies]
        assert words == [expected(c, p) for c, p in order]


def expected(client: str, payload: dict[str, str]) -> str:
    if client == "linked":
        return "K"  # never, not even with the PIN
    return "A" if payload.get("pin") == PIN else "P"


async def test_a_request_asked_while_the_lock_flips_is_decided_whole() -> None:
    """The input lock is read when the request is checked: each of many requests is either
    refused with the lock's words or runs, never a mixture."""
    world = build_world()
    world.node("remote_pc")

    async def flip() -> None:
        for on in (True, False, True, False, True):
            world.local_lock(on)
            await asyncio.sleep(0)

    async def ask() -> str:
        return WORDS[outcome(await world.send("dashboard", "plans.approve", {"pin": PIN}))]

    results = await asyncio.gather(flip(), *(ask() for _ in range(12)))
    assert set(results[1:]) <= {"A", "L"}


async def test_the_pin_is_stripped_only_where_the_node_rule_read_it() -> None:
    """The PIN was for the check; a request that takes none does not see it. A calm request, or
    a role no lock applies to, passes the payload on as it came."""
    world = build_world()
    ok = await world.send("dashboard", "strict.change", {"pin": PIN})
    assert ok.kind is Kind.RESPONSE  # stripped, though no session runs
    calm = await world.send("dashboard", "strict.list", {"pin": PIN})
    assert calm.payload["code"] == "validation.failed"  # calm: the payload is untouched
    other = await world.send("plugin", "strict.change", {"pin": PIN})
    assert other.payload["code"] == "validation.failed"  # no lock applies to this role
