"""Free loopback port pairs for tests.

`random.randint(20000, 60000)` used to pick the base port; on Windows CI runners that regularly
landed inside a Hyper-V *excluded* port range (`netsh int ipv4 show excludedportrange`), where
binding fails with `[WinError 10013]` even though nothing listens there. Asking the OS for a port
(`bind(("127.0.0.1", 0))`) never returns an excluded one, and the pair (`base`, `base + 1`) is
verified by binding both before it is handed out.
"""

from __future__ import annotations

import socket


def free_port_base(attempts: int = 50) -> int:
    """A port `base` such that both `base` and `base + 1` were bindable a moment ago."""
    last: OSError | None = None
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            base = probe.getsockname()[1]
        if base + 1 > 65535:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                second.bind(("127.0.0.1", base + 1))
        except OSError as exc:
            last = exc
            continue
        return base
    raise RuntimeError(f"no free port pair after {attempts} attempts") from last
