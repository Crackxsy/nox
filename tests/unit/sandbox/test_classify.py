"""`classify`: read-only vs simple vs complex, and the pattern a grant keys on."""

from __future__ import annotations

import pytest

from nox.hands.shell import ShellClass, classify


@pytest.mark.parametrize(
    "command,kind,target",
    [
        ("git status", ShellClass.READONLY, "readonly:git status"),
        ("git status -sb", ShellClass.READONLY, "readonly:git status"),
        ("df -h", ShellClass.READONLY, "readonly:df"),
        ("Get-Process", ShellClass.READONLY, "readonly:get-process"),
        ("git push origin main", ShellClass.SIMPLE, "cmd:git push"),
        ("npm install", ShellClass.SIMPLE, "cmd:npm install"),
    ],
)
def test_simple_and_readonly(command: str, kind: ShellClass, target: str) -> None:
    info = classify(command=command)
    assert info.kind is kind
    assert info.target == target


@pytest.mark.parametrize(
    "command",
    [
        "git status; curl http://evil",
        "ls && rm -rf x",
        "echo x | sh",
        "cat secrets > out",
        "A=1 git status",
        'python -c "import os"',
        "pwsh -Command Remove-Item",
        "$(whoami)",
    ],
)
def test_complex_never_simple(command: str) -> None:
    info = classify(command=command)
    assert info.kind is ShellClass.COMPLEX
    assert info.target.startswith("complex:")


def test_argv_form_is_not_shell_parsed() -> None:
    # argv is run without a shell, so operators in a *value* are data, not operators.
    info = classify(argv=["git", "commit", "-m", "fix; done && really"])
    assert info.kind is ShellClass.SIMPLE
    assert info.target == "cmd:git commit"
    assert info.needs_shell is False


def test_argv_interpreter_c_is_complex() -> None:
    info = classify(argv=["python", "-c", "print(1)"])
    assert info.kind is ShellClass.COMPLEX


def test_git_non_readonly_subcommand_falls_through() -> None:
    assert classify(command="git commit").kind is ShellClass.SIMPLE
