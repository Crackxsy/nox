"""The Nox-OS firewall generator (os/pi-gen/stage-nox/03-hardening/files/nox_firewall.py): default
deny, Nox's ports from its configuration and only while network access is on, from private
networks only, SSH only while remote maintenance is on, and nothing the configuration could use
to open SSH or a privileged port."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

import pytest

from nox.lan.tls import PERMITTED_NETWORKS
from tests.unit.nox_os.conftest import HARDENING, REPO, load_tool

fw = load_tool("nox_firewall", HARDENING)


def rules(**settings: object) -> str:
    return str(fw.render(fw.Settings(**settings)))


LOCAL_SSH_DROP = 'iif "lo" tcp dport 22 drop'


def input_chain(script: str) -> str:
    """The input chain, without the rule that drops SSH from the device itself (its own test):
    what is left mentions a port only to let it in."""
    chain = script.split("chain input {", 1)[1].split("}\n  chain forward", 1)[0]
    assert f"\n    {LOCAL_SSH_DROP}\n" in chain
    return chain.replace(f"    {LOCAL_SSH_DROP}\n", "")


@pytest.mark.parametrize("settings", [{}, {"ssh": True}, {"ssh": True, "lan_enabled": True}])
def test_ssh_from_the_device_itself_is_dropped_before_anything_else(
    settings: dict[str, bool],
) -> None:
    lines = rules(**settings).split("chain input {", 1)[1].splitlines()
    # Before `iif "lo" accept`: a local process must not reach SSH, even while it is on.
    assert lines[2].strip() == LOCAL_SSH_DROP
    assert lines[3].strip() == 'iif "lo" accept'


def test_the_baseline_opens_nothing_of_noxs() -> None:
    script = str(fw.render(fw.Settings(), boot=True))
    assert "policy drop;" in input_chain(script)
    assert "flush ruleset" in script
    for port in (22, 47843, 47844, 80, 47800, 47801):
        assert f"dport {port} " not in input_chain(script), port
    assert "udp dport 5353 accept" in script  # mDNS, from private networks
    assert "udp sport 67 udp dport 68 accept" in script  # the DHCP client
    # A reload never flushes other tables (NetworkManager's hotspot keeps its own).
    assert "flush ruleset" not in rules()


def test_the_table_is_replaced_atomically() -> None:
    lines = rules().splitlines()
    assert lines[3:6] == ["table inet nox_os", "delete table inet nox_os", "table inet nox_os {"]


def test_network_access_opens_the_dashboard_and_node_ports_rate_limited() -> None:
    chain = input_chain(rules(lan_enabled=True))
    for port in (47843, 47844):
        for family, private in (("ip", "@private4"), ("ip6", "@private6")):
            assert f"{family} saddr {private} tcp dport {port} ct state new accept" in chain
            assert f"limit rate over {fw.DASHBOARD_RATE} }} drop" in chain
        # The rate limit comes before the accept, per address family.
        assert chain.index(f"add @rate4_{port}") < chain.index(
            f"tcp dport {port} ct state new accept"
        )
    assert "dport 22 " not in chain


def test_public_clients_only_when_the_configuration_says_so() -> None:
    chain = input_chain(rules(lan_enabled=True, public=True))
    assert "meta nfproto ipv4 tcp dport 47843 ct state new accept" in chain
    assert "meta nfproto ipv6 tcp dport 47843 ct state new accept" in chain
    assert "add @rate6_47843 { ip6 saddr limit" in chain
    # mDNS stays in the home network either way.
    assert "ip saddr @private4 udp dport 5353 accept" in chain


def test_ssh_only_while_remote_maintenance_is_on_and_only_from_private_networks() -> None:
    chain = input_chain(rules(ssh=True))
    assert "ip saddr @private4 tcp dport 22 ct state new accept" in chain
    assert "ip6 saddr @private6 tcp dport 22 ct state new accept" in chain
    assert "add @rate4_22 { ip saddr limit rate over 10/minute" in chain
    for line in chain.splitlines():
        if "dport 22 " in line:
            assert line.strip().startswith(("ip saddr @private4 ", "ip6 saddr @private6 ")), line


def test_setup_mode_opens_the_portal_ports() -> None:
    chain = input_chain(rules(setup=True))
    assert "tcp dport 80 accept" in chain and "udp dport { 53, 67 } accept" in chain
    assert "dport 80" not in rules()


def test_outbound_is_open_except_cloud_metadata() -> None:
    script = rules()
    output = script.split("chain output {", 1)[1]
    assert "policy accept;" in output
    assert "ip daddr 169.254.169.254 drop" in output and "ip6 daddr fd00:ec2::254 drop" in output
    assert "chain forward {\n    type filter hook forward priority filter; policy drop;" in script


def test_the_private_networks_are_the_ones_nox_answers() -> None:
    ours = {ipaddress.ip_network(n) for n in (*fw.PRIVATE_V4, *fw.PRIVATE_V6)}
    nox = {n for n in PERMITTED_NETWORKS if not n.is_loopback}
    assert ours == nox


def test_settings_follow_the_configuration_user_over_defaults() -> None:
    defaults = {"ipc": {"lan": {"enabled": False, "port": 47843, "node_port": 47844}}}
    user = {"ipc": {"lan": {"enabled": True, "port": 48000}}}
    got = fw.settings_from(defaults, user, ssh=False, setup=False)
    assert (got.lan_enabled, got.port, got.node_port, got.public) == (True, 48000, 47844, False)
    assert fw.settings_from(defaults, {}, ssh=False, setup=False).lan_enabled is False


@pytest.mark.parametrize("port", [22, 80, 443, 1023, 0, 47800, 47801, 5353, 70000, True, "48000"])
def test_the_configuration_cannot_open_ssh_or_a_privileged_port(port: object) -> None:
    user = {"ipc": {"lan": {"enabled": True, "port": port, "node_port": port}}}
    settings = fw.settings_from({}, user, ssh=False, setup=False)
    chain = input_chain(fw.render(settings))
    assert f"tcp dport {port} " not in chain
    assert "dport 22 " not in chain
    if not isinstance(port, int) or isinstance(port, bool):
        assert settings.port == fw.DEFAULT_PORT  # a wrong type falls back, not through


def test_the_shipped_defaults_are_what_the_fallback_assumes() -> None:
    import yaml

    defaults = yaml.safe_load((REPO / "config" / "defaults.yaml").read_text("utf-8"))
    lan = defaults["ipc"]["lan"]
    assert (lan["port"], lan["node_port"]) == (fw.DEFAULT_PORT, fw.DEFAULT_NODE_PORT)
    assert fw.openable(lan["port"]) and fw.openable(lan["node_port"])
    http = defaults["ipc"].get("http_port", 47801)
    assert http in fw.RESERVED_PORTS  # loopback-only ports are never opened


def test_a_broken_or_linked_user_file_counts_as_absent(tmp_path: Path) -> None:
    broken = tmp_path / "user.yaml"
    broken.write_text("ipc: [unclosed", "utf-8")
    assert fw._read_yaml(broken) == {}  # noqa: SLF001
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (fw.MAX_CONFIG_BYTES + 1))
    assert fw._read_yaml(big) == {}  # noqa: SLF001
    if sys.platform != "win32":
        target = tmp_path / "real.yaml"
        target.write_text("ipc:\n  lan:\n    enabled: true\n", "utf-8")
        link = tmp_path / "link.yaml"
        os.symlink(target, link)
        assert fw._read_yaml(link) == {}  # noqa: SLF001
        assert fw._read_yaml(target) == {"ipc": {"lan": {"enabled": True}}}  # noqa: SLF001


@pytest.mark.skipif(sys.platform == "win32", reason="file ownership")
def test_ssh_counts_only_while_its_time_runs(tmp_path: Path) -> None:
    state = tmp_path / "ssh.json"
    state.write_text(json.dumps({"on": True, "until": 2_000}), "utf-8")
    own = os.stat(state).st_uid == 0  # the state file must belong to root
    assert fw.ssh_on(state, now=1_000) is own
    assert fw.ssh_on(state, now=3_000) is False
    state.write_text(json.dumps({"on": False, "until": 2_000}), "utf-8")
    assert fw.ssh_on(state, now=1_000) is False
    assert fw.ssh_on(tmp_path / "missing.json") is False
