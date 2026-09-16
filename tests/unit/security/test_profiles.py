"""Profiles: the six repo profiles load, restrictions match the Security Model, hard list wins."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.security.model import Decision, Profile, ProfileRule
from nox.security.profiles import (
    PROFILE_IDS,
    InMemoryProfileProvider,
    ProfileError,
    YamlProfileProvider,
    load_profile,
    parse_profile,
)
from nox.security.prohibitions import HardProhibitionRemovedError

from .conftest import PROFILES_DIR


def test_all_profiles_load(profiles: YamlProfileProvider) -> None:
    assert set(profiles.available()) == set(PROFILE_IDS)
    for pid in PROFILE_IDS:
        profile = profiles.get(pid)
        assert profile.id == pid
        assert profile.rules, pid
        assert all(isinstance(r, ProfileRule) for r in profile.rules)


def test_work_profile_is_local_only_without_memory(profiles: YamlProfileProvider) -> None:
    work = profiles.get("work")
    assert work.cloud_allowed is False
    assert work.memory_writes_allowed is False
    assert work.screenshots_to_cloud is False
    assert all(
        ":" in e and e.split(":")[0] in ("127.0.0.1", "localhost") for e in work.egress_allowlist
    )


def test_offline_profile_has_no_egress_but_keeps_ollama_on_loopback(
    profiles: YamlProfileProvider,
) -> None:
    """OP-7 C: no host is reachable; the allow-listed local chat service stays (A165/D89)."""
    offline = profiles.get("offline")
    assert offline.cloud_allowed is False
    assert offline.egress_allowlist == []
    assert offline.loopback_allowlist == ["127.0.0.1:11434"]


def test_stream_profile_adds_the_obs_websocket_to_the_loopback_allowlist(
    profiles: YamlProfileProvider,
) -> None:
    assert profiles.get("stream").loopback_allowlist == ["127.0.0.1:11434", "127.0.0.1:4455"]


def test_stream_profile_confirms_scene_switch_and_keeps_screenshots_local(
    profiles: YamlProfileProvider,
) -> None:
    stream = profiles.get("stream")
    assert stream.screenshots_to_cloud is False
    switch = next(r for r in stream.rules if r.action == "scene.switch")
    assert switch.decision is Decision.CONFIRM
    delete = next(r for r in stream.rules if r.action == "scene.delete")
    assert delete.decision is Decision.DENY


def test_coding_profile_limits_filesystem_roots(profiles: YamlProfileProvider) -> None:
    assert profiles.get("coding").filesystem_roots


def test_research_profile_allows_cloud_and_web(profiles: YamlProfileProvider) -> None:
    research = profiles.get("research")
    assert research.cloud_allowed is True
    assert any(r.tool.startswith("web") and r.decision is Decision.ALLOW for r in research.rules)


def test_no_profile_can_lift_a_hard_prohibition(tmp_path: Path) -> None:
    bad = tmp_path / "evil.yaml"
    bad.write_text(
        "permissions:\n  id: evil\n  rules:\n"
        "    - {id: evil.rl, tool: 'game.input', action: 'send', decision: allow}\n",
        encoding="utf-8",
    )
    with pytest.raises(HardProhibitionRemovedError, match="game.input.send"):
        load_profile(bad)
    glob_bad = Profile(
        id="g", rules=[ProfileRule(id="g.1", tool="stream.*", decision=Decision.CONFIRM)]
    )
    with pytest.raises(HardProhibitionRemovedError):
        InMemoryProfileProvider([glob_bad])
    # a deny rule on a hard entry is redundant but harmless
    InMemoryProfileProvider(
        [
            Profile(
                id="ok",
                rules=[
                    ProfileRule(id="ok.1", tool="stream", action="stop", decision=Decision.DENY)
                ],
            )
        ]
    )


def test_both_file_layouts_parse_and_ids_must_match(tmp_path: Path) -> None:
    literal = parse_profile({"id": "x", "rules": []})
    nested = parse_profile({"permissions": {"rules": []}}, expected_id="x")
    assert literal.id == nested.id == "x"
    with pytest.raises(ProfileError, match="does not match"):
        parse_profile({"id": "y", "rules": []}, expected_id="x")
    with pytest.raises(ProfileError):
        parse_profile({"permissions": {"rules": "nope"}}, expected_id="x")
    dup = Profile(
        id="d",
        rules=[
            ProfileRule(id="same", decision=Decision.ALLOW),
            ProfileRule(id="same", decision=Decision.DENY),
        ],
    )
    with pytest.raises(ProfileError, match="duplicate"):
        InMemoryProfileProvider([dup])


def test_unknown_profile_raises_key_error(profiles: YamlProfileProvider) -> None:
    with pytest.raises(KeyError):
        profiles.get("does-not-exist")
    assert (PROFILES_DIR / "companion.yaml").is_file()
