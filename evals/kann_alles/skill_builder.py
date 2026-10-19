"""The skill builder of the benchmark world: writes a known, small plugin instead of asking a model.

Learning itself is measured elsewhere (it needs Claude Code and minutes per skill). What a task
measures here is the path around it: Nox notices the gap, offers to learn, calls `skill.learn`, the
core checks and tests what was written, and the skill becomes usable once activated. The plugin
written is a dice roller whatever the wish was - the checks and tests are the real ones.
"""

from __future__ import annotations

from pathlib import Path

from nox.skills.builder import BuildOutcome, BuildRequest

_MANIFEST = """id: {sid}
name: {sid}
version: 0.1.0
api_version: 1
entry: nox_skill_{sid}:create
profiles: []
permissions:
  - tool: {sid}.roll
    risk: medium
events: {{emits: [], listens: []}}
secrets: []
network: {{egress: []}}
"""

_SOURCE = '''"""Rolls dice."""

import random

from pydantic import BaseModel, Field

from nox.plugins.api import PluginApi
from nox.security.model import Risk


class RollInput(BaseModel):
    sides: int = Field(default=6, ge=2, le=100)


def roll(sides: int, rng: random.Random | None = None) -> int:
    return (rng or random.Random()).randint(1, sides)


def create(api: PluginApi) -> object:
    async def handler(data: RollInput) -> dict[str, int]:
        return {{"value": roll(data.sides)}}

    api.tools.register("{sid}.roll", RollInput, handler, Risk.MEDIUM, description="Roll a die.")
    return object()
'''

_TEST = """import random

from nox_skill_{sid} import roll


def test_roll_stays_in_range():
    rng = random.Random(1)
    assert all(1 <= roll(6, rng) <= 6 for _ in range(50))
"""


class WorldSkillBuilder:
    """`nox.skills.builder.SkillBuilder` for the benchmark world."""

    def __init__(self) -> None:
        self.requests: list[BuildRequest] = []

    def unavailable(self) -> str | None:
        return None

    async def build(self, request: BuildRequest) -> BuildOutcome:
        self.requests.append(request)
        sid = request.skill_id
        root: Path = request.work_dir
        package = root / "src" / f"nox_skill_{sid}"
        package.mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir(exist_ok=True)
        (root / "manifest.yaml").write_text(_MANIFEST.format(sid=sid), encoding="utf-8")
        (package / "__init__.py").write_text(_SOURCE.format(sid=sid), encoding="utf-8")
        (root / "tests" / f"test_{sid}.py").write_text(_TEST.format(sid=sid), encoding="utf-8")
        return BuildOutcome(True, "wrote the benchmark's dice skill")
