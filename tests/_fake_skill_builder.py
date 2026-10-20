"""A stand-in for the Claude Code skill builder, for tests and the benchmark (no model, no network).

`FakeSkillBuilder(rounds=["forbidden_api", "good"])` writes the first template in round one, the
second in round two, and so on (the last one repeats). Every template is a complete plugin folder;
each bad one breaks exactly one rule the core checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nox.skills.builder import BuildOutcome, BuildRequest

MANIFEST = """id: {sid}
name: {sid}
version: 0.1.0
api_version: 1
entry: nox_skill_{sid}:create
profiles: []
permissions:
  - tool: {tool}
    risk: {risk}
events:
  emits: [{emits}]
  listens: []
secrets: []
network:
  egress: [{egress}]
"""

SOURCE = '''"""Rolls dice."""

import random
{extra_import}
from pydantic import BaseModel, Field

from nox.plugins.api import PluginApi
from nox.security.model import Risk


class RollInput(BaseModel):
    sides: int = Field(default=6, ge=2, le=100)


def roll(sides: int, rng: random.Random | None = None) -> int:
    {body}
    return (rng or random.Random()).randint(1, sides)


def create(api: PluginApi) -> object:
    async def handler(data: RollInput) -> dict[str, int]:
        return {{"value": roll(data.sides)}}

    api.tools.register("{tool}", RollInput, handler, Risk.{risk_upper}, description="Roll a die.")
    return object()
'''

TEST = """from nox_skill_{sid} import roll
import random


def test_roll_stays_in_range():
    rng = random.Random(1)
    assert all(1 <= roll(6, rng) <= 6 for _ in range(50))


def test_roll_{verdict}():
    assert {assertion}
"""


@dataclass(frozen=True)
class Template:
    tool: str = "{sid}.roll"
    risk: str = "medium"
    egress: str = ""
    emits: str = ""
    extra_import: str = ""
    body: str = "pass"
    assertion: str = "roll(2, random.Random(3)) in (1, 2)"


TEMPLATES: dict[str, Template] = {
    "good": Template(),
    "forbidden_api": Template(extra_import="import subprocess"),
    # Text only: the static checks refuse these before anything of them runs.
    "os_system": Template(extra_import="import os", body="os.system('calc')"),
    "dunder": Template(body="().__class__.__bases__[0].__subclasses__()"),
    "foreign_namespace": Template(tool="weather.roll"),
    "reserved_namespace": Template(tool="{sid}.security_off"),
    "hard_prohibition": Template(tool="{sid}.game_input_send"),
    "low_risk": Template(risk="low"),
    "undeclared_egress": Template(body="url = 'https://evil.example.com/x'"),
    "loopback_egress": Template(egress="127.0.0.1:8123"),
    "foreign_event": Template(emits="security.kill_switch"),
    "failing_tests": Template(assertion="roll(2) == 7"),
    "with_host": Template(egress="api.example.org:443", body="url = 'https://api.example.org/x'"),
}


def write_skill(work_dir: Path, sid: str, name: str) -> None:
    t = TEMPLATES[name]
    tool = t.tool.format(sid=sid)
    package = work_dir / "src" / f"nox_skill_{sid}"
    package.mkdir(parents=True, exist_ok=True)
    (work_dir / "tests").mkdir(exist_ok=True)
    (work_dir / "manifest.yaml").write_text(
        MANIFEST.format(sid=sid, tool=tool, risk=t.risk, egress=t.egress, emits=t.emits),
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        SOURCE.format(
            tool=tool,
            risk_upper=t.risk.upper(),
            extra_import=t.extra_import,
            body=t.body,
        ),
        encoding="utf-8",
    )
    (work_dir / "tests" / f"test_{sid}.py").write_text(
        TEST.format(sid=sid, verdict="ok", assertion=t.assertion), encoding="utf-8"
    )


@dataclass
class FakeSkillBuilder:
    rounds: list[str] = field(default_factory=lambda: ["good"])
    reason: str | None = None
    requests: list[BuildRequest] = field(default_factory=list)

    def unavailable(self) -> str | None:
        return self.reason

    async def build(self, request: BuildRequest) -> BuildOutcome:
        self.requests.append(request)
        name = self.rounds[min(request.round, len(self.rounds)) - 1]
        write_skill(request.work_dir, request.skill_id, name)
        return BuildOutcome(True, f"wrote {name}")
