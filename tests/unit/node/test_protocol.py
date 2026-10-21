"""The node protocol: the role `node` allow-list, strict models and the pairing link."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from nox.node import protocol as p
from nox.node.protocol import NodeHello, NoxCallMeta, PairLink, PairLinkError

FP = "ab" * 32
FP_COLONS = ":".join(["AB"] * 32)


def test_a_node_may_send_exactly_these_methods() -> None:
    assert p.NODE_TO_CORE == {
        "initialize",
        "notifications/initialized",
        "nox/killed",
        "nox/ping",
        "nox/pong",
        "nox/confirm_result",
        "security.kill",
        "notifications/tools/list_changed",
        "nox/blob",
    }


@pytest.mark.parametrize(
    "method", ["tools/call", "tools/list", "nox/kill", "nox/resume", "nox/policy", "nox/revoked"]
)
def test_what_the_core_sends_a_node_cannot(method: str) -> None:
    assert method in p.CORE_TO_NODE
    assert method not in p.NODE_TO_CORE


def test_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NoxCallMeta(call_id="c1", risk="low", deadline=1.0, role="dashboard")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        NoxCallMeta(call_id="c1", risk="extreme", deadline=1.0)  # type: ignore[arg-type]


def test_a_hello_is_bounded() -> None:
    base = {
        "protocolVersion": "2025-06-18",
        "node_protocol": 1,
        "node_version": "0.7.0",
        "os": "linux",
        "arch": "aarch64",
        "manifest_hash": "0" * 64,
    }
    assert NodeHello.model_validate(base).arch == "aarch64"
    for key, value in [
        ("os", "templeos"),
        ("arch", "x86 64; rm"),
        ("manifest_hash", "Z" * 64),
        ("node_version", "1" * 33),
    ]:
        with pytest.raises(ValidationError):
            NodeHello.model_validate({**base, key: value})


def test_a_link_round_trips() -> None:
    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=FP_COLONS)
    assert link.ca_fingerprint == FP
    assert PairLink.parse(link.to_url()) == link
    assert link.to_url().startswith("nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=")


@pytest.mark.parametrize(
    "host", ["wohnzimmer-pi.local", "nox.home.arpa", "localhost", "10.0.0.5", "fe80::1"]
)
def test_a_link_may_name_a_home_host(host: str) -> None:
    assert PairLink.make(host=host, port=47843, code="ABCD2345", ca_fingerprint=FP).host == host


@pytest.mark.parametrize(
    "host", ["8.8.8.8", "example.com", "nox.fritz.box", "evil.local/../x", "a b.local", ""]
)
def test_a_link_may_not_name_another_host(host: str) -> None:
    with pytest.raises(PairLinkError):
        PairLink.make(host=host, port=47843, code="ABCD2345", ca_fingerprint=FP)


@pytest.mark.parametrize(
    "url",
    [
        "https://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=" + FP,
        "nox-node://other?h=192.168.1.20&p=47843&c=ABCD2345&fp=" + FP,
        "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345",
        "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=" + FP + "&x=1",
        "nox-node://pair?h=192.168.1.20&h=10.0.0.1&p=47843&c=ABCD2345&fp=" + FP,
        "nox-node://pair?h=192.168.1.20&p=0&c=ABCD2345&fp=" + FP,
        "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD0123&fp=" + FP,  # 0 and 1 are not codes
        "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=" + FP[:-2],
        "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=" + FP + "#frag",
    ],
)
def test_malformed_links_are_refused(url: str) -> None:
    with pytest.raises(PairLinkError):
        PairLink.parse(url)


@given(st.text(max_size=300))
def test_parsing_anything_either_succeeds_or_refuses_cleanly(text: str) -> None:
    try:
        link = PairLink.parse(text)
    except PairLinkError:
        return
    assert PairLink.parse(link.to_url()) == link


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("tools/call", "tools/call"),
        ("chat.send", "chat.send"),
        ("x\x1b[2J\x1b[31mDENY", r"x\x1b[2J\x1b[31mDENY"),
        ("ok\r\n     1  allow  core   system.info", r"ok\x0d\x0a     1  allow  core   system.info"),
        ("safe\u202etxt.exe", r"safe\u202etxt.exe"),
        ("c1\x9bcontrol", r"c1\x9bcontrol"),
        ("del\x7f", r"del\x7f"),
    ],
)
def test_names_from_the_other_side_are_recorded_escaped(raw: str, shown: str) -> None:
    assert p.safe_name(raw) == shown
    escaped = p.escape_controls(raw)
    assert not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c == "\u202e" for c in escaped)


def test_a_non_string_name_is_its_escaped_repr() -> None:
    assert p.safe_name(["a\nb"]) == r"['a\nb']"
