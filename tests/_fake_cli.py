"""A fake command-line program for integration tests, on every operating system.

Several plugins run an external CLI (Claude Code) by absolute path. The tests replace it with a
small Python script behind a wrapper the plugin can spawn like the real thing: a `.cmd` file on
Windows (resolved through `PATHEXT`), an executable shell script elsewhere.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path


def write_fake_cli(
    directory: Path, name: str, script: Path, *, env: Mapping[str, str] | None = None
) -> Path:
    """Write `<directory>/<name>(.cmd)` that runs `script` with the test's Python and its args.

    `env` is set inside the wrapper: the plugin manager passes a plugin only an allow-list of
    environment variables, so one set on the test process would never reach the program.
    """
    env = dict(env or {})
    if sys.platform == "win32":
        lines = ["@echo off"]
        lines += [f"set {key}={value}" for key, value in env.items()]
        lines.append(f'"{sys.executable}" "{script}" %*')
        wrapper = directory / f"{name}.cmd"
        wrapper.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return wrapper
    lines = ["#!/bin/sh"]
    lines += [f"export {key}='{value}'" for key, value in env.items()]
    lines.append(f'exec "{sys.executable}" "{script}" "$@"')
    wrapper = directory / name
    wrapper.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(wrapper, wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper
