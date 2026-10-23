"""The Nox-OS build (os/pi-gen, .github/workflows/os-image.yml) ships nothing that must be made per
device or never exist, and keeps the release key where docs/RELEASES.md says it lives."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest
import yaml

from tests.unit.nox_os.conftest import REPO

PIGEN = REPO / "os" / "pi-gen"
WORKFLOW = REPO / ".github" / "workflows" / "os-image.yml"
INSTALLER = REPO / "scripts" / "install-linux.sh"
SECRET = "secrets.NOX_RELEASE_SIGNING_KEY"


def _config() -> dict[str, str]:
    values = {}
    for line in (PIGEN / "config").read_text("utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def _workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    return data


def test_no_credentials_and_no_remote_access_in_the_build_settings() -> None:
    config = _config()
    for key in (
        "FIRST_USER_PASS",
        "PUBKEY_SSH_FIRST_USER",
        "WPA_PASSWORD",
        "WPA_ESSID",
        "APT_PROXY",
        "PASSWORDLESS_SUDO",
        "TEMP_REPO",
        "WPA_COUNTRY",
    ):
        assert key not in config, key
    assert config["ENABLE_SSH"] == "0"
    assert config["ENABLE_CLOUD_INIT"] == "0"
    assert config["DISABLE_FIRST_BOOT_USER_RENAME"] == "1"
    assert config["FIRST_USER_NAME"] == "nox-admin"
    assert config["STAGE_LIST"].split() == [
        "/pi-gen/stage0",
        "/pi-gen/stage1",
        "/pi-gen/stage2",
        "/pi-gen/stage-nox",
    ]


def test_the_first_users_build_password_is_random_and_replaced_by_a_lock() -> None:
    build = (PIGEN / "build.sh").read_text("utf-8")
    assert 'throwaway="$(openssl rand -hex 32)"' in build
    system = (PIGEN / "stage-nox" / "02-system" / "00-run.sh").read_text("utf-8")
    assert """usermod --password '!' "${FIRST_USER_NAME}\"""" in system
    assert "systemctl mask ssh.service ssh.socket sshswitch.service" in system
    assert 'rm -f "${ROOTFS_DIR}"/etc/ssh/ssh_host_*' in system


def test_pi_gen_is_pinned_by_tag_and_commit() -> None:
    build = (PIGEN / "build.sh").read_text("utf-8")
    assert re.search(r'^PI_GEN_TAG="\d{4}-\d{2}-\d{2}-raspios-[a-z]+-arm64"$', build, re.M)
    assert re.search(r'^PI_GEN_COMMIT="[0-9a-f]{40}"$', build, re.M)
    assert '= "$PI_GEN_COMMIT" ]' in build


def test_stage_scripts_are_executable_in_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-s", "os"], capture_output=True, text=True, check=True
    ).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in listing.splitlines()}
    for script in (
        "os/pi-gen/build.sh",
        "os/pi-gen/stage-nox/prerun.sh",
        "os/pi-gen/stage-nox/01-nox/00-run.sh",
        "os/pi-gen/stage-nox/02-system/00-run.sh",
        "os/pi-gen/stage-nox/03-hardening/00-run.sh",
        "os/pi-gen/stage-nox/04-firstboot/00-run.sh",
        "os/pi-gen/export-nox/00-run.sh",
        "os/tools/check-image.sh",
        "os/tools/qemu-smoke.sh",
    ):
        if script in modes:  # only once committed
            assert modes[script] == "100755", script


def test_every_action_is_pinned_and_nothing_gets_more_than_it_needs() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {}
    for name, job in workflow["jobs"].items():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
        expected = {"contents": "write"} if name == "publish" else {"contents": "read"}
        assert job["permissions"] == expected, name


def test_the_key_is_used_in_one_step_of_the_protected_job_only() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert text.count(SECRET) == 1
    jobs = _workflow()["jobs"]
    holders = [
        (name, step.get("name"))
        for name, job in jobs.items()
        for step in job["steps"]
        if SECRET in str(step.get("env", {}))
    ]
    assert holders == [("sign", "Write the OS manifest and sign it")]
    assert jobs["sign"]["environment"] == "release-signing"
    step = next(s for s in jobs["sign"]["steps"] if s.get("name") == holders[0][1])
    run = " ".join(step["run"].replace("\\\n", " ").split())
    # No key: stop before anything is signed or uploaded (no unsigned manifest, ever).
    assert run.startswith('if [ -z "$NOX_RELEASE_SIGNING_KEY" ]; then')
    assert (
        "printf '%s\\n' \"$NOX_RELEASE_SIGNING_KEY\" | env -u NOX_RELEASE_SIGNING_KEY "
        "python -I -S source/scripts/sign_release.py --key-stdin --kind os-image" in run
    )
    assert "--allow-unsigned" not in WORKFLOW.read_text("utf-8")


def test_no_secret_and_no_privileged_trigger_outside_the_signing_job() -> None:
    workflow = _workflow()
    # PyYAML reads the key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request", "workflow_dispatch", "release", "schedule"}
    for name, job in workflow["jobs"].items():
        if name != "sign":
            assert "secrets." not in str(job), name


def test_only_a_signed_release_writes() -> None:
    jobs = _workflow()["jobs"]
    for name in ("sign", "publish"):
        assert jobs[name]["if"] == "github.event_name == 'release'", name
    assert jobs["publish"]["needs"] == ["image", "sign"]
    publish = str(jobs["publish"])
    assert "nox-os-manifest.json.sig" in publish
    assert "SIGNED" not in publish  # no unsigned branch left to take
    check = next(s for s in jobs["publish"]["steps"] if "lists exactly" in s.get("name", ""))
    assert 'manifest.get("kind") != "os-image"' in check["run"]


def test_the_smoke_boots_only_a_copy() -> None:
    smoke = (REPO / "os" / "tools" / "qemu-smoke.sh").read_text("utf-8")
    assert smoke.count('"$image"') == 1
    assert 'cp --sparse=always "$image" "$copy"' in smoke
    assert '-drive "file=$copy,if=sd,format=raw"' in smoke
    assert "losetup --read-only --find --show --offset" in smoke


def test_the_build_runs_on_arm64_and_boots_the_image() -> None:
    job = _workflow()["jobs"]["image"]
    assert job["runs-on"] == "ubuntu-24.04-arm"
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "os/tools/check-image.sh" in runs
    assert "os/tools/qemu-smoke.sh" in runs


def _installer(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *args], capture_output=True, text=True, timeout=30, env=env
    )


linux_bash = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None, reason="the installer runs on Linux"
)


@linux_bash
@pytest.mark.parametrize(
    "args",
    [("--image", "--version", "v0.7.0"), ("--node", "--headless", "--version", "v0.7.0")],
)
def test_the_installer_knows_image_and_node_mode(args: tuple[str, ...]) -> None:
    # Past the option parser, a normal user stops at "needs root" - never at "unknown option".
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    result = _installer(*args)
    assert result.returncode == 1, result.stderr
    assert "Unknown option" not in result.stderr


@linux_bash
@pytest.mark.parametrize(
    ("args", "action"),
    [
        (("--ui-bundle", "nox-ui.tar.gz"), "install"),
        (("--image",), "rollback"),
        (("--no-such-option",), "install"),
        (("--image", "--node"), "install"),
        (("--node", "--headless", "--ui-bundle", "nox-ui.tar.gz"), "install"),
    ],
)
def test_the_installer_refuses_options_that_do_not_belong(
    args: tuple[str, ...], action: str
) -> None:
    result = _installer(*args, env={**os.environ, "NOX_ACTION": action})
    assert result.returncode == 2, result.stderr


# ---- O2: hardening ----------------------------------------------------------------------------

HARDENING_STAGE = PIGEN / "stage-nox" / "03-hardening"


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    """A systemd unit file as {section: {key: [values]}} (repeated keys kept)."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for raw in (HARDENING_STAGE / "files" / name).read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


def test_remote_maintenance_can_never_be_enabled_at_boot() -> None:
    ssh = _unit("nox-ssh.service")
    assert "Install" not in ssh  # nothing to enable: only the helper starts it
    service = ssh["Service"]
    assert service["ExecStart"] == ["/usr/sbin/sshd -D -e -f /etc/nox-os/ssh/sshd_config"]
    assert service["KillMode"] == ["control-group"]  # open sessions end with it
    assert "--stopped" in service["ExecStopPost"][0]
    run = (HARDENING_STAGE / "00-run.sh").read_text("utf-8")
    assert "nox-ssh.service" not in run.split("systemctl enable", 1)[1].split("\n", 2)[0]
    # O1's masks stay untouched.
    system = (PIGEN / "stage-nox" / "02-system" / "00-run.sh").read_text("utf-8")
    assert "systemctl mask ssh.service ssh.socket sshswitch.service" in system
    assert "unmask" not in run


def test_the_root_helper_is_for_nox_service_only_and_has_no_privileges() -> None:
    socket = _unit("nox-os-helper.socket")["Socket"]
    assert socket["ListenStream"] == ["/run/nox-os/helper/helper.sock"]
    assert (socket["SocketUser"], socket["SocketGroup"], socket["SocketMode"]) == (
        ["root"],
        ["nox-os-helper"],
        ["0660"],
    )
    assert socket["Accept"] == ["yes"]
    # A flood trips the trigger limit; the socket then comes back on its own after a pause.
    assert (socket["TriggerLimitIntervalSec"], socket["TriggerLimitBurst"]) == (["10s"], ["20"])
    assert _unit("nox-os-helper.socket")["Unit"]["OnFailure"] == ["nox-os-helper-recover.service"]
    recover = _unit("nox-os-helper-recover.service")["Service"]
    assert recover["ExecStart"][-1] == "/usr/bin/systemctl start nox-os-helper.socket"
    assert recover["ExecStartPre"] == ["/usr/bin/sleep 30"]
    tmpfiles = (HARDENING_STAGE / "files" / "tmpfiles-nox-os.conf").read_text("utf-8")
    assert re.search(r"^d /run/nox-os/helper\s+0750 root nox-os-helper ", tmpfiles, re.M)
    # The keys are the helper's: root only.
    assert re.search(r"^d /var/lib/nox-os\s+0700 root root ", tmpfiles, re.M)
    # Only nox.service carries the group - as a drop-in, and nobody is made a member of it.
    dropin = _unit("nox-service-nox-os.conf")
    assert dropin["Service"] == {"SupplementaryGroups": ["nox-os-helper"]}
    # No firewall, no Nox.
    assert dropin["Unit"] == {"Requires": ["nftables.service"], "After": ["nftables.service"]}
    run = (HARDENING_STAGE / "00-run.sh").read_text("utf-8")
    assert "groupadd --system nox-os-helper" in run
    assert "usermod" not in run and "gpasswd" not in run and "adduser" not in run
    service = _unit("nox-os-helper@.service")["Service"]
    assert service["ExecStart"] == ["/usr/bin/python3 -I -S /usr/local/lib/nox-os/nox_os_helper.py"]
    for key, value in (
        ("CapabilityBoundingSet", ""),
        ("ProtectSystem", "strict"),
        ("ReadWritePaths", "/run/nox-os -/var/lib/nox-os"),
        ("PrivateNetwork", "yes"),
        ("NoNewPrivileges", "yes"),
        ("RestrictAddressFamilies", "AF_UNIX"),
    ):
        assert service[key] == [value], key
    helper = (HARDENING_STAGE / "files" / "nox_os_helper.py").read_text("utf-8")
    assert "shell=True" not in helper and "os.system" not in helper


def test_sshd_allows_keys_from_private_networks_only() -> None:
    config = (HARDENING_STAGE / "files" / "sshd_config").read_text("utf-8").splitlines()
    assert not [line for line in config if line.lower().startswith("include")]
    allow = next(line for line in config if line.startswith("AllowUsers "))
    networks = [entry.removeprefix("nox-admin@") for entry in allow.split()[1:]]
    assert all(entry.startswith("nox-admin@") for entry in allow.split()[1:])
    from tests.unit.nox_os.conftest import HARDENING, load_tool

    firewall = load_tool("nox_firewall", HARDENING)
    assert set(networks) == set(firewall.PRIVATE_V4) | set(firewall.PRIVATE_V6)
    algorithms = next(line for line in config if line.startswith("PubkeyAcceptedAlgorithms "))
    assert "rsa" not in algorithms and "dss" not in algorithms


def test_the_console_status_runs_without_rights() -> None:
    console = _unit("nox-console.service")
    assert console["Service"]["DynamicUser"] == ["yes"]
    assert console["Service"]["TTYPath"] == ["/dev/tty1"]
    assert console["Service"]["CapabilityBoundingSet"] == [""]
    assert "getty@tty1.service" in console["Unit"]["Conflicts"][0]


def test_the_firewall_fails_closed() -> None:
    service = _unit("nox-firewall.service")
    assert service["Unit"]["Requires"] == ["nftables.service"]
    run = (HARDENING_STAGE / "00-run.sh").read_text("utf-8")
    assert "nox_firewall.py baseline > /etc/nftables.conf" in run
    assert "nftables.service nox-firewall.service nox-firewall.path nox-os-helper.socket" in run


def test_o2_is_checked_by_the_workflow() -> None:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["check"]["steps"]}
    run = steps["Firewall rules, sshd and sudo configuration are valid"]["run"]
    assert "sudo nft -c -f rules.nft" in run and "sshd -t -f" in run and "visudo -cf" in run
    assert "03-hardening/files/*.py" in steps["Python tools compile"]["run"]


# ---- O3: first boot without a screen --------------------------------------------------------

FIRSTBOOT_STAGE = PIGEN / "stage-nox" / "04-firstboot"


def _o3_unit(name: str) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for raw in (FIRSTBOOT_STAGE / "files" / name).read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


def test_the_first_boot_runs_before_the_network_and_nox() -> None:
    unit = _o3_unit("nox-firstboot.service")
    before = " ".join(unit["Unit"]["Before"])
    for name in (
        "network-pre.target",
        "NetworkManager.service",
        "nox.service",
        "nox-firewall.service",
    ):
        assert name in before, name
    assert unit["Unit"]["RequiresMountsFor"] == ["/boot/firmware /var/lib"]
    assert unit["Install"]["WantedBy"] == ["sysinit.target"]
    run = (FIRSTBOOT_STAGE / "00-run.sh").read_text("utf-8")
    assert "systemctl mask systemd-firstboot.service" in run
    assert "systemctl enable nox-firstboot.service nox-setup.timer nox-setup-apply.path" in run


def test_the_setup_portal_is_unprivileged_and_never_enabled() -> None:
    unit = _o3_unit("nox-setup-portal.service")
    assert "Install" not in unit
    service = unit["Service"]
    for key, value in (
        ("User", "nox-setup"),
        ("CapabilityBoundingSet", "CAP_NET_BIND_SERVICE"),
        ("AmbientCapabilities", "CAP_NET_BIND_SERVICE"),
        ("NoNewPrivileges", "yes"),
        ("ProtectSystem", "strict"),
        ("ReadWritePaths", "/run/nox-os/setup/portal"),
        ("IPAddressDeny", "any"),
        ("IPAddressAllow", "10.42.0.0/24 127.0.0.1"),
        ("RestrictAddressFamilies", "AF_INET AF_UNIX"),
    ):
        assert service[key] == [value], key
    run = (FIRSTBOOT_STAGE / "00-run.sh").read_text("utf-8")
    assert "--shell /usr/sbin/nologin nox-setup" in run
    assert "groupadd --system nox-os-setup" in run and "usermod" not in run


def test_the_setup_window_has_one_clock_and_closes_itself() -> None:
    timer = _o3_unit("nox-setup.timer")["Timer"]
    assert timer["OnUnitActiveSec"] == ["15s"]
    setup = (FIRSTBOOT_STAGE / "files" / "nox_setup.py").read_text("utf-8")
    assert "WINDOW_S = 30 * 60" in setup and "CLOCK_BOOTTIME" in setup
    assert "shell=True" not in setup and "os.system" not in setup
    close = _o3_unit("nox-setup-close.service")["Service"]
    assert close["ExecStart"] == [
        "/usr/bin/python3 -I -S /usr/local/lib/nox-os/nox_setup.py close claimed"
    ]
    # The root helper may write the claim mark, nothing more.
    dropin = _o3_unit("nox-os-helper-claim.conf")
    assert dropin == {"Service": {"ReadWritePaths": ["/var/lib/nox-os-claim"]}}


def test_o3_is_checked_by_the_workflow() -> None:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["check"]["steps"]}
    assert "04-firstboot/files/*.py" in steps["Python tools compile"]["run"]
