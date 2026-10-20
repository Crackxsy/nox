"""scripts/pii_guard.py finds fingerprinted private terms in tracked files and paths - and the
repository itself holds none of the terms listed in docs/pii_guard_hashes.txt.

Every term and key below is synthetic; the real list is only exercised by the last test, and only
where the real key is available (PII_GUARD_KEY), which CI's release-hygiene job checks anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "pii_guard.py"
KEY = "test-key-not-a-secret"
TERMS = ("zorblatt", "quenmira", "vexalor.quint42")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pii_guard_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(term: str, key: str = KEY) -> str:
    return hmac.new(key.encode("utf-8"), term.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture
def hashes(tmp_path: Path) -> Path:
    path = tmp_path / "hashes.txt"
    lines = ["# test list", *(_digest(t) for t in TERMS)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _prints(guard: ModuleType, hashes: Path, key: str = KEY) -> Any:
    return guard.Fingerprints(key.encode("utf-8"), guard.load_digests(hashes))


def _found(guard: ModuleType, hashes: Path, text: str) -> list[int]:
    return [f.line for f in guard.scan_text("notes.txt", text, _prints(guard, hashes), {})]


@pytest.mark.parametrize(
    "text",
    [
        "Das Wetter in Zorblatt morgen",
        "ZORBLATT",
        "city = 'zorblatt'",
        "C:\\Users\\Zorblatt\\AppData",
        "/home/zorblatt/.config",
        "weather_zorblatt_today",
        "WeatherZorblattToday",
        "Ｚｏｒｂｌａｔｔ",  # full-width letters are normalised, not a way around the guard
        "mail vexalor.quint42@example.invalid",
        "Vexalor.Quint42",
    ],
)
def test_a_listed_term_is_found_in_any_shape(guard: ModuleType, hashes: Path, text: str) -> None:
    assert _found(guard, hashes, text) == [1]


@pytest.mark.parametrize(
    "text",
    [
        "zorblatter",  # whole tokens only, not substrings
        "unzorblatt",
        "zorb latt",
        "vexalor quint42",  # the compound term needs its dot
        "vexalor.quint4",
        "nothing private here",
    ],
)
def test_other_text_is_not_a_finding(guard: ModuleType, hashes: Path, text: str) -> None:
    assert _found(guard, hashes, text) == []


def test_the_list_is_worthless_without_the_key(guard: ModuleType, hashes: Path) -> None:
    other = _prints(guard, hashes, key="some-other-key")
    assert guard.scan_text("notes.txt", "zorblatt quenmira", other, {}) == []
    assert _digest("zorblatt") != hashlib.sha256(b"zorblatt").hexdigest()


def test_a_finding_names_place_and_fingerprint_never_the_term(
    guard: ModuleType, hashes: Path
) -> None:
    (finding,) = guard.scan_text("a.md", "one\ntwo quenmira", _prints(guard, hashes), {})
    assert (finding.line, finding.column) == (2, 5)
    rendered = finding.render()
    assert rendered.startswith("a.md:2:5: ")
    assert "quenmira" not in rendered.casefold()
    assert _digest("quenmira")[:12] in rendered


def test_paths_are_scanned(guard: ModuleType, hashes: Path) -> None:
    prints = _prints(guard, hashes)
    (finding,) = guard.scan_path("docs/Zorblatt/readme.md", prints, {})
    assert finding.line == 0
    assert "(path)" in finding.render()
    assert guard.scan_path("docs/other/readme.md", prints, {}) == []


def test_add_term_reads_the_term_from_stdin_and_never_writes_it(
    guard: ModuleType, hashes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("Plimwex\n"))
    assert guard.main(["--add-term", "--hashes", str(hashes)], key_source=lambda: KEY) == 0
    text = hashes.read_text(encoding="utf-8")
    assert "plimwex" not in text.casefold()
    assert _digest("plimwex") in text
    assert _found(guard, hashes, "met Plimwex today") == [1]
    assert guard.add_term("PLIMWEX", KEY, hashes) is False  # already listed, not duplicated
    assert hashes.read_text(encoding="utf-8").count(_digest("plimwex")) == 1


def test_add_term_without_a_key_reads_the_key_from_stdin_first(
    guard: ModuleType, hashes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{KEY}\nGrelnox\n"))
    assert guard.main(["--add-term", "--hashes", str(hashes)], key_source=lambda: None) == 0
    assert _digest("grelnox") in hashes.read_text(encoding="utf-8")


@pytest.mark.parametrize("term", ["two words", "someone@example.invalid", "", "a/b"])
def test_add_term_refuses_what_could_never_match(
    guard: ModuleType, hashes: Path, term: str
) -> None:
    before = hashes.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="single token"):
        guard.add_term(term, KEY, hashes)
    assert hashes.read_text(encoding="utf-8") == before


def test_a_malformed_list_is_rejected(guard: ModuleType, tmp_path: Path) -> None:
    broken = tmp_path / "broken.txt"
    broken.write_text("zorblatt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected line"):
        guard.load_digests(broken)


def test_without_a_key_the_guard_skips_unless_a_key_is_required(
    guard: ModuleType, hashes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--check", "--hashes", str(hashes)]
    assert guard.main(args, key_source=lambda: None) == 0
    assert "skipped (no key" in capsys.readouterr().out
    assert guard.main([*args, "--require-key"], key_source=lambda: None) == 1


def test_the_key_comes_from_the_environment_and_never_from_a_keyring_in_ci(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PII_GUARD_KEY", "  from-env  ")
    assert guard.find_key() == "from-env"
    monkeypatch.delenv("PII_GUARD_KEY")
    monkeypatch.setenv("CI", "true")
    assert guard.key_from_keyring() is None


@needs_git
def test_check_fails_on_a_tracked_file_and_skips_binaries(
    guard: ModuleType, hashes: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "clean.txt").write_text("hello\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\0zorblatt\0")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    args = ["--check", "--require-key", "--root", str(repo), "--hashes", str(hashes)]
    assert guard.main(args, key_source=lambda: KEY) == 0

    (repo / "leak.py").write_text("CITY = 'Zorblatt'\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("zorblatt\n", encoding="utf-8")
    subprocess.run(["git", "add", "leak.py"], cwd=repo, check=True)
    findings = guard.scan_repo(repo, _prints(guard, hashes))
    assert [(f.path, f.line) for f in findings] == [("leak.py", 1)]
    assert guard.main(args, key_source=lambda: KEY) == 1


@needs_git
@pytest.mark.skipif(not os.environ.get("PII_GUARD_KEY"), reason="needs the real PII_GUARD_KEY")
def test_the_repository_holds_none_of_the_listed_terms(guard: ModuleType) -> None:
    key = os.environ["PII_GUARD_KEY"].strip().encode("utf-8")
    prints = guard.Fingerprints(key, guard.load_digests())
    assert [f.render() for f in guard.scan_repo(REPO_ROOT, prints)] == []
