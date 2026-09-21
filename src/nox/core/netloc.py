"""Parsing `host` / `host:port` / `scheme://host:port` allow-list entries, IPv6 included.

The egress guard and the configuration validator share this parser, so an entry the configuration
accepts is an entry the guard can match. Splitting on the last colon used to read the documented
loopback entry `::1` as host `::` and port `1`, which meant the entry silently matched nothing.
Brackets are therefore required for an IPv6 host that names a port (`[::1]:11434`) and optional
without one, exactly as in a URL.
"""

from __future__ import annotations

__all__ = [
    "LOOPBACK_HOSTS",
    "NetlocError",
    "is_loopback",
    "normalize_loopback_host",
    "split_netloc",
]

#: Spellings of "this machine" that all denote the same address.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})


class NetlocError(ValueError):
    """An allow-list entry that cannot be read as `host`, `host:port` or `scheme://host:port`."""


def split_netloc(entry: str) -> tuple[str, str]:
    """`entry` -> `(host, port)`, where `port` is `"*"` when the entry names none.

    The host keeps its glob characters (`*.example.com`) and is lowercased; an IPv6 host comes
    back without its brackets. Raises `NetlocError` for an empty entry, an unbracketed IPv6
    address followed by a port, or a port that is neither a number in 1..65535 nor `*`.
    """
    text = entry.strip().lower()
    if not text:
        raise NetlocError("empty allow-list entry")
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if not text:
        raise NetlocError(f"{entry!r} names no host")

    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not host:
            raise NetlocError(f"{entry!r}: unterminated '[' in an IPv6 address")
        if not rest:
            return host, "*"
        if not rest.startswith(":"):
            raise NetlocError(f"{entry!r}: expected ':<port>' after the IPv6 address")
        return host, _port(entry, rest[1:])

    if text.count(":") > 1:
        # A bare IPv6 address. With a port it would need brackets, so this names the host only.
        return text, "*"
    host, sep, port = text.partition(":")
    if not sep:
        return host, "*"
    if not host:
        raise NetlocError(f"{entry!r} names no host")
    return host, _port(entry, port)


def _port(entry: str, port: str) -> str:
    if port == "*":
        return port
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise NetlocError(f"{entry!r}: {port!r} is not a port number or '*'")
    return port


def is_loopback(host: str) -> bool:
    """Whether `host` addresses this machine: any `127.x` address, `localhost` or `::1`."""
    normalised = host.strip().lower().strip("[]")
    return normalised in LOOPBACK_HOSTS or normalised.startswith("127.")


def normalize_loopback_host(host: str) -> str:
    """Fold every spelling of this machine onto `127.0.0.1`; another host is only normalised."""
    normalised = host.strip().lower().strip("[]").rstrip(".")
    return "127.0.0.1" if normalised in LOOPBACK_HOSTS else normalised
