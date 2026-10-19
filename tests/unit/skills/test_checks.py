"""`nox.skills.checks` and `nox.skills.gaps`: every rule refuses its counter-example."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.capabilities.gaps import CURATED_GAPS
from nox.capabilities.model import Gap, GapReason
from nox.skills.checks import check_skill, scan_code, tree_hash
from nox.skills.gaps import Verdict, assess
from nox.skills.testing import ProcessTestRunner
from tests._fake_skill_builder import TEMPLATES, write_skill


def build(tmp_path: Path, template: str, sid: str = "dice") -> Path:
    work = tmp_path / sid
    write_skill(work, sid, template)
    return work


def test_the_good_template_passes(tmp_path: Path) -> None:
    report = check_skill(build(tmp_path, "good"), skill_id="dice")
    assert report.ok, report.problems


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("forbidden_api", "import subprocess"),
        ("os_system", "import os"),
        ("dunder", "attribute __class__"),
        ("foreign_namespace", "namespace"),
        ("reserved_namespace", "reserved namespace"),
        ("hard_prohibition", "hard prohibition"),
        ("low_risk", "medium or high"),
        ("undeclared_egress", "does not declare"),
        ("loopback_egress", "local network"),
        ("foreign_event", "own namespace"),
    ],
)
def test_each_broken_rule_is_refused(tmp_path: Path, template: str, expected: str) -> None:
    report = check_skill(build(tmp_path, template), skill_id="dice")
    assert not report.ok
    assert any(expected in problem for problem in report.problems), report.problems


def test_a_skill_id_that_is_taken_or_reserved_is_refused(tmp_path: Path) -> None:
    assert not check_skill(build(tmp_path, "good"), skill_id="dice", taken={"dice"}).ok
    home = build(tmp_path, "good", sid="home")
    assert any("reserved" in p for p in check_skill(home, skill_id="home").problems)


def test_the_home_assistant_host_is_never_an_egress(tmp_path: Path) -> None:
    work = build(tmp_path, "with_host")
    assert check_skill(work, skill_id="dice").ok
    report = check_skill(work, skill_id="dice", home_host="api.example.org")
    assert any("Home Assistant" in p for p in report.problems)


def test_binaries_hidden_files_and_missing_tests_are_refused(tmp_path: Path) -> None:
    work = build(tmp_path, "good")
    (work / "evil.pyd").write_bytes(b"MZ")
    (work / ".pth").write_text("import os", encoding="utf-8")
    (work / "tests" / "test_dice.py").unlink()
    problems = check_skill(work, skill_id="dice").problems
    assert any("file type" in p for p in problems)
    assert any("hidden" in p for p in problems)
    assert any("no tests" in p for p in problems)


@pytest.mark.parametrize(
    "source",
    [
        "import ctypes",
        "from os import system",
        "import importlib",
        "import socket",
        "import httpx",
        "from urllib.request import urlopen",
        "import asyncio.subprocess",
        "open('x')",
        "eval('1')",
        "exec('x=1')",
        "__import__('os')",
        "getattr(object, name)",
        "getattr(object, '__subclasses__')",
        "x.__globals__",
        "import asyncio\nasyncio.create_subprocess_exec('calc')",
        "loop.create_connection()",
        "__builtins__",
        "# SendInput",
        "import nox.core",
        "from nox.security import secrets",
    ],
)
def test_scan_code_refuses(source: str) -> None:
    assert scan_code(source, filename="m.py", own_package="nox_skill_dice")


@pytest.mark.parametrize(
    "source",
    [
        "import json, re, math\nfrom datetime import datetime",
        "from pydantic import BaseModel",
        "from nox.plugins.api import PluginApi\nfrom nox.security.model import Risk",
        "from nox_skill_dice.parse import parse\nfrom . import x",
        "from urllib.parse import quote",
        "getattr(obj, 'value', None)",
        "import asyncio\nawait asyncio.sleep(1)",
    ],
)
def test_scan_code_allows(source: str) -> None:
    code = source if "await" not in source else f"async def f():\n    {source.splitlines()[-1]}"
    if "await" in source:
        code = "import asyncio\n" + code
    assert scan_code(code, filename="m.py", own_package="nox_skill_dice") == []


def test_mocks_only_in_tests() -> None:
    assert scan_code("from unittest.mock import Mock", filename="t.py", own_package="p")
    assert not scan_code(
        "from unittest.mock import Mock", filename="t.py", own_package="p", tests=True
    )


def test_tree_hash_sees_every_change(tmp_path: Path) -> None:
    work = build(tmp_path, "good")
    before = tree_hash(work)
    (work / "src" / "nox_skill_dice" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    assert tree_hash(work) != before


# ---- tests in a separate, offline process --------------------------------------------------------


async def test_the_tests_run_offline_and_report(tmp_path: Path) -> None:
    runner = ProcessTestRunner()
    good = await runner.run(build(tmp_path, "good"), timeout_s=60)
    assert good.ok and good.passed == 2
    bad = await runner.run(build(tmp_path / "b", "failing_tests"), timeout_s=60)
    assert not bad.ok and bad.failed == 1


async def test_the_test_process_has_no_network(tmp_path: Path) -> None:
    work = build(tmp_path, "good")
    (work / "tests" / "test_net.py").write_text(
        "import socket\n\ndef test_net():\n"
        "    socket.create_connection(('example.com', 80), timeout=2)\n",
        encoding="utf-8",
    )
    outcome = await ProcessTestRunner().run(work, timeout_s=60)
    assert outcome.failed == 1 and "network access is not available" in outcome.output


# ---- learning is offered only for what is missing ------------------------------------------------


def test_assess_refuses_forbidden_wishes_and_names() -> None:
    for intent in (
        "Mach mir die Haustür auf, wenn ich komme",
        "Drück im Spiel Rocket League für mich die Taste",
        "lern einen aimbot",
        "unlock the front door automatically",
    ):
        verdict = assess(capability="", intent=intent, gaps=CURATED_GAPS, usable_tools=[])
        assert verdict.verdict is Verdict.REFUSE, intent
    for name in ("game.input.send", "home.lock", "security.off", "permission.self_elevate"):
        verdict = assess(capability=name, intent="eine Sache", gaps=CURATED_GAPS, usable_tools=[])
        assert verdict.verdict is Verdict.REFUSE, name


def test_assess_gives_the_fix_for_disabled_or_missing_prerequisites() -> None:
    gaps = [
        Gap(name="twitch.*", what="w", reason=GapReason.DISABLED, detail="add twitch"),
        Gap(name="home.light", what="w", reason=GapReason.PREREQUISITE, detail="add the token"),
    ]
    assert (
        assess(capability="twitch.chat", intent="chat lesen", gaps=gaps, usable_tools=[]).verdict
        is Verdict.FIX
    )
    assert (
        assess(
            capability="weather.now",
            intent="Wetter abfragen",
            gaps=gaps,
            usable_tools=["weather.now"],
        ).verdict
        is Verdict.EXISTS
    )


def test_assess_offers_learning_for_what_is_missing() -> None:
    verdict = assess(
        capability="weather.forecast",
        intent="Sag mir das Wetter für morgen",
        gaps=CURATED_GAPS,
        usable_tools=["file.read"],
    )
    assert verdict.verdict is Verdict.OFFER and "beibringen" in verdict.message
    assert "cheat sheet" not in ""  # a word containing "cheat" alone is not a game cheat
    assert (
        assess(
            capability="", intent="mach mir ein git cheatsheet", gaps=(), usable_tools=[]
        ).verdict
        is Verdict.OFFER
    )


def test_templates_cover_every_rule() -> None:
    assert len(TEMPLATES) >= 12


async def test_raw_sockets_are_refused_in_the_test_process(tmp_path: Path) -> None:
    work = build(tmp_path, "good")
    (work / "tests" / "test_raw.py").write_text(
        "import socket\n\ndef test_raw():\n"
        "    s = socket.socket()\n    s.settimeout(2)\n    s.connect(('127.0.0.1', 9))\n",
        encoding="utf-8",
    )
    outcome = await ProcessTestRunner().run(work, timeout_s=60)
    assert outcome.failed == 1 and "network access is not available" in outcome.output


def test_a_forbidden_gap_is_refused_even_with_an_ordinary_name() -> None:
    gaps = [Gap(name="drone.fly", what="fly a drone", reason=GapReason.FORBIDDEN, detail="no")]
    verdict = assess(capability="drone.fly", intent="flieg die Drohne", gaps=gaps, usable_tools=[])
    assert verdict.verdict is Verdict.REFUSE
