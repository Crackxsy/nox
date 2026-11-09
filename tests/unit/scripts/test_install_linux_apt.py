"""`scripts/install-linux.sh` never waits forever for apt (C10): a stuck dpkg lock or a mirror that
stopped answering once held a CI job for 30 minutes. Every apt-get call carries the lock and
network timeouts, has an overall time limit and fails with a clear message."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "install-linux.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _apt_calls(source: str) -> list[str]:
    """Every line that runs apt-get (not `command -v apt-get` and not a message)."""
    joined = re.sub(r"\\\n\s*", " ", source)  # continuation lines joined
    return [
        line.strip()
        for line in joined.splitlines()
        if re.search(r"(^|\s)apt-get\s", line)
        and "command -v" not in line
        and not line.strip().startswith(("#", "'", "define"))
    ]


def test_every_apt_call_has_timeouts_a_limit_and_a_clear_failure() -> None:
    source = _source()
    options = re.search(r"APT_OPTS=\(([^)]*)\)", source)
    assert options is not None
    for option in (
        "DPkg::Lock::Timeout=300",
        "Acquire::http::Timeout=30",
        "Acquire::https::Timeout=30",
        "Acquire::Retries=3",
    ):
        assert f"-o {option}" in options.group(1)
    calls = _apt_calls(source)
    assert len(calls) == 2  # update and install
    for call in calls:
        assert '"${APT_OPTS[@]}"' in call, call
        assert re.search(r"\btimeout \d+ apt-get\b", call), call
        assert '|| fail "$(msg apt_failed' in call, call


def test_the_failure_message_exists_in_both_languages() -> None:
    source = _source()
    block = re.search(r"define apt_failed \\\n\s*'([^']*)' \\\n\s*'([^']*)'", source)
    assert block is not None
    german, english = block.groups()
    assert german.count("%s") == english.count("%s") == 2
    assert "another update" in english


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_script_still_parses() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv
        [str(shutil.which("bash")), "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
