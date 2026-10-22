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

from tests.unit.nox_os.conftest import TOOLS

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
    return root, boot


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
    "a password in passwd": lambda r, b: _write(r / "etc/passwd", "root:abc:0:0::/root:/bin/sh\n"),
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
}


@pytest.mark.parametrize("spoiler", sorted(SPOILERS))
def test_each_forbidden_item_fails_the_image(tmp_path: Path, spoiler: str) -> None:
    root, boot = _clean_tree(tmp_path)
    SPOILERS[spoiler](root, boot)
    result = _check(root, boot)
    assert result.returncode == 1, f"{spoiler} passed:\n{result.stdout}"
    assert result.stdout.count("FAIL") == 1, result.stdout
