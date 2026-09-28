"""`is_loopback` decides exactly: IP literals by `ipaddress`, names only when they are `localhost`.

A prefix test once let `127.evil.example` count as this machine, and with it past every egress
allow-list (a loopback host is allowed before any list is consulted).
"""

from __future__ import annotations

import pytest

from nox.core.netloc import is_loopback, normalize_loopback_host


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.5.5.5",
        "127.255.255.254",
        "localhost",
        "LOCALHOST",
        "localhost.",
        "::1",
        "[::1]",
        "0:0:0:0:0:0:0:1",
        "::ffff:127.0.0.1",
        "[::ffff:127.0.0.1]",
        " 127.0.0.1 ",
    ],
)
def test_loopback_addresses_and_the_one_loopback_name_are_loopback(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize(
    "host",
    [
        "127.evil.example",
        "127.0.0.1.nip.io",
        "127.0.0.1.evil.example",
        "localhost.evil",
        "localhost.evil.example",
        "evil-localhost",
        "my.localhost.example",
        "10.0.0.1",
        "192.168.1.10",
        "::ffff:10.0.0.1",
        "::2",
        "0.0.0.0",
        "::",
        "127.*",
        "",
        "[127.0.0.1",
    ],
)
def test_names_that_only_look_like_loopback_are_not_loopback(host: str) -> None:
    assert not is_loopback(host)


def test_only_spellings_of_127_0_0_1_itself_fold_onto_it() -> None:
    assert normalize_loopback_host("localhost") == "127.0.0.1"
    assert normalize_loopback_host("[::1]") == "127.0.0.1"
    assert normalize_loopback_host("::ffff:127.0.0.1") == "127.0.0.1"
    assert normalize_loopback_host("127.0.0.2") == "127.0.0.2"
    assert normalize_loopback_host("127.evil.example") == "127.evil.example"
