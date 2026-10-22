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
    run = " ".join(step["run"].split())
    assert run.startswith(
        "printf '%s\\n' \"$NOX_RELEASE_SIGNING_KEY\" | env -u NOX_RELEASE_SIGNING_KEY"
    )
    assert "python -I -S source/scripts/sign_release.py --key-stdin --allow-unsigned" in run


def test_only_a_release_signs_or_writes() -> None:
    jobs = _workflow()["jobs"]
    for name in ("sign", "publish"):
        assert jobs[name]["if"] == "github.event_name == 'release'", name
    upload = next(s for s in jobs["publish"]["steps"] if s.get("name") == "Upload")
    assert 'if [ "$SIGNED" = true ]; then' in upload["run"]


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
