"""What the person is shown before a call on a node runs: the command and the write named
plainly, one token shown as one token, invisible characters made visible, a call too long
to show refused instead of cut, and the same text on the core and in the device's dialog."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.node.device.gate import NodeGate
from nox.node.device.hands.catalogue import hand_tools
from nox.node.device.hands.node_hands import NodeHands
from nox.node.hub.install.routes import shell_target
from nox.node.hub.registry import NodeRegistry
from nox.node.hub.router import routed_spec
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from nox.security.model import Risk
from tests.unit.node.support import FakeRegistry, registry_entry, signed
from tests.unit.node.support_sessions import LAPTOP

# ---- the core's side -----------------------------------------------------------------------------


def test_the_core_routes_the_hands_and_names_a_node_call_plainly() -> None:
    accepted = NodeRegistry._accept_tools(
        [{"name": n, "_nox": {"risk": "read"}} for n in ("shell.run", "file.write", "rm.rf")]
    )
    assert set(accepted) == {"shell.run", "file.write"}
    target = shell_target({"argv": ["df", "-h"], "egress": ["deb.example.invalid"]})
    assert target == 'argv ["df", "-h"] egress ["deb.example.invalid"]'
    assert shell_target({"command": "ls", "egress": []}) == 'command "ls" egress [none]'


def test_a_token_with_spaces_or_quotes_is_shown_as_one_token() -> None:
    # `["rm", "-rf /"]` and `["rm", "-rf", "/"]` must never look alike.
    assert shell_target({"argv": ["rm", "-rf /"]}) != shell_target({"argv": ["rm", "-rf", "/"]})
    assert '"say \\"hi\\""' in shell_target({"argv": ["echo", 'say "hi"']})


def test_invisible_bidi_control_and_space_runs_are_made_visible() -> None:
    sneaky = shell_target({"command": "echo safe\u202e\u200b\x1b[2J" + " " * 40 + "&& curl x"})
    assert "\u202e" not in sneaky and "\u200b" not in sneaky and "\x1b" not in sneaky
    assert "\\u202e" in sneaky and "\\u200b" in sneaky and "\\u001b" in sneaky
    assert "[40 spaces]" in sneaky and "&& curl x" in sneaky
    assert "\\n" in shell_target({"command": "echo a\nrm -rf ~"})


def test_a_padded_call_is_refused_never_shortened() -> None:
    from nox.node.hub.install.routes import MAX_TARGET_CHARS
    from nox.node.hub.router import CannotShow

    padded = {"command": "echo " + "a" * MAX_TARGET_CHARS + " ; rm -rf ~", "egress": []}
    with pytest.raises(CannotShow):
        shell_target(padded)
    hosts = [f"h{i}.example.invalid" for i in range(8)]
    shown = shell_target({"argv": ["curl", "x"], "egress": hosts})
    assert all(h in shown for h in hosts)  # the whole egress list, always


def _node_shell_spec(confirmer: bool = True) -> Any:
    from nox.node.device.hands.args import ShellArgs

    laptop = registry_entry("a" * 32, "Laptop", tools={"shell.run": "high"})
    laptop.confirmer = confirmer
    registry = FakeRegistry([laptop])
    spec = routed_spec(
        name="shell.run",
        description="t",
        risk=Risk.HIGH,
        side_effects=True,
        registry=registry,  # type: ignore[arg-type]
        input_model=ShellArgs,
        node_target=shell_target,
    )
    return spec, registry


def test_a_node_shell_call_is_high_and_names_the_device() -> None:
    spec, _registry = _node_shell_spec()
    payload = spec.input_model.model_validate({"argv": ["whoami"], "device": "Laptop"}).model_dump()
    scope = spec.scope(payload)
    assert scope.risk is Risk.HIGH and scope.device == "a" * 32 and scope.confirm_on_device
    assert scope.target.endswith(': argv ["whoami"] egress [none]')


async def test_a_call_too_long_to_show_is_refused_before_anyone_is_asked() -> None:
    from nox.node.hub.install.routes import MAX_TARGET_CHARS

    spec, registry = _node_shell_spec(confirmer=False)
    command = "echo " + " x" * MAX_TARGET_CHARS + " ; curl evil.example.invalid"
    payload = spec.input_model.model_validate({"command": command, "device": "Laptop"}).model_dump()
    scope = spec.scope(payload)
    assert scope.risk is Risk.READ and scope.target == "device:none"  # nothing to confirm
    answer = await spec.handler(payload)
    assert answer["error"] == "cannot_show_in_full"
    assert registry.calls == []  # never sent to the node


async def test_replacing_or_extending_a_file_is_high_and_shown_with_size_and_preview(
    tmp_path: Path,
) -> None:
    from nox.node.device.agent import confirmation_text
    from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    groups = {g: g == "files" for g in GROUPS}
    world_tools = hand_tools(
        NodeHands(folders=lambda: [], protected=(tmp_path / "n",), sandbox=lambda: None),
        platform="linux",
    )
    gate = NodeGate(world_tools, NodeManifest(groups=groups), None, receipts=verifier)
    args = {"path": "a.txt", "text": "x" * 500, "append": False, "overwrite": True}
    low = signed(signer, "file.write", args, risk="medium", device_id="d" * 32, core_id="c" * 64)
    assert gate.check("file.write", args, meta=NoxCallMeta(**low)).reason == "risk_understated"
    high = signed(signer, "file.write", args, risk="high", device_id="d" * 32, core_id="c" * 64)
    decision = gate.check("file.write", args, meta=NoxCallMeta(**high))
    assert decision.allowed and decision.needs_local_confirmation
    tool = world_tools["file.write"]
    assert tool.shown is not None and decision.args is not None
    text = confirmation_text("file.write", tool.shown(decision.args))
    assert text is not None and '"overwrite"' in text and "500" in text
    assert "content shortened - 100 characters not shown" in text


def test_the_device_dialog_and_the_core_target_render_alike() -> None:
    from nox.node.device.agent import confirmation_text
    from nox.node.hub.install.routes import write_target
    from nox.node.wire.protocol import render

    argv = ["echo", "safe‮", "a" + " " * 9 + "b", "Ж"]
    dialog = confirmation_text("shell.run", {"argv": argv})
    assert dialog is not None and render(argv) in dialog
    assert render(argv) in shell_target({"argv": argv})
    assert "‮" not in dialog and "Ж" not in dialog and "[9 spaces]" in dialog
    shown = {"path": "a.txt", "text": "Grüße", "overwrite": True}
    assert render("Grüße") in write_target(shown) and render("Grüße") == '"Grüße"'


def test_free_text_is_not_a_path_but_a_path_still_is(tmp_path: Path) -> None:
    from nox.node.device.hands.args import ShellArgs as _Shell  # noqa: F401 - the model the gate validates

    gate = NodeGate(
        hand_tools(
            NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None),
            platform="linux",
        ),
        NodeManifest(groups={g: g in ("files", "shell") for g in GROUPS}),
        None,
    )
    for args in ({"argv": ["git", "log", "HEAD~1"], "egress": []}, {"command": "cd ~/x && ls"}):
        assert gate.check("shell.run", args).reason != "path_alias_refused"
    assert gate.check("file.read", {"path": "../x"}).reason == "path_alias_refused"
    assert gate.check("file.read", {"path": "~/x"}).reason == "path_alias_refused"


def test_a_long_write_shows_head_tail_hash_and_what_is_not_shown() -> None:
    import hashlib

    from nox.node.device.hands.args import PREVIEW_CHARS, write_summary

    text = "A" * PREVIEW_CHARS + "M" * 50 + "\n" + "Z" * PREVIEW_CHARS
    shown = write_summary("a.txt", text, append=False, overwrite=True)
    assert shown["content_start"] == "A" * PREVIEW_CHARS
    assert shown["content_end"] == "Z" * PREVIEW_CHARS
    assert shown["not_shown"] == "content shortened - 51 characters not shown"
    assert shown["sha256"] == hashlib.sha256(text.encode()).hexdigest()[:16]
    assert shown["lines"] == 2 and "content" not in shown
    short = write_summary("a.txt", "kurz\n", append=False, overwrite=False)
    assert short["content"] == "kurz\n" and "not_shown" not in short


def test_the_write_is_rendered_alike_on_the_core_and_on_the_device() -> None:
    from nox.node.device.agent import confirmation_text
    from nox.node.device.hands.args import PREVIEW_CHARS, write_summary
    from nox.node.hub.install.routes import write_target
    from nox.node.wire.protocol import render

    text = "Anfang " + "x" * (2 * PREVIEW_CHARS) + " Ende \u202e"
    payload = {"path": "notes/a.txt", "text": text, "append": True, "overwrite": False}
    shown = write_summary(payload["path"], text, append=True, overwrite=False)
    target = write_target(payload)
    dialog = confirmation_text("file.write", shown)
    assert dialog is not None
    for key, value in shown.items():
        assert f"{key} {render(value)}" in target
        assert f"{key}: {render(value)}" in dialog
    assert "\u202e" not in dialog and "\u202e" not in target


def test_a_permission_question_names_the_device_in_structured_fields() -> None:
    from nox.security.model import PermissionRequest, Risk

    def asked(**fields: Any) -> Any:
        from nox.security.permissions import DefaultPermissionEngine

        request = PermissionRequest(
            agent="companion", tool="file", action="write", mode="companion", risk=Risk.HIGH,
            target="Laptop · aaaa: path \"C:/Notizen/a.txt\"", **fields,
        )  # fmt: skip
        pending = SimpleNamespace(request=request, created_at=__import__("datetime").datetime.now())
        engine = SimpleNamespace(
            _confirm_timeout_s=60,
            _may_remember=lambda _p: False,
            _session_grant_ttl=None,
            _satellite_of=lambda _c: "",
        )
        return DefaultPermissionEngine._requested(engine, "g", pending)  # type: ignore[arg-type]

    on_node = asked(device=LAPTOP, device_name="Laptop · aaaa")
    assert on_node.device_id == LAPTOP and on_node.device_name == "Laptop · aaaa"
    assert on_node.target.startswith("Laptop · aaaa:")  # the target text stays as it was
    here = asked(device_name="Wohnzimmer-Pi")
    assert here.device_id == "core" and here.device_name == "Wohnzimmer-Pi"
    alone = asked()
    assert alone.device_id == "" and alone.device_name == ""
    assert (
        "device_name"
        not in PermissionRequest(
            agent="a", tool="t", action="x", mode="m", risk=Risk.LOW, device_name="x"
        ).model_dump()
    )
