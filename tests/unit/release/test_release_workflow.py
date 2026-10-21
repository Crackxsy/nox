""".github/workflows/release-assets.yml: the release key is handled the way docs/RELEASES.md says.

Read as YAML, so these hold for the workflow that actually runs: every action pinned to a commit,
the secret only in the one signing step of the one job in the protected environment, the signer
started isolated with the key on stdin, and an unsigned release never published as signed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "release-assets.yml"
SECRET = "secrets.NOX_RELEASE_SIGNING_KEY"


def _workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    return data


def _steps(job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"][job]["steps"]
    return steps


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_the_secret_appears_in_exactly_one_step_of_the_protected_job() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert text.count(SECRET) == 1
    jobs = _workflow()["jobs"]
    holders = [
        (name, step.get("name"))
        for name, job in jobs.items()
        for step in job["steps"]
        if SECRET in str(step.get("env", {}))
    ]
    assert holders == [("manifest", "Write the manifest and sign it")]
    assert jobs["manifest"]["environment"] == "release-signing"
    assert jobs["manifest"]["permissions"] == {"contents": "read"}


def test_the_signer_runs_isolated_with_the_key_on_stdin_only() -> None:
    step = next(s for s in _steps("manifest") if s.get("name") == "Write the manifest and sign it")
    run = " ".join(step["run"].split())
    assert run.startswith(
        "printf '%s\\n' \"$NOX_RELEASE_SIGNING_KEY\" | env -u NOX_RELEASE_SIGNING_KEY"
    )
    assert "python -I -S source/scripts/sign_release.py --key-stdin" in run
    assert '--lib "$RUNNER_TEMP/signer-lib"' in run
    # Nothing else runs in that step: no command substitution, no second command.
    assert "$(" not in run and ";" not in run and "&&" not in run
    install = next(
        s for s in _steps("manifest") if "signer-lib" in str(s.get("run", "")) and s is not step
    )
    assert "--require-hashes" in install["run"] and "--no-deps" in install["run"]
    assert "--only-binary :all:" in install["run"]
    assert "signing-requirements.txt" in install["run"]


def test_an_unsigned_manifest_is_never_published_and_the_run_fails() -> None:
    steps = _steps("upload")
    upload = next(s for s in steps if s.get("name") == "Upload")
    assert 'if [ "$SIGNED" = "true" ]; then' in upload["run"]
    refuse = next(s for s in steps if s.get("name") == "Refuse to publish an unsigned release")
    assert refuse["if"] == "needs.manifest.outputs.signed != 'true'"
    assert "exit 1" in refuse["run"]
