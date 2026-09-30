"""`extend` configuration: whether Nox may work on its own source, and where.

Ships disabled, in the same shape as `files.roots` and the preset actions: `workspace` is empty, so
there is no checkout Nox may touch and the tool says which setting to fill in. Naming one is a
deliberate act, and it is the only thing that turns this on.

`test_command` is the other half of the boundary and is a command *line*, which is why it is here
and not something a model can pass. A proposal that has not been through the test suite is a
diff nobody should read, but the suite is also the one place where running something the model
chose would be arbitrary code execution wearing a lab coat.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

from pydantic import Field, field_validator

from nox.core.config.types import StrictSection

__all__ = ["ExtendConfig"]


class ExtendConfig(StrictSection):
    """The `extend` section of `config/defaults.yaml`."""

    enabled: bool = True
    #: The git checkout Nox may propose changes in. Empty means none, which is the default.
    workspace: str = ""
    #: Branches are created here and never merged, pushed or deleted by Nox.
    branch_prefix: str = Field(default="nox/proposal/", min_length=1, max_length=40)
    #: What proves a proposal is not broken. Empty means a proposal ships untested and says so.
    test_command: list[str] = Field(default_factory=list, max_length=16)
    test_timeout_s: float = Field(default=900.0, gt=0.0, le=3600.0)
    #: How many proposals are kept. They are branches on disk; this bounds the record, not the git.
    keep: int = Field(default=20, ge=1, le=200)

    @field_validator("test_command")
    @classmethod
    def _program_is_absolute(cls, value: list[str]) -> list[str]:
        """Same rule the preset actions use: a bare name would be looked up in PATH.

        Which means it could mean a different program on the next start, and this one runs
        unattended against a checkout.
        """
        if not value:
            return value
        program = value[0].strip()
        if not PureWindowsPath(program).is_absolute():
            raise ValueError(
                f"extend.test_command must start with an absolute path, got {program!r}"
            )
        return [program, *(argument.strip() for argument in value[1:])]

    @field_validator("branch_prefix")
    @classmethod
    def _prefix_is_a_git_ref(cls, value: str) -> str:
        prefix = value.strip()
        if prefix.startswith("/") or ".." in prefix or " " in prefix:
            raise ValueError(f"{value!r} is not usable as the start of a branch name")
        return prefix if prefix.endswith("/") else f"{prefix}/"
