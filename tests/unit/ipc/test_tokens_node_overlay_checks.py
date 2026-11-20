"""`TokenStore.add_node_overlay_check`: who may hold a node overlay credential over the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.hooks import DuplicateHookError
from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.ipc.tokens import NODE_OVERLAY_PREFIX, TokenStore

PI = "a" * 32


def _store(tmp_path: Path) -> tuple[TokenStore, str]:
    overlay = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    _credential, token = overlay.issue(f"{NODE_OVERLAY_PREFIX}{PI}")
    return TokenStore(overlay=overlay), token


def _accepted(tokens: TokenStore, token: str) -> bool:
    return tokens.authenticate(token, "overlay", "overlay:x", via_lan=True).ok


def test_with_no_check_registered_no_node_overlay_comes_in_over_the_network(
    tmp_path: Path,
) -> None:
    tokens, token = _store(tmp_path)
    assert not _accepted(tokens, token)


def test_every_registered_check_has_to_agree(tmp_path: Path) -> None:
    tokens, token = _store(tmp_path)
    answers = {"nodes": True, "second": True}
    tokens.add_node_overlay_check("nodes", lambda device: answers["nodes"] and device == PI)
    assert _accepted(tokens, token)
    tokens.add_node_overlay_check("second", lambda _device: answers["second"])
    assert _accepted(tokens, token)  # a second check does not replace the first ...
    answers["second"] = False
    assert not _accepted(tokens, token)  # ... and can only narrow what the first allows
    answers.update(second=True, nodes=False)
    assert not _accepted(tokens, token)


def test_the_order_checks_are_registered_in_does_not_change_the_answer(tmp_path: Path) -> None:
    for first, second in ((True, False), (False, True)):
        tokens, token = _store(tmp_path)
        tokens.add_node_overlay_check("a", lambda _device, answer=first: answer)  # type: ignore[misc]
        tokens.add_node_overlay_check("b", lambda _device, answer=second: answer)  # type: ignore[misc]
        assert not _accepted(tokens, token)


def test_a_failing_check_means_no(tmp_path: Path) -> None:
    tokens, token = _store(tmp_path)
    tokens.add_node_overlay_check("nodes", lambda _device: True)
    tokens.add_node_overlay_check("broken", lambda _device: 1 / 0)  # type: ignore[arg-type,return-value]
    assert not _accepted(tokens, token)


def test_a_second_check_under_the_same_name_is_refused(tmp_path: Path) -> None:
    tokens, token = _store(tmp_path)
    tokens.add_node_overlay_check("nodes", lambda _device: True)
    with pytest.raises(DuplicateHookError):
        tokens.add_node_overlay_check("nodes", lambda _device: False)
    assert _accepted(tokens, token)
