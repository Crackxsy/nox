"""os/tools/check-image.sh on a fake image tree: a clean tree passes, and each thing that must
never ship - a password, a key, a token, a seed, SSH, Nox data, a boot seed - fails it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit.nox_os.conftest import FIRSTBOOT, HARDENING, REPO, TOOLS, load_tool

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None, reason="GNU tools, symlinks: Linux"
)

SCRIPT = TOOLS / "check-image.sh"
RELEASE = "0123456789ab"
# Fictitious-only key and token material: shapes, never real values.
FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
FAKE_PGP = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nAAAA\n-----END PGP PRIVATE KEY BLOCK-----\n"
FAKE_TOKEN = "ghp_" + "x" * 36


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def _link(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


def _clean_tree(base: Path) -> tuple[Path, Path]:
    root, boot = base / "root", base / "boot"
    release = root / "opt" / "nox" / "releases" / RELEASE
    _write(release / ".complete", "v0.7.0\n")
    _write(release / ".venv" / "bin" / "nox")
    _write(release / "ui" / "dashboard" / "dist" / "index.html", "ok")
    _link(root / "opt" / "nox" / "current", f"/opt/nox/releases/{RELEASE}")
    _write(
        root / "opt" / "nox" / "src" / ".git" / "config",
        '[remote "origin"]\n\turl = https://github.com/Crackxsy/nox\n'
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
    )
    _write(root / "usr" / "local" / "bin" / "nox")
    system = root / "etc" / "systemd" / "system"
    _write(system / "nox.service")
    _link(system / "multi-user.target.wants" / "nox.service", "/etc/systemd/system/nox.service")
    for unit in ("ssh.service", "ssh.socket", "sshswitch.service"):
        _link(system / unit, "/dev/null")
    _write(root / "etc" / "nox-os-release", "NOX_OS_VERSION=v0.7.0\n")
    _write(root / "etc" / "hostname", "nox\n")
    _write(root / "etc" / "machine-id", "uninitialized\n")
    _write(
        root / "etc" / "shadow", "root:*:20000::::::\nnox-admin:!:20000::::::\nnox:!:20000::::::\n"
    )
    _write(
        root / "etc" / "passwd",
        "root:x:0:0:root:/root:/bin/bash\nnox-admin:x:1000:1000::/home/nox-admin:/bin/bash\n",
    )
    _write(root / "etc" / "sudoers", "root ALL=(ALL:ALL) ALL\n%sudo ALL=(ALL:ALL) ALL\n")
    (root / "etc" / "sudoers.d").mkdir()
    (root / "etc" / "ssh").mkdir()
    (root / "etc" / "apt" / "apt.conf.d").mkdir(parents=True)
    (root / "root").mkdir()
    (root / "home" / "nox-admin").mkdir(parents=True)
    (root / "var" / "lib" / "systemd").mkdir(parents=True)
    _write(root / "var" / "lib" / "nox" / "user.yaml", "ipc:\n  lan:\n    enabled: true\n")
    _write(boot / "cmdline.txt", "console=serial0,115200 root=PARTUUID=0000-02 rootwait resize\n")
    _write(boot / "config.txt", "arm_64bit=1\n")
    _o2(root)
    _o3(root)
    return root, boot


def _o2(root: Path) -> None:
    """What O2's sub-stage (os/pi-gen/stage-nox/03-hardening/00-run.sh) leaves in the image, from
    the very files it installs."""
    system = root / "etc" / "systemd" / "system"
    shutil.copy(REPO / "installer" / "linux" / "nox.service", system / "nox.service")
    for target, unit in (
        ("sysinit.target", "nftables.service"),
        ("multi-user.target", "nox-firewall.service"),
        ("multi-user.target", "nox-firewall.path"),
        ("multi-user.target", "nox-console.service"),
        ("sockets.target", "nox-os-helper.socket"),
    ):
        _link(system / f"{target}.wants" / unit, f"/etc/systemd/system/{unit}")
    for unit in (
        "getty@tty1.service",
        "autovt@tty1.service",
        "bluetooth.service",
        "hciuart.service",
        "triggerhappy.service",
        "ModemManager.service",
    ):
        _link(system / unit, "/dev/null")
    for unit in (
        "nox-os-helper.socket",
        "nox-os-helper@.service",
        "nox-os-helper-recover.service",
        "nox-ssh.service",
    ):
        shutil.copy(HARDENING / unit, system / unit)
    (system / "nox.service.d").mkdir()
    shutil.copy(HARDENING / "nox-service-nox-os.conf", system / "nox.service.d" / "50-nox-os.conf")
    _write(
        root / "etc" / "group", "root:x:0:\nsudo:x:27:nox-admin\nnox:x:999:\nnox-os-helper:x:998:\n"
    )
    # The one file by that name a package ships: sshd's manual page.
    # Debian ships it as a link to sshd's own page.
    _write(root / "usr" / "share" / "man" / "man8" / "sshd.8.gz", "x")
    _link(root / "usr" / "share" / "man" / "man5" / "authorized_keys.5.gz", "../man8/sshd.8.gz")
    firewall = load_tool("nox_firewall", HARDENING)
    _write(root / "etc" / "nftables.conf", firewall.render(firewall.Settings(), boot=True))
    _write(
        root / "etc" / "nox-os" / "ssh" / "sshd_config",
        (HARDENING / "sshd_config").read_text("utf-8"),
    )
    _write(
        root / "etc" / "sudoers.d" / "50-nox-os", (HARDENING / "sudoers-nox-os").read_text("utf-8")
    )
    _write(
        root / "etc" / "systemd" / "journald.conf.d" / "50-nox-os.conf",
        (HARDENING / "journald-nox-os.conf").read_text("utf-8"),
    )
    _write(
        root / "etc" / "rpi" / "swap.conf.d" / "50-nox-os.conf",
        (HARDENING / "swap-nox-os.conf").read_text("utf-8"),
    )
    _write(
        root / "etc" / "sysctl.d" / "90-nox-os.conf",
        (HARDENING / "90-nox-os.conf").read_text("utf-8"),
    )
    _write(
        root / "etc" / "fstab",
        "proc /proc proc defaults 0 0\n"
        "PARTUUID=0000-01 /boot/firmware vfat defaults 0 2\n"
        "PARTUUID=0000-02 / ext4 defaults,noatime,commit=60 0 1\n"
        "tmpfs /tmp tmpfs defaults,noatime,nosuid,nodev,mode=1777,size=50% 0 0\n"
        "tmpfs /var/tmp tmpfs defaults,noatime,nosuid,nodev,mode=1777,size=25% 0 0\n",
    )


def _o3(root: Path) -> None:
    """What O3's sub-stage (os/pi-gen/stage-nox/04-firstboot/00-run.sh) leaves in the image."""
    system = root / "etc" / "systemd" / "system"
    for unit in (
        "nox-firstboot.service",
        "nox-setup.timer",
        "nox-setup.service",
        "nox-setup-close.service",
        "nox-setup-apply.path",
        "nox-setup-apply.service",
        "nox-setup-portal.service",
    ):
        shutil.copy(FIRSTBOOT / unit, system / unit)
    for target, unit in (
        ("sysinit.target", "nox-firstboot.service"),
        ("timers.target", "nox-setup.timer"),
        ("multi-user.target", "nox-setup-apply.path"),
    ):
        _link(system / f"{target}.wants" / unit, f"/etc/systemd/system/{unit}")
    _link(system / "systemd-firstboot.service", "/dev/null")
    _append(root / "etc" / "group", "nox-os-setup:x:997:\nnox-setup:x:996:\n")
    _append(root / "etc" / "passwd", "nox-setup:x:996:996::/nonexistent:/usr/sbin/nologin\n")
    _write(
        root / "etc" / "NetworkManager" / "dnsmasq-shared.d" / "50-nox-setup.conf",
        (FIRSTBOOT / "dnsmasq-nox-setup.conf").read_text("utf-8"),
    )


def _check(root: Path, boot: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--root", str(root), "--boot", str(boot)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "LC_ALL": "C"},
    )


def test_a_clean_tree_passes(tmp_path: Path) -> None:
    result = _check(*_clean_tree(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout


Spoil = Callable[[Path, Path], None]

SPOILERS: dict[str, Spoil] = {
    "a password hash": lambda r, b: _write(r / "etc/shadow", "root:$y$j9T$abc$def:20000::::::\n"),
    # Appended: the rest of the file (the setup portal's locked user too) stays as it ships.
    "a password in passwd": lambda r, b: _append(
        r / "etc/passwd", "admin2:abc:0:0::/root:/bin/sh\n"
    ),
    "NOPASSWD sudo": lambda r, b: _write(
        r / "etc/sudoers.d/010_pi-nopasswd", "nox-admin ALL=(ALL) NOPASSWD: ALL\n"
    ),
    "authorized_keys": lambda r, b: _write(
        r / "home/nox-admin/.ssh/authorized_keys", "ssh-ed25519"
    ),
    "an SSH host key": lambda r, b: _write(r / "etc/ssh/ssh_host_ed25519_key", "x"),
    "ssh.socket not masked": lambda r, b: (r / "etc/systemd/system/ssh.socket").unlink(),
    "sshswitch not masked": lambda r, b: (r / "etc/systemd/system/sshswitch.service").unlink(),
    "an OpenSSH private key": lambda r, b: _write(r / "etc/ssl/private/x.key", FAKE_PEM),
    "a PGP private key": lambda r, b: _write(r / "root/secret.asc", FAKE_PGP),
    "a GitHub token": lambda r, b: _write(r / "usr/share/doc/x/notes.txt", f"token {FAKE_TOKEN}\n"),
    "a token on the boot partition": lambda r, b: _write(b / "notes.txt", FAKE_TOKEN),
    "Nox data": lambda r, b: _write(r / "var/lib/nox/tls/ca.key", "x"),
    "no Nox data folder": lambda r, b: shutil.rmtree(r / "var/lib/nox"),
    "a machine id": lambda r, b: _write(r / "etc/machine-id", "0123456789abcdef0123456789abcdef\n"),
    "a D-Bus machine id": lambda r, b: _write(r / "var/lib/dbus/machine-id", "x"),
    "a random seed": lambda r, b: _write(r / "var/lib/systemd/random-seed", "x"),
    "a credential secret": lambda r, b: _write(r / "var/lib/systemd/credential.secret", "x"),
    "a NetworkManager key": lambda r, b: _write(r / "var/lib/NetworkManager/secret_key", "x"),
    "a saved Wi-Fi network": lambda r, b: _write(
        r / "etc/NetworkManager/system-connections/Heimnetz-Beispiel.nmconnection", "[wifi]\n"
    ),
    "root's shell history": lambda r, b: _write(r / "root/.bash_history", "ls\n"),
    "root's cache": lambda r, b: _write(r / "root/.cache/uv/x", "x"),
    "root's git identity": lambda r, b: _write(r / "root/.gitconfig", "[user]\n"),
    "a user's cache": lambda r, b: _write(r / "home/nox-admin/.cache/x", "x"),
    "apt credentials": lambda r, b: _write(r / "etc/apt/auth.conf.d/mirror.conf", "machine x"),
    "an apt proxy": lambda r, b: _write(
        r / "etc/apt/apt.conf.d/51cache", 'Acquire::http::Proxy "http://proxy.example.invalid";\n'
    ),
    "credentials in the source's remote": lambda r, b: _write(
        r / "opt/nox/src/.git/config",
        '[remote "origin"]\n\turl = https://user:secret@github.com/Crackxsy/nox\n',
    ),
    "another remote for the source": lambda r, b: _write(
        r / "opt/nox/src/.git/config", '[remote "origin"]\n\turl = /var/tmp/nox-image/src\n'
    ),
    "a cloud-init seed": lambda r, b: _write(b / "user-data", "#cloud-config\n"),
    "an ssh file": lambda r, b: _write(b / "ssh"),
    "a userconf.txt": lambda r, b: _write(b / "userconf.txt", "pi:x\n"),
    "a custom.toml": lambda r, b: _write(b / "custom.toml", "[ssh]\n"),
    "a first-run script in cmdline.txt": lambda r, b: _write(
        b / "cmdline.txt", "root=PARTUUID=0000-02 systemd.run=/boot/firstrun.sh\n"
    ),
    "init= in cmdline.txt": lambda r, b: _write(
        b / "cmdline.txt", "root=/dev/mmcblk0p2 init=/bin/sh\n"
    ),
    "cloud-init installed": lambda r, b: _write(r / "usr/bin/cloud-init"),
    "Raspberry Pi Connect installed": lambda r, b: _write(r / "usr/bin/rpi-connect"),
    "userconf-pi installed": lambda r, b: _write(r / "usr/lib/userconf-pi/userconf"),
    "a download cache": lambda r, b: _write(r / "opt/nox/.cache/uv/x"),
    "nox.service not enabled": lambda r, b: (
        r / "etc/systemd/system/multi-user.target.wants/nox.service"
    ).unlink(),
    # O2
    "nox-ssh.service enabled": lambda r, b: _link(
        r / "etc/systemd/system/multi-user.target.wants/nox-ssh.service", "/x"
    ),
    "authorized_keys under /etc": lambda r, b: _write(r / "etc/nox-os/ssh/authorized_keys", "x"),
    "sshd with passwords": lambda r, b: _replace(
        r / "etc/nox-os/ssh/sshd_config", "PasswordAuthentication no", "PasswordAuthentication yes"
    ),
    "sshd for everyone": lambda r, b: _replace(
        r / "etc/nox-os/ssh/sshd_config", "AllowUsers nox-admin@10.0.0.0/8", "AllowUsers *"
    ),
    "sshd including other files": lambda r, b: _append(
        r / "etc/nox-os/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\n"
    ),
    "sudo outside /run": lambda r, b: _write(r / "etc/sudoers.d/50-nox-os", "@includedir /etc/x\n"),
    "no root helper": lambda r, b: (
        r / "etc/systemd/system/sockets.target.wants/nox-os-helper.socket"
    ).unlink(),
    "nox.service without its sandbox": lambda r, b: _replace(
        r / "etc/systemd/system/nox.service", "ProtectSystem=strict", "ProtectSystem=full"
    ),
    "nftables not enabled": lambda r, b: (
        r / "etc/systemd/system/sysinit.target.wants/nftables.service"
    ).unlink(),
    "no firewall path unit": lambda r, b: (
        r / "etc/systemd/system/multi-user.target.wants/nox-firewall.path"
    ).unlink(),
    "a boot firewall that accepts": lambda r, b: _replace(
        r / "etc/nftables.conf",
        "hook input priority filter; policy drop;",
        "hook input priority filter; policy accept;",
    ),
    "a boot firewall with SSH open": lambda r, b: _replace(
        r / "etc/nftables.conf", 'iif "lo" accept', 'iif "lo" accept\n    tcp dport 22 accept'
    ),
    "a persistent journal": lambda r, b: (r / "var/log/journal").mkdir(parents=True),
    "no journald limit": lambda r, b: (r / "etc/systemd/journald.conf.d/50-nox-os.conf").unlink(),
    "/ with access times": lambda r, b: _replace(
        r / "etc/fstab", "defaults,noatime,commit=60", "defaults"
    ),
    "/tmp on the card": lambda r, b: _replace(r / "etc/fstab", "tmpfs /tmp ", "#tmpfs /tmp "),
    "a swap file": lambda r, b: _write(r / "var/swap", "x"),
    "a swap file in fstab": lambda r, b: _append(r / "etc/fstab", "/var/swap none swap sw 0 0\n"),
    "swap on the card": lambda r, b: _write(
        r / "etc/rpi/swap.conf.d/50-nox-os.conf", "[Main]\nMechanism=auto\n"
    ),
    "no kernel hardening": lambda r, b: (r / "etc/sysctl.d/90-nox-os.conf").unlink(),
    "Bluetooth not masked": lambda r, b: (r / "etc/systemd/system/bluetooth.service").unlink(),
    "a login prompt on tty1": lambda r, b: (r / "etc/systemd/system/getty@tty1.service").unlink(),
    "authorized_keys anywhere else": lambda r, b: _write(r / "opt/nox/authorized_keys2", "x"),
    "shipped remote-maintenance keys": lambda r, b: _write(r / "var/lib/nox-os/ssh_keys", "x"),
    "the helper socket for the group nox": lambda r, b: _replace(
        r / "etc/systemd/system/nox-os-helper.socket",
        "SocketGroup=nox-os-helper",
        "SocketGroup=nox",
    ),
    "the helper socket for everyone": lambda r, b: _replace(
        r / "etc/systemd/system/nox-os-helper.socket", "SocketMode=0660", "SocketMode=0666"
    ),
    "a member in the helper's group": lambda r, b: _replace(
        r / "etc/group", "nox-os-helper:x:998:", "nox-os-helper:x:998:nox"
    ),
    "nox.service without the helper's group": lambda r, b: _replace(
        r / "etc/systemd/system/nox.service.d/50-nox-os.conf",
        "SupplementaryGroups=nox-os-helper\n",
        "",
    ),
    "nox.service without the firewall": lambda r, b: _replace(
        r / "etc/systemd/system/nox.service.d/50-nox-os.conf", "Requires=nftables.service\n", ""
    ),
    "authorized_keys under /usr": lambda r, b: _write(r / "usr/share/doc/x/authorized_keys", "x"),
    "a link named like the manual page": lambda r, b: _link(
        r / "usr/share/man/de/man5/authorized_keys.5.gz", "/home/nox-admin/.ssh/authorized_keys"
    ),
    "a shipped maintenance passphrase": lambda r, b: _write(r / "var/lib/nox-os/maintenance", "x"),
    "a boot firewall letting SSH in from the device itself": lambda r, b: _replace(
        r / "etc/nftables.conf", 'iif "lo" tcp dport 22 drop\n', ""
    ),
    # O3
    "systemd-firstboot not masked": lambda r, b: (
        r / "etc/systemd/system/systemd-firstboot.service"
    ).unlink(),
    "no first-boot unit": lambda r, b: (
        r / "etc/systemd/system/sysinit.target.wants/nox-firstboot.service"
    ).unlink(),
    "the setup portal enabled": lambda r, b: _link(
        r / "etc/systemd/system/multi-user.target.wants/nox-setup-portal.service", "/x"
    ),
    "the setup portal as root": lambda r, b: _replace(
        r / "etc/systemd/system/nox-setup-portal.service", "User=nox-setup\n", ""
    ),
    "the setup portal on every network": lambda r, b: _replace(
        r / "etc/systemd/system/nox-setup-portal.service", "IPAddressDeny=any\n", ""
    ),
    "a login for the portal's user": lambda r, b: _replace(
        r / "etc/passwd", ":/usr/sbin/nologin", ":/bin/bash"
    ),
    "a member in the setup-secrets group": lambda r, b: _replace(
        r / "etc/group", "nox-os-setup:x:997:", "nox-os-setup:x:997:nox"
    ),
    "no captive DNS": lambda r, b: (
        r / "etc/NetworkManager/dnsmasq-shared.d/50-nox-setup.conf"
    ).unlink(),
    "a provisioning file on the boot partition": lambda r, b: _write(
        b / "nox-provision.json", '{"v": 1}'
    ),
    "a shipped claim mark": lambda r, b: _write(r / "var/lib/nox-os-claim/claimed", "{}"),
    "a reclaim marker on the boot partition": lambda r, b: _write(b / "nox-reclaim", "reclaim"),
    "a root unit running Nox's Python": lambda r, b: _write(
        r / "etc/systemd/system/nox-firewall.service",
        "[Service]\nExecStart=/opt/nox/current/.venv/bin/python -I /usr/local/lib/nox-os/x.py\n",
    ),
    "a root setup program calling Nox's Python": lambda r, b: _write(
        r / "usr/local/lib/nox-os/nox_x.py", 'PY = "/opt/nox/current/.venv/bin/python"\n'
    ),
    "a shipped test window": lambda r, b: _write(r / "etc/nox-os/testing/setup-window-s", "60"),
}


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text("utf-8")
    assert old in text, (path, old)
    path.write_text(text.replace(old, new), "utf-8")


def _append(path: Path, line: str) -> None:
    path.write_text(path.read_text("utf-8") + line, "utf-8")


@pytest.mark.parametrize("spoiler", sorted(SPOILERS))
def test_each_forbidden_item_fails_the_image(tmp_path: Path, spoiler: str) -> None:
    root, boot = _clean_tree(tmp_path)
    SPOILERS[spoiler](root, boot)
    result = _check(root, boot)
    assert result.returncode == 1, f"{spoiler} passed:\n{result.stdout}"
    assert result.stdout.count("FAIL") == 1, result.stdout
