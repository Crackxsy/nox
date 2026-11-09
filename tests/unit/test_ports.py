"""`tests._ports.free_port_base`: a pair the OS will not hand to someone else meanwhile (C10).

A base taken from `bind(0)` sits in the OS's ephemeral range, and Windows allocates that range in
sequence - `base + 1` was the next port any `bind(0)` or outgoing connection got while the core
under test was starting, which left a live-core dashboard test without a connection now and then.
"""

from __future__ import annotations

import socket

from tests._ports import PORT_RANGE, free_port_base


def test_the_pair_comes_from_below_every_ephemeral_range_and_is_free() -> None:
    for _ in range(20):
        base = free_port_base()
        assert PORT_RANGE.start <= base and base + 1 < PORT_RANGE.stop
        assert base + 1 < 32768  # Linux's ephemeral range starts there; Windows' at 49152
        for port in (base, base + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))


def test_ports_the_os_hands_out_are_never_chosen() -> None:
    handed_out = []
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            handed_out.append(probe.getsockname()[1])
    assert all(port not in PORT_RANGE for port in handed_out)
