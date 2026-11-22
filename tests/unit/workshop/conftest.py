"""Fixtures for the stream test workshop's tests: the two fakes, and the plugin sources on
`sys.path` (the worker puts them there itself at spawn time; these tests import the plugins
in-process)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.workshop.fake_obs import FakeObs
from tests.workshop.fake_twitch import FakeTwitchIrc

_PLUGINS = Path(__file__).resolve().parents[3] / "plugins"
for _plugin in ("obs", "twitch"):
    _src = str(_PLUGINS / _plugin / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

#: The scenes the OBS plugin's manifest allows switching between (`scene_set`), plus nothing else.
PLUGIN_SCENES = ("Start", "Live", "Pause", "Technical Problems")


@pytest.fixture
async def obs() -> AsyncIterator[FakeObs]:
    """A fake OBS with the plugin's scenes and no password."""
    async with FakeObs(scenes=PLUGIN_SCENES) as server:
        yield server


@pytest.fixture
async def obs_with_password() -> AsyncIterator[FakeObs]:
    async with FakeObs(password="correct-horse", scenes=PLUGIN_SCENES) as server:
        yield server


@pytest.fixture
async def irc() -> AsyncIterator[FakeTwitchIrc]:
    """A fake Twitch chat server that accepts any well-formed login."""
    async with FakeTwitchIrc() as server:
        yield server
