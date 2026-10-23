"""Free loopback port pairs for tests.

`random.randint(20000, 60000)` used to pick the base port; on Windows CI runners that regularly
landed inside a Hyper-V *excluded* port range (`netsh int ipv4 show excludedportrange`), where
binding fails with `[WinError 10013]` even though nothing listens there. The fix that followed
asked the OS for a port (`bind(("127.0.0.1", 0))`) and checked `base + 1` too - but a port the OS
hands out comes from its *ephemeral* range, which Windows allocates in sequence: `base + 1` was
then the very next port the OS gave to the next `bind(0)` or outgoing connection - the LAN
server's own port, a plugin worker's connection, the browser's sockets - while the core was still
starting. The core's HTTP server and its clients could then meet someone else on that port, and
a dashboard never got online (connection resets, C10).

So the pair is drawn from below every common ephemeral range (Linux starts at 32768, Windows and
macOS at 49152), where the OS never allocates on its own, and verified by binding both - which
also skips any excluded range.
"""

from __future__ import annotations

import random
import socket

#: Where a pair is drawn from: above the registered well-known services most runners use, below
#: every OS's ephemeral range.
PORT_RANGE = range(20000, 32000)


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port_base(attempts: int = 200) -> int:
    """A port `base` such that both `base` and `base + 1` were bindable a moment ago, outside
    the range the OS hands out by itself."""
    rng = random.SystemRandom()
    for _ in range(attempts):
        base = rng.randrange(PORT_RANGE.start, PORT_RANGE.stop - 1)
        if _bindable(base) and _bindable(base + 1):
            return base
    raise RuntimeError(f"no free port pair in {PORT_RANGE} after {attempts} attempts")
