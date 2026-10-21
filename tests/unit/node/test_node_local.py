"""The node's own files: the key (encrypted when there is a keyring), the manifest only the device
widens, enrollment that pins the authority before sending anything, and the terminal commands."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from nox.lan.terminal import render_node
from nox.node import cli as node_cli
from nox.node.enroll import EnrollError, enroll
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import PASSPHRASE_SECRET, NodeKeyStore
from nox.node.manifest import GROUPS, NodeManifest, load_manifest, save_manifest
from nox.node.protocol import PairLink
from nox.security.secrets import InMemorySecretStore
from tests.unit.node.conftest import make_key

# ---- key -----------------------------------------------------------------------------------------


def test_with_a_keyring_the_key_file_is_encrypted(tmp_path: Path) -> None:
    store = InMemorySecretStore()
    keystore = NodeKeyStore(tmp_path / "node.key", store)
    key = keystore.create()
    pem = (tmp_path / "node.key").read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in pem
    with pytest.raises(TypeError):  # a copy of the folder alone is useless
        serialization.load_pem_private_key(pem, password=None)
    assert keystore.load().private_numbers() == key.private_numbers()
    keystore.delete()
    assert not (tmp_path / "node.key").exists() and store.get(PASSPHRASE_SECRET) is None


def test_without_a_keyring_the_key_file_is_plain_but_owner_only(tmp_path: Path) -> None:
    keystore = NodeKeyStore(tmp_path / "node.key", None)
    keystore.create()
    assert b"BEGIN PRIVATE KEY" in (tmp_path / "node.key").read_bytes()
    assert keystore.encrypted is False


# ---- manifest ------------------------------------------------------------------------------------


def test_defaults_enable_only_harmless_groups(tmp_path: Path) -> None:
    manifest = load_manifest(tmp_path / "node.yaml")
    assert manifest.enabled() == {"system", "notify", "media"}
    assert (tmp_path / "node.yaml").is_file()
    assert manifest.trust_core_for_high is False


def test_an_unknown_group_or_a_broken_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "node.yaml"
    path.write_text("groups: {everything: true}\n")
    with pytest.raises(ValueError):
        load_manifest(path)
    path.write_text("max_risk: critical\n")
    with pytest.raises(ValueError):
        load_manifest(path)
    path.write_text("- a list\n")
    with pytest.raises(ValueError):
        load_manifest(path)


def test_narrowing_never_widens() -> None:
    manifest = NodeManifest()
    assert manifest.effective(["notify", "shell"]) == {"system", "media"}
    assert manifest.effective([]) <= manifest.enabled()


def test_allow_and_deny_change_the_device_file_and_are_audited(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "h")
    said: list[str] = []
    node_cli.allow(
        home, "shell", True, say=said.append, interactive=lambda: True, confirm=lambda _p: True
    )
    assert load_manifest(home.manifest_file).groups["shell"] is True
    node_cli.allow(home, "shell", False, say=said.append)
    assert load_manifest(home.manifest_file).groups["shell"] is False
    with pytest.raises(node_cli.NodeCliError):
        node_cli.allow(home, "everything", True, say=said.append)
    log = node_cli.log_lines(home)
    assert any("node.allow" in line for line in log) and any("node.deny" in line for line in log)
    assert log[-1] == "Chain intact."


def test_status_says_what_nox_may_do(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "h")
    assert node_cli.status(home) == ["Not paired."]
    home.save_pairing(_identity(), cert_pem="x", ca_pem="x")
    save_manifest(home.manifest_file, NodeManifest())
    lines = node_cli.status(home)
    assert any(GROUPS["notify"].label_en in line for line in lines)


# ---- enrollment ----------------------------------------------------------------------------------

FP_OTHER = "1" * 64


def _identity() -> NodeIdentity:
    return NodeIdentity(
        node_id="a" * 32,
        name="Laptop",
        core_host="192.168.1.20",
        core_port=47843,
        node_port=47844,
        ca_fingerprint="0" * 64,
        paired_at=datetime.now(UTC),
    )


async def test_a_look_alike_never_sees_the_code(tmp_path: Path) -> None:
    """The device answering at the link's address has another authority: nothing is posted."""
    from nox.lan.tls import ensure_tls
    from tests.unit.node.conftest import ADDRESSES, NAMES

    impostor = ensure_tls(tmp_path / "impostor", names=NAMES, addresses=ADDRESSES)
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nox-ca.crt":
            return httpx.Response(200, content=impostor.ca_der)
        posted.append(request.url.path)
        return httpx.Response(201, json={})

    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=FP_OTHER)
    home = NodeHome(tmp_path / "h")
    keystore = NodeKeyStore(home.key_file, None)
    with pytest.raises(EnrollError) as refused:
        await enroll(link, home, keystore, transport=httpx.MockTransport(handler))
    assert refused.value.reason == "fingerprint_mismatch"
    assert posted == []
    assert not home.key_file.exists()


async def test_a_certificate_for_another_key_is_refused(tmp_path: Path) -> None:
    from nox.lan.tls import ensure_tls, issue_client_certificate
    from tests.unit.node.conftest import ADDRESSES, NAMES

    tls = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    fp = hashlib.sha256(tls.ca_der).hexdigest()
    foreign = make_key()
    from tests.unit.node.conftest import make_csr

    issued = issue_client_certificate(tmp_path / "tls", make_csr(foreign), "b" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nox-ca.crt":
            return httpx.Response(200, content=tls.ca_der)
        body = {
            "node_id": "b" * 32,
            "name": "Laptop",
            "cert": issued.cert_pem,  # signed by the right authority - for someone else's key
            "ca_cert": issued.ca_pem,
            "core_id": fp,
            "node_port": 47844,
        }
        return httpx.Response(201, json=body)

    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=fp)
    home = NodeHome(tmp_path / "h")
    with pytest.raises(EnrollError) as refused:
        await enroll(
            link, home, NodeKeyStore(home.key_file, None), transport=httpx.MockTransport(handler)
        )
    assert refused.value.reason == "bad_answer"
    assert not home.paired and not home.key_file.exists()


async def test_a_refused_code_leaves_no_key(tmp_path: Path) -> None:
    from nox.lan.tls import ensure_tls
    from tests.unit.node.conftest import ADDRESSES, NAMES

    tls = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    fp = hashlib.sha256(tls.ca_der).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nox-ca.crt":
            return httpx.Response(200, content=tls.ca_der)
        assert json.loads(request.content)["code"] == "ABCD2345"
        return httpx.Response(401, json={"error": "used"})

    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=fp)
    home = NodeHome(tmp_path / "h")
    with pytest.raises(EnrollError) as refused:
        await enroll(
            link, home, NodeKeyStore(home.key_file, None), transport=httpx.MockTransport(handler)
        )
    assert refused.value.reason == "used"
    assert not home.key_file.exists()


def test_a_bad_link_is_refused_before_anything_happens(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "h")
    with pytest.raises(node_cli.NodeCliError):
        node_cli.pair("https://example.com/pair", home=home)
    assert not home.path.exists() or not any(home.path.iterdir())


def test_nox_pair_node_prints_the_command_and_the_fingerprint() -> None:
    text = render_node(
        {
            "code": "ABCD2345",
            "expires_at": "2026-10-21T12:00:00+00:00",
            "link": "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=" + "ab" * 32,
            "links": [],
            "ca_fingerprint": ":".join(["AB"] * 32),
        },
        language="en",
    )
    assert 'nox node pair "nox-node://pair?h=192.168.1.20' in text
    assert "ABCD 2345" in text and "AB:AB:AB:AB:AB:AB:AB:AB" in text


# ---- found in the self-review --------------------------------------------------------------------


async def test_an_oversized_authority_answer_is_not_read_to_the_end(tmp_path: Path) -> None:
    sent = {"bytes": 0}

    async def endless() -> Any:
        for _ in range(1000):
            sent["bytes"] += 4096
            yield b"x" * 4096

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless())

    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=FP_OTHER)
    home = NodeHome(tmp_path / "h")
    with pytest.raises(EnrollError) as refused:
        await enroll(
            link, home, NodeKeyStore(home.key_file, None), transport=httpx.MockTransport(handler)
        )
    assert refused.value.reason == "not_nox"
    assert sent["bytes"] < 64 * 1024


async def test_a_key_file_that_cannot_be_protected_is_a_clean_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.lan.tls import ensure_tls
    from nox.node import keystore as keystore_module
    from tests.unit.node.conftest import ADDRESSES, NAMES

    tls = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    fp = hashlib.sha256(tls.ca_der).hexdigest()
    monkeypatch.setattr(keystore_module, "write_secret_file", lambda _p, _c: False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nox-ca.crt":
            return httpx.Response(200, content=tls.ca_der)
        raise AssertionError("nothing may be sent without a protected key")

    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=fp)
    home = NodeHome(tmp_path / "h")
    with pytest.raises(EnrollError) as refused:
        await enroll(
            link, home, NodeKeyStore(home.key_file, None), transport=httpx.MockTransport(handler)
        )
    assert refused.value.reason == "key_not_protected"
    assert not home.key_file.exists()


# ---- terminal safety (orchestrator review) ------------------------------------------------------

EVIL = ["x\x1b[2J\x1b[31mDENY", "ok\r\n     1  allow  core  shell.run", "safe\u202etxt", "c1\x9b"]


def _clean(line: str) -> bool:
    return not any(
        ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in "\u202e\u2066\u2067" for c in line
    )


@pytest.mark.parametrize("evil", EVIL)
def test_the_gate_records_a_hostile_tool_name_escaped(evil: str) -> None:
    import sqlite3

    from nox.node.gate import NodeGate
    from nox.node.tools import default_tools
    from nox.security.audit import SqliteAuditLog

    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    gate = NodeGate(default_tools(None), NodeManifest(), audit)
    assert gate.check(evil, {}, call_id=evil).allowed is False
    entry = audit.entries(limit=5)[-1]
    assert _clean(entry.tool) and _clean(entry.target)


def test_the_core_cannot_narrow_with_names_that_are_no_groups() -> None:
    from nox.node.gate import NodeGate
    from nox.node.tools import default_tools

    gate = NodeGate(default_tools(None), NodeManifest(), None)
    gate.narrow(["notify", "x\x1b[2Jfake"])
    assert gate.narrowed == {"notify"}


def test_nox_node_log_escapes_every_field_even_of_old_entries(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "h")
    audit = node_cli.open_audit(home)
    for evil in EVIL:  # written directly, as an older release or another writer might have
        audit.append(actor=evil, tool=evil, action=evil, target=evil, decision="deny", result=evil)
    lines = node_cli.log_lines(home)
    assert len(lines) == len(EVIL) + 1
    assert all(_clean(line) for line in lines)
    assert r"\x1b[2J" in lines[0]


def test_nox_node_status_escapes_what_the_core_or_a_file_says(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "h")
    identity = _identity().model_copy(update={"name": "Lap\x1b[2Jtop\u202e"})
    home.save_pairing(identity, cert_pem="x", ca_pem="x")
    home.write_status({"state": "connected\r\nState:     removed", "at": "now"})
    assert all(_clean(line) for line in node_cli.status(home))


async def test_a_paired_device_does_not_pair_again(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "h")
    home.ensure()
    home.key_file.write_text("existing key")
    home.save_pairing(_identity(), cert_pem="x", ca_pem="x")
    link = PairLink.make(host="192.168.1.20", port=47843, code="ABCD2345", ca_fingerprint=FP_OTHER)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an already paired device contacts nobody")

    with pytest.raises(EnrollError) as refused:
        await enroll(
            link, home, NodeKeyStore(home.key_file, None), transport=httpx.MockTransport(handler)
        )
    assert refused.value.reason == "already_paired"
    assert home.key_file.read_text() == "existing key"  # the existing key is untouched


def test_the_node_speaks_tls_1_3_only(tmp_path: Path) -> None:
    import ssl

    from nox.lan.tls import ensure_tls, issue_client_certificate
    from tests.unit.node.conftest import ADDRESSES, NAMES, make_csr

    tls = ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)
    keystore = NodeKeyStore(tmp_path / "node.key", None)
    key = keystore.create()
    issued = issue_client_certificate(tmp_path / "tls", make_csr(key), "a" * 32)
    (tmp_path / "node.crt").write_text(issued.cert_pem)
    context = keystore.client_context(ca_file=tls.ca_file, cert_file=tmp_path / "node.crt")
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname
