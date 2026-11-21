"""The addresses of this device the node tells the core, without the ones of virtual adapters."""

from __future__ import annotations

#: Interface names of virtual adapters (Hyper-V, WSL, Docker, VirtualBox, VMware, bridges):
#: their addresses are listed last, after the real ones, before the cap of 16.
_VIRTUAL_ADAPTERS = ("vethernet", "wsl", "docker", "veth", "br-", "virbr", "hyper-v", "virtualbox",
                     "vmware", "vboxnet", "vmnet", "zt", "tailscale")  # fmt: skip


def local_addresses() -> list[str]:
    """This device's own IP addresses (no loopback), at most 16 - for the core to recognise a
    browser on this PC (`NodeHello.addresses`, every `nox/pong`). Private addresses of real,
    active interfaces first; virtual adapters and interfaces that are down last."""
    import ipaddress  # noqa: PLC0415

    import psutil  # noqa: PLC0415

    try:
        interfaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError):
        return []
    ranked: list[tuple[int, str]] = []
    for name, entries in interfaces.items():
        up = bool(getattr(stats.get(name), "isup", False))
        virtual = name.lower().startswith(_VIRTUAL_ADAPTERS) or any(
            word in name.lower() for word in ("virtual", "hyper-v", "wsl")
        )
        for entry in entries:
            try:
                ip = ipaddress.ip_address(str(entry.address).split("%", 1)[0])
            except ValueError:
                continue
            if ip.is_loopback:
                continue
            rank = (0 if up else 2) + (1 if virtual else 0) + (0 if ip.is_private else 4)
            ranked.append((rank, str(ip)))
    found: list[str] = []
    for _rank, address in sorted(ranked, key=lambda item: item[0]):
        if address not in found:
            found.append(address)
    return found[:16]


#: How long the addresses are reused between pongs (they rarely change; a rotating IPv6
#: temporary address is picked up within this).
ADDRESSES_TTL_S = 30.0
