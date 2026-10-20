"""scripts/secrets_scan.py scans the checked-out branch, not every ref.

Built on a throwaway git repository: a secret added on the branch under test is found - also once
it has been deleted again - while one that only lives on some other branch is not.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "secrets_scan.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# Assembled at runtime so this file itself is not a finding.
SECRET = "sk-" + "live" + "-" + "Zq8vT2mK9" + "wL4xR7nP3cY6bJ1"
POLICY = {"exclude_paths": [], "accepted_findings": []}


@pytest.fixture(scope="module")
def scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("secrets_scan_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main <- feature (secret added, then removed again); other (a secret of its own)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _commit(root, "readme.txt", "hello\n", "base")
    _git(root, "checkout", "-q", "-b", "other")
    _commit(root, "other.txt", f"key = '{SECRET}OTHER'\n", "secret on another branch")
    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "feature")
    _commit(root, "config.txt", f"key = '{SECRET}'\n", "oops")
    _commit(root, "config.txt", "key = ''\n", "remove it again")
    return root


def _excerpts(findings: list) -> list[str]:  # type: ignore[type-arg]
    return [f.excerpt for f in findings]


def test_a_pull_request_scan_finds_a_secret_in_its_own_history(
    scan: ModuleType, repo: Path
) -> None:
    findings = scan.scan_history(POLICY, base="main", repo=repo)
    assert any(SECRET in e and "OTHER" not in e for e in _excerpts(findings))


def test_another_branch_does_not_leak_into_the_scan(scan: ModuleType, repo: Path) -> None:
    for base in ("main", None):
        findings = scan.scan_history(POLICY, base=base, repo=repo)
        assert not any("OTHER" in e for e in _excerpts(findings)), base


def test_a_branch_scan_covers_its_whole_history(scan: ModuleType, repo: Path) -> None:
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    findings = scan.scan_history(POLICY, base=None, repo=repo)
    assert any(SECRET in e for e in _excerpts(findings))


def test_the_tree_scan_sees_the_checked_out_files(scan: ModuleType, repo: Path) -> None:
    _commit(repo, "now.txt", f"key = '{SECRET}'\n", "secret in the tree")
    findings = scan.scan_tree(POLICY, repo=repo)
    assert {f.path for f in findings} == {"now.txt"}


def test_an_unknown_base_is_an_error_not_an_empty_scan(scan: ModuleType, repo: Path) -> None:
    with pytest.raises(scan.UnknownBaseError):
        scan.scan_history(POLICY, base="origin/does-not-exist", repo=repo)
