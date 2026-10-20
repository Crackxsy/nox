"""`nox.skills.scan`: the reference shapes the first scan let through are refused now.

Only the shape of each reference matters to a static scan, so the cases name the attribute chain
and nothing more - no arguments, nothing that would do anything if it were run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.skills.scan import scan_code

OWN = "nox_skill_dice"


def problems(source: str, *, tests: bool = False) -> list[str]:
    return scan_code(source, filename="m.py", own_package=OWN, tests=tests)


#: Each shape reached a process or a file through the first version of the scan.
REPORTED_BYPASSES = [
    "import asyncio\nf = asyncio.subprocess.subprocess.Popen",
    "import random\nf = random._os.spawnve",
    "import random\nf = random._os.open",
    "import random\nf = random._os.read",
]

RELATED_SHAPES = [
    "from asyncio import subprocess",
    "from random import _os",
    "import random\nf = random._os._exit",
    "import typing\nm = typing.sys",
    "import uuid\nm = uuid.os",
    "import calendar\nm = calendar.sys",
    "import json\nm = json.decoder",
    "import random\nr = random\nm = r.Random",  # a module may not be aliased or passed on
    "import random\nhelper(random)",
    "x = obj.__class__",
    "x = obj._private",
    "def _helper():\n    pass",
    "f(_x=1)",
    "f = getattr",  # a reference, not only a call
    "f = open",
    "f = exit",
    "f = eval",
    "f = vars",
    "e = SystemExit",
    "e = BaseException",
    "from operator import attrgetter",
    "import operator\nf = operator.methodcaller",
    "import string\nf = string.Formatter",
    "import typing\nf = typing.get_type_hints",
    "from pydantic import ImportString",
    "import pydantic.v1",
    "from pydantic.v1 import BaseModel",
    "from nox.plugins.api import load_manifest",
    "from nox.plugins.api import httpx",
    "import nox.plugins.api\nm = nox.plugins.manager",
    "s = template.format(x)",  # a format string built at runtime
    "s = '{0._secret}'.format(x)",
    "s = '__builtins__'",
    "x: 'list[helper()]' = []",  # pydantic and typing evaluate string annotations
    "frame = gen.gi_frame",
    "g = frame.f_globals",
    "m = thing.modules",
    "from operator import *",
    "loop.subprocess_exec",
]


@pytest.mark.parametrize("source", REPORTED_BYPASSES + RELATED_SHAPES)
def test_each_shape_is_refused(source: str) -> None:
    assert problems(source), source


def test_mock_patch_is_refused_in_tests() -> None:
    assert problems("from unittest.mock import patch", tests=True)
    assert problems("from unittest import mock\np = mock.patch", tests=True)
    assert problems("from unittest.mock import Mock", tests=True) == []


@pytest.mark.parametrize(
    "source",
    [
        "import collections.abc\nT = collections.abc.Mapping",
        "from collections import abc\nT = abc.Mapping",
        "import email.utils\nf = email.utils.parseaddr",
        "import datetime\nnow = datetime.datetime.now()",
        "import random\nr = random.Random(1)",
        "from pydantic import BaseModel, Field",
        "from nox.plugins.api import PluginApi\nfrom nox.security.model import Risk",
        "import nox.plugins.api\nT = nox.plugins.api.PluginApi",
        "s = '{0} and {name}'.format(1, name='x')",
        "x: 'list[int]' = []",
        "class Item:\n    def __init__(self) -> None:\n        super().__init__()",
        "for _ in range(3):\n    pass",
        "text = 'call me (later)'",
    ],
)
def test_ordinary_code_passes(source: str) -> None:
    assert problems(source) == [], source


@pytest.mark.parametrize("package", ["json", "nox", "os", "pydantic", "random", "dice", "x"])
def test_the_own_package_cannot_shadow_a_real_module(package: str) -> None:
    # A skill's own package is trusted by the scan: `import json` from a skill named `json`
    # would otherwise pass as "own code".
    assert scan_code("import json", filename="m.py", own_package=package)


def test_the_entry_package_is_nox_skill_id(tmp_path: Path) -> None:
    from nox.skills.checks import check_skill
    from tests._fake_skill_builder import write_skill

    work = tmp_path / "dice"
    write_skill(work, "dice", "good")
    manifest = work / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("nox_skill_dice:create", "json:create"),
        encoding="utf-8",
    )
    assert any("nox_skill_dice" in p for p in check_skill(work, skill_id="dice").problems)


def test_match_statements_are_refused() -> None:
    assert problems("match value:\n    case Point(x=0):\n        pass")


@pytest.mark.parametrize(
    "source",
    [
        "x: 'random._os' = None",  # a string annotation that reaches a private attribute
        "x: 'typing.sys' = None",  # ... or a module the scan refuses
        "import random\nx: 'random._os.environ' = None",
        "ref = 'gen.gi_frame.f_globals'",
    ],
)
def test_strings_that_parse_as_code_are_checked_like_code(source: str) -> None:
    assert problems(source), source


def test_plain_words_in_strings_stay_data() -> None:
    assert problems("state = 'open'\nmode = 'exit'\nlabel = 'Hello world.'") == []
