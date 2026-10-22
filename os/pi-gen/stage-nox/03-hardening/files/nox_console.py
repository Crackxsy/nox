"""Nox-OS console status: what the Pi shows on a screen plugged into it (tty1, os/README.md).

No login, no secrets: the Nox-OS and Nox versions, this device's addresses, the dashboard's
address, whether network access and pairing are on, how many devices are paired, Nox's health and
whether remote maintenance (SSH) is on. Refreshed every ten seconds. It runs as a throwaway user
with no rights (`DynamicUser=`) and reads only public facts: /etc/nox-os-release, `ip`, Nox's
unauthenticated `/health` on loopback and the firewall's and SSH switch's state files.

    python3 -I -S nox_console.py           loop on the terminal systemd gives it
    python3 -I -S nox_console.py --once    print the screen once (tests, `ssh`)

Standard library only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

OS_RELEASE = Path("/etc/nox-os-release")
FIREWALL = Path("/run/nox-os/firewall.json")
SSH = Path("/run/nox-os/ssh.json")
HEALTH_URL = "http://127.0.0.1:47801/health"
REFRESH_S = 10
#: Clear the screen, cursor home (the kernel prints to tty1 too).
CLEAR = "\033[2J\033[H"


def os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isidentifier():
            values[key] = value.strip().strip("'\"")
    return values


def addresses(ip_json: str) -> list[str]:
    """Global and link-local unicast addresses of the interfaces that are up (`ip -j addr`),
    loopback and container bridges left out."""
    try:
        links = json.loads(ip_json)
    except ValueError:
        return []
    found = []
    for link in links if isinstance(links, list) else []:
        name = str(link.get("ifname", ""))
        if name == "lo" or "UP" not in link.get("flags", []):
            continue
        for info in link.get("addr_info", []):
            local = info.get("local")
            if not isinstance(local, str) or info.get("scope") not in ("global", "link"):
                continue
            if info.get("family") == "inet6" and info.get("scope") == "link":
                continue  # fe80:: is per link, of no use to type into a phone
            found.append(local)
    return found


def _url(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"https://{host}:{port}/dashboard/"


def render(
    release: Mapping[str, str],
    addrs: Sequence[str],
    health: Mapping[str, Any] | None,
    firewall: Mapping[str, Any] | None,
    ssh: Mapping[str, Any] | None,
    now: float,
) -> str:
    """The screen, as plain text (80 columns, no colours: works on every screen)."""
    lines = [
        "Nox-OS " + release.get("NOX_OS_VERSION", "(unknown version)"),
        release.get("NOX_OS_NAME", "Nox-OS (based on Raspberry Pi OS)"),
        "",
    ]
    if health is None:
        lines.append("Nox:            not answering yet (starting, or stopped)")
    else:
        level = str(health.get("level", "unknown"))
        components = health.get("components") or {}
        down = sorted(
            name
            for name, comp in components.items()
            if isinstance(comp, dict) and comp.get("status") not in ("available", "disabled")
        )
        lines.append(f"Nox:            {health.get('version', '?')}, {level}")
        lines.append("Health:         " + ("all parts available" if not down else "limited: " + ", ".join(down[:6])))
        lan = components.get("lan") if isinstance(components, dict) else None
        if isinstance(lan, dict) and lan.get("status") == "available":
            lines.append(f"Pairing:        {lan.get('reason', '')}")
    lines.append("")
    lines.append("Addresses:      " + (", ".join(addrs) if addrs else "no network yet"))
    port = (firewall or {}).get("dashboard_port")
    if isinstance(port, int) and addrs:
        lines.append("Dashboard:      " + _url(addrs[0], port))
        lines.append("                https://nox.local:%d/dashboard/" % port)
    elif firewall is not None and not firewall.get("lan_enabled"):
        lines.append("Dashboard:      network access is off")
    if firewall is not None and firewall.get("setup"):
        lines.append("Setup mode:     on")
    until = (ssh or {}).get("until")
    if (ssh or {}).get("on") is True and isinstance(until, (int, float)) and until > now:
        lines.append(
            "Remote access:  SSH ON until "
            + time.strftime("%H:%M", time.localtime(until))
            + " (key only, home network only)"
        )
    else:
        lines.append("Remote access:  SSH off")
    lines += [
        "",
        "Open the dashboard on a phone or computer in the same network.",
        "This screen shows no secrets and needs no login.",
        "Updated " + time.strftime("%H:%M:%S", time.localtime(now)),
    ]
    return "\n".join(lines) + "\n"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _health() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:  # noqa: S310 - fixed loopback URL
            data = json.loads(response.read(64 * 1024))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def screen(now: float) -> str:
    try:
        release = os_release(OS_RELEASE.read_text("utf-8"))
    except OSError:
        release = {}
    try:
        shown = subprocess.run(  # noqa: S603 - fixed program
            [shutil.which("ip") or "/usr/bin/ip", "-j", "addr"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        shown = "[]"
    return render(release, addresses(shown), _health(), _read_json(FIREWALL), _read_json(SSH), now)


def main(argv: list[str]) -> int:
    if argv[1:] == ["--once"]:
        sys.stdout.write(screen(time.time()))
        return 0
    while True:
        sys.stdout.write(CLEAR + screen(time.time()))
        sys.stdout.flush()
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
