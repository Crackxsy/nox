"""Who may do what while Nox's own hands are on the machine: the whole table, cell by cell.

Two surfaces, one rule. A *request* (`RequestRegistry.dispatch`) and an *answer to a permission
prompt* (`CoreHandlers.permission_reply`) are asked from six kinds of client in nine states of
the world. Every cell is a literal, so a rule that changes shows up as one red cell and a diff in
this file - not as a refactor that "kept the tests green".

The words of a cell: A = allowed, L = refused by the local input lock (synthetic input could be
clicking), K = refused as the PC Nox controls (linked browser, or this machine), R = refused as a
remote session, N = refused because no PIN is set, S = refused because the node's address binding
is full, O = refused as a role that never approves, P = the PIN is missing or wrong, D = a
"deny" (always counts), X = a paired device's high-risk "allow" without a valid PIN. The client
"at_the_pc" is a paired device that is not linked in the dashboard but connects from the address
of the PC Nox controls: it counts as that PC's browser.
"""

from __future__ import annotations

import pytest

from nox.ipc.errors import IpcError
from nox.security.model import Decision, Risk
from tests.unit.node.support_sessions import PinGate
from tests.unit.security.access_world import (
    CLIENTS,
    PIN,
    World,
    build_world,
    outcome,
    reply_outcome,
)

#: The requests of the columns, in the order of the cells.
REQUESTS: list[tuple[str, dict[str, object]]] = [
    ("state.get", {}),  # looking
    ("security.kill", {}),  # stopping
    ("plans.list", {}),  # a `.list`
    ("voice.mute", {"muted": True}),  # muting
    ("voice.mute", {"muted": False}),  # unmuting: could give Nox an ear again
    ("chat.send", {}),  # talking to Nox
    ("voice.ptt", {}),  # talking to Nox
    ("plans.approve", {}),  # a change, no PIN given
    ("plans.approve", {"pin": PIN}),  # a change, the right PIN
    ("mcp.servers.add", {"pin": "0000"}),  # a change, a wrong PIN
    ("node.unpair", {}),  # a way out of a node lock, no PIN
    ("node.unpair", {"pin": PIN}),  # a way out, the right PIN
    ("security.resume", {"pin": PIN}),  # a way out, the right PIN
]
WORDS = {
    "allow": "A",
    "lock": "L",
    "node_session_linked": "K",
    "node_session_remote": "R",
    "node_session_no_pin": "N",
    "node_session_saturated": "S",
    "node_session_role": "O",
    "node_session_pin": "P",
}

#: state -> (local input lock, node session, remote console, a PIN is set)
STATES: dict[str, tuple[bool, str, bool | None, bool]] = {
    "idle": (False, "off", False, True),
    "local lock": (True, "off", False, True),
    "node session": (False, "remote_pc", False, True),
    "both locks": (True, "remote_pc", False, True),
    "node on this PC": (False, "this_pc", False, True),
    "binding full": (False, "saturated", False, True),
    "remote console": (False, "remote_pc", True, True),
    "remote unknown": (False, "remote_pc", None, True),
    "no PIN set": (False, "remote_pc", False, False),
}

#: state -> client -> one word per request (`REQUESTS`).
REQUEST_TABLE: dict[str, dict[str, str]] = {
    "idle": {
        "dashboard": "AAAAAAAAAAAAA",
        "shell": "AAAAAAAAAAAAA",
        "phone": "AAAAAAAAAAAAA",
        "linked": "AAAAAAAAAAAAA",
        "at_the_pc": "AAAAAAAAAAAAA",
        "plugin": "AAAAAAAAAAAAA",
    },
    "local lock": {
        "dashboard": "AAAALLLLLLLLL",
        "shell": "AAAALLLLLLLLL",
        "phone": "AAAALLLLLLLLL",
        "linked": "AAAALLLLLLLLL",
        "at_the_pc": "AAAALLLLLLLLL",
        "plugin": "AAAAAAAAAAAAA",
    },
    "node session": {
        "dashboard": "AAAAPAAPAPAAA",
        "shell": "AAAAPAAPAPAAA",
        "phone": "AAAAPAAPAPPAA",
        "linked": "AAAAKKKKKKPAA",
        "at_the_pc": "AAAAKKKKKKPAA",
        "plugin": "AAAAAAAAAAAAA",
    },
    "both locks": {
        "dashboard": "AAAALLLLLLLLL",
        "shell": "AAAALLLLLLLLL",
        "phone": "AAAALLLLLLLLL",
        "linked": "AAAALLLLLLLLL",
        "at_the_pc": "AAAALLLLLLLLL",
        "plugin": "AAAAAAAAAAAAA",
    },
    "node on this PC": {
        "dashboard": "AAAAKAAKKKAAA",
        "shell": "AAAAKAAKKKAAA",
        "phone": "AAAAPAAPAPPAA",
        "linked": "AAAAKKKKKKPAA",
        "at_the_pc": "AAAAPAAPAPPAA",
        "plugin": "AAAAAAAAAAAAA",
    },
    "binding full": {
        "dashboard": "AAAAPSSPAPAAA",
        "shell": "AAAAPSSPAPAAA",
        "phone": "AAAAPSSPAPPAA",
        "linked": "AAAAKSSKKKPAA",
        "at_the_pc": "AAAAKSSKKKPAA",
        "plugin": "AAAAAAAAAAAAA",
    },
    "remote console": {
        "dashboard": "AAAARRRRRRAAA",
        "shell": "AAAARRRRRRAAA",
        "phone": "AAAAPAAPAPPAA",
        "linked": "AAAAKKKKKKPAA",
        "at_the_pc": "AAAAKKKKKKPAA",
        "plugin": "AAAAAAAAAAAAA",
    },
    "remote unknown": {
        "dashboard": "AAAARRRRRRAAA",
        "shell": "AAAARRRRRRAAA",
        "phone": "AAAAPAAPAPPAA",
        "linked": "AAAAKKKKKKPAA",
        "at_the_pc": "AAAAKKKKKKPAA",
        "plugin": "AAAAAAAAAAAAA",
    },
    "no PIN set": {
        "dashboard": "AAAANAANNNAAN",
        "shell": "AAAANAANNNAAN",
        "phone": "AAAANAANNNNNN",
        "linked": "AAAAKKKKKKNNN",
        "at_the_pc": "AAAAKKKKKKNNN",
        "plugin": "AAAAAAAAAAAAA",
    },
}

#: state -> client -> the answers to a permission prompt: allow with the PIN, allow without it,
#: deny, allow with the PIN at high risk, allow without it at high risk, the same two at
#: critical risk.
REPLY_TABLE: dict[str, dict[str, str]] = {
    "idle": {
        "dashboard": "AADAAAA",
        "shell": "AADAAAA",
        "phone": "AADAXAX",
        "linked": "AADAXAX",
        "at_the_pc": "AADAXAX",
        "plugin": "AADAAAA",
    },
    "local lock": {
        "dashboard": "LLDLLLL",
        "shell": "LLDLLLL",
        "phone": "LLDLLLL",
        "linked": "LLDLLLL",
        "at_the_pc": "LLDLLLL",
        "plugin": "LLDLLLL",
    },
    "node session": {
        "dashboard": "APDAPAP",
        "shell": "APDAPAP",
        "phone": "APDAPAP",
        "linked": "KKDKKKK",
        "at_the_pc": "KKDKKKK",
        "plugin": "OODOOOO",
    },
    "both locks": {
        "dashboard": "LLDLLLL",
        "shell": "LLDLLLL",
        "phone": "LLDLLLL",
        "linked": "LLDLLLL",
        "at_the_pc": "LLDLLLL",
        "plugin": "LLDLLLL",
    },
    "node on this PC": {
        "dashboard": "KKDKKKK",
        "shell": "KKDKKKK",
        "phone": "APDAPAP",
        "linked": "KKDKKKK",
        "at_the_pc": "APDAPAP",
        "plugin": "OODOOOO",
    },
    "binding full": {
        "dashboard": "SSDSSSS",
        "shell": "SSDSSSS",
        "phone": "SSDSSSS",
        "linked": "SSDSSSS",
        "at_the_pc": "SSDSSSS",
        "plugin": "SSDSSSS",
    },
    "remote console": {
        "dashboard": "RRDRRRR",
        "shell": "RRDRRRR",
        "phone": "APDAPAP",
        "linked": "KKDKKKK",
        "at_the_pc": "KKDKKKK",
        "plugin": "OODOOOO",
    },
    "remote unknown": {
        "dashboard": "RRDRRRR",
        "shell": "RRDRRRR",
        "phone": "APDAPAP",
        "linked": "KKDKKKK",
        "at_the_pc": "KKDKKKK",
        "plugin": "OODOOOO",
    },
    "no PIN set": {
        "dashboard": "NNDNNNN",
        "shell": "NNDNNNN",
        "phone": "NNDNNNN",
        "linked": "KKDKKKK",
        "at_the_pc": "KKDKKKK",
        "plugin": "OODOOOO",
    },
}

#: What a prompt is answered with, per column of a `REPLY_TABLE` row.
ANSWERS: list[tuple[Decision, str | None, Risk | None]] = [
    (Decision.ALLOW, PIN, None),
    (Decision.ALLOW, None, None),
    (Decision.DENY, None, None),
    (Decision.ALLOW, PIN, Risk.HIGH),
    (Decision.ALLOW, None, Risk.HIGH),
    (Decision.ALLOW, PIN, Risk.CRITICAL),
    (Decision.ALLOW, None, Risk.CRITICAL),
]


def cases() -> list[tuple[str, str]]:
    return [(state, client) for state in STATES for client in CLIENTS]


def put_into(world: World, state: str) -> None:
    lock, node, remote, _pin = STATES[state]
    world.local_lock(lock)
    world.node(node)
    world.remote[0] = remote


async def request_words(world: World, client: str) -> str:
    words = ""
    for name, payload in REQUESTS:
        words += WORDS[outcome(await world.send(client, name, dict(payload)))]
    return words


async def answer_words(world: World, client: str) -> str:
    words = ""
    for decision, pin, risk in ANSWERS:
        world.risk[0] = risk
        try:
            answer = await world.reply(client, decision, pin)
        except IpcError:  # a paired device's high-risk "allow": its own PIN door refuses
            words += "X"
            continue
        word = reply_outcome(answer, world.replied)
        words += "D" if word == "deny" else WORDS[word]
    return words


@pytest.mark.parametrize(("state", "client"), cases())
async def test_a_request_meets_the_rule_of_its_state(state: str, client: str) -> None:
    world = build_world(pin=PinGate(required=STATES[state][3]))
    put_into(world, state)
    assert await request_words(world, client) == REQUEST_TABLE[state][client], f"{state}/{client}"


@pytest.mark.parametrize(("state", "client"), cases())
async def test_an_answer_to_a_prompt_meets_the_rule_of_its_state(state: str, client: str) -> None:
    world = build_world(pin=PinGate(required=STATES[state][3]))
    put_into(world, state)
    assert await answer_words(world, client) == REPLY_TABLE[state][client], f"{state}/{client}"
