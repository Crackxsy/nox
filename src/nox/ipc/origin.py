"""Host and Origin checks for the core's two loopback servers (HTTP and the WebSocket hub).

Binding to 127.0.0.1 keeps other machines out, but not a web page: a page on any site can point
its own DNS name at 127.0.0.1 (DNS rebinding) and then talk to the core as if it were that site,
or open a WebSocket straight to the hub from its own origin. Two checks close that:

* **Host** must name this machine - a loopback IP literal or exactly `localhost` - with the port
  the server is actually listening on. A rebinding page always sends its own name
  (`Host: attacker.example:47801`), so it is turned away before any handler runs.
* **Origin**, when a browser sends one, must be the core's own UI origin: `http://127.0.0.1:<http
  port>` or `http://localhost:<http port>` (plus the configured `ipc.host`, if it is another
  loopback address). A request without an Origin header comes from a non-browser client - the Qt
  shell, the workers, the plugins, `nox doctor` - and is allowed; the session token still decides
  what it may do. `null` is an opaque browser origin (a sandboxed frame, a `file:` page), never a
  Nox client, and is refused like any other foreign origin.

Neither check replaces the token: they only stop a browser from reaching the servers at all.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from nox.core.netloc import NetlocError, is_loopback, split_netloc

__all__ = [
    "LOOPBACK_UI_HOSTS",
    "host_header_allowed",
    "origin_allowed",
    "ui_origins",
]

#: The host names a browser can have loaded one of the core's own pages from.
LOOPBACK_UI_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")

#: The port an HTTP Host header means when it names none.
_DEFAULT_HTTP_PORT = 80


def host_header_allowed(host_header: str | None, port: int) -> bool:
    """Whether a `Host` header names this machine on `port`.

    The host part must be a loopback IP literal (`127.0.0.1`, `[::1]`) or exactly `localhost`;
    the port must be the listening port. A missing or unparsable header is refused.
    """
    if not host_header:
        return False
    try:
        host, port_text = split_netloc(host_header)
    except NetlocError:
        return False
    if not is_loopback(host):
        return False
    requested = _DEFAULT_HTTP_PORT if port_text == "*" else int(port_text)
    return requested == port


def ui_origins(http_port: int, extra_hosts: Iterable[str] = ()) -> frozenset[str]:
    """The browser origins of the core's own pages when the HTTP server listens on `http_port`."""
    hosts = {*LOOPBACK_UI_HOSTS, *(h.strip().lower() for h in extra_hosts if h.strip())}
    return frozenset(f"http://{_bracketed(host)}:{http_port}" for host in hosts)


def origin_allowed(origin: str | None, allowed: Collection[str]) -> bool:
    """Absent Origin: a non-browser client, allowed. Present: it must be one of `allowed`."""
    if origin is None:
        return True
    return origin.strip().lower().rstrip("/") in allowed


def _bracketed(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host
